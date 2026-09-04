"""A session id set through `member add` is reusable by a later dispatch.

`member add` has no `--model` flag, so a session it records carries no
configuration of its own. These tests exercise both entry points a
coordinator actually uses — `ledger.register_member` (what `crew member add`
calls) and `crew.dispatch` (what consumes the roster) — never
`_session_for_configuration` alone, since a helper-level assertion is exactly
how the defect this covers stayed invisible.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.calibration import agent_configuration_key

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}
MEDIUM_AGENT = {
    "backend": "alpha",
    "launch": "cli",
    "model": "some-model",
    "effort": "medium",
    "sandbox": "worktree-full",
}
BARE_SESSION = "266f04b2-75c1-43f0-aa27-0d72a67b340f"
FIXTURE = Path(__file__).parent / "fixtures" / "backends" / "codex-turn.jsonl"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text()
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="session-routing">Session routing</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _configuration_key(agent: Mapping[str, object]) -> str:
    return agent_configuration_key({"agent": agent})


def _config(**role_overrides: object) -> dict[str, object]:
    config = json.loads(json.dumps(CONFIG))
    if role_overrides:
        config["roles"]["implement"] = role_overrides
    return config


def _node(home: Path, sequence: int) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"member-node-{sequence}",
        goal="verify a bare member-add session resolves against the harness default",
        plan="plan-a",
        section="session-routing",
        done_when=("pytest tests/test_crew_members.py reports every case passing"),
        write_paths=[f"reckon/member_{sequence}.py"],
        time_budget="20m",
        manifest_path=str(home / f"member-node-{sequence}.md"),
    )


def _dispatch(
    home: Path,
    repo: Path,
    sequence: int,
    *,
    config: Mapping[str, object] = CONFIG,
    member: str = "worker-a",
) -> dict[str, object]:
    return crew.dispatch(
        node=_node(home, sequence),
        project="proj",
        repo=repo,
        config=config,
        session=f"coordinator-{sequence}",
        member=member,
        launcher=lambda *args, **kwargs: 0,
    )


def _complete_stream(record: Mapping[str, object], session_id: str) -> None:
    original = "019ff509-8a60-7723-94fd-65942a6d8faa"
    Path(str(record["log_path"])).write_text(
        FIXTURE.read_text().replace(original, session_id)
    )
    observed = crew.observe(str(record["run_id"]))
    assert observed["phase"] == "complete"
    assert observed["session_id"] == session_id


def test_bare_session_from_member_add_resumes_and_then_records_its_configuration(
    home: Path, repo: Path
) -> None:
    """The command example in the fix's own done-when: no model, no capture."""
    entry = ledger.register_member(
        "proj", "worker-a", harness="alpha", session_id=BARE_SESSION, root=repo
    )
    assert entry["session_model"] is None
    assert entry["sessions"] == {}

    dispatched = _dispatch(home, repo, 1)

    assert dispatched["session_id"] == BARE_SESSION
    assert dispatched["argv"][dispatched["argv"].index("resume") + 1] == BARE_SESSION

    _complete_stream(dispatched, BARE_SESSION)

    member = ledger.member("proj", "worker-a", repo)
    assert member is not None
    assert member["session_model"] == "some-model"
    assert member["sessions"] == {_configuration_key(MEDIUM_AGENT): BARE_SESSION}


def test_bare_session_ignored_when_the_dispatched_model_moves_off_the_harness_default(
    home: Path, repo: Path
) -> None:
    """A recorded harness (`alpha`) whose role overlay now serves a different
    model does not risk resuming a session that may belong to neither."""
    before = ledger.register_member(
        "proj", "worker-a", harness="alpha", session_id=BARE_SESSION, root=repo
    )

    dispatched = _dispatch(home, repo, 1, config=_config(model="other-model"))

    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]

    after = ledger.member("proj", "worker-a", repo)
    assert after == before


def test_member_with_no_recorded_session_is_unaffected(home: Path, repo: Path) -> None:
    ledger.register_member("proj", "worker-a", harness="alpha", root=repo)

    dispatched = _dispatch(home, repo, 1)

    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]
