"""Roster session reuse follows the complete resolved agent configuration."""

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
XHIGH_AGENT = {**MEDIUM_AGENT, "effort": "xhigh"}
MEDIUM_SESSION = "066f04b2-75c1-43f0-aa27-0d72a67b340f"
XHIGH_SESSION = "166f04b2-75c1-43f0-aa27-0d72a67b340f"
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


def _config(**backend_overrides: object) -> dict[str, object]:
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["alpha"].update(backend_overrides)
    return config


def _node(home: Path, sequence: int) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"session-node-{sequence}",
        goal="verify configuration-scoped session reuse",
        plan="plan-a",
        section="session-routing",
        done_when=(
            "pytest tests/test_crew_session_keying.py reports 6 passing cases and "
            "each resolved argv carries at most 1 eligible session"
        ),
        write_paths=[f"reckon/session_{sequence}.py"],
        time_budget="20m",
        manifest_path=str(home / f"session-node-{sequence}.md"),
    )


def _dispatch(
    home: Path,
    repo: Path,
    sequence: int,
    *,
    config: Mapping[str, object] = CONFIG,
) -> dict[str, object]:
    return crew.dispatch(
        node=_node(home, sequence),
        project="proj",
        repo=repo,
        config=config,
        session=f"coordinator-{sequence}",
        member="worker-a",
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


def _register(repo: Path) -> None:
    ledger.register_member("proj", "worker-a", harness="alpha", root=repo)


def test_two_dispatches_at_the_same_configuration_resume_one_session(
    home: Path, repo: Path
) -> None:
    _register(repo)
    first = _dispatch(home, repo, 1)
    _complete_stream(first, MEDIUM_SESSION)

    second = _dispatch(home, repo, 2)

    assert second["session_id"] == MEDIUM_SESSION
    assert second["argv"][second["argv"].index("resume") + 1] == MEDIUM_SESSION


def test_changed_effort_starts_fresh_and_both_sessions_remain_reusable(
    home: Path, repo: Path
) -> None:
    _register(repo)
    first = _dispatch(home, repo, 1)
    _complete_stream(first, MEDIUM_SESSION)

    changed = _dispatch(home, repo, 2, config=_config(effort="xhigh"))
    assert changed["session_id"] is None
    assert "resume" not in changed["argv"]
    _complete_stream(changed, XHIGH_SESSION)

    medium_again = _dispatch(home, repo, 3)
    assert medium_again["session_id"] == MEDIUM_SESSION
    _complete_stream(medium_again, MEDIUM_SESSION)

    xhigh_again = _dispatch(home, repo, 4, config=_config(effort="xhigh"))
    assert xhigh_again["session_id"] == XHIGH_SESSION
    assert xhigh_again["argv"][xhigh_again["argv"].index("resume") + 1] == XHIGH_SESSION
    member = ledger.member("proj", "worker-a", repo)
    assert member and member["sessions"] == {
        _configuration_key(MEDIUM_AGENT): MEDIUM_SESSION,
        _configuration_key(XHIGH_AGENT): XHIGH_SESSION,
    }


def test_changed_model_starts_fresh(home: Path, repo: Path) -> None:
    _register(repo)
    first = _dispatch(home, repo, 1)
    _complete_stream(first, MEDIUM_SESSION)

    changed = _dispatch(home, repo, 2, config=_config(model="other-model"))

    assert changed["session_id"] is None
    assert "resume" not in changed["argv"]


def _write_legacy_member(
    repo: Path, *, capturing_agent: Mapping[str, object] | None
) -> None:
    _register(repo)
    data, version = ledger.load("proj", repo)
    data["members"][0].update(
        {
            "session_id": MEDIUM_SESSION,
            "session_model": "some-model",
            "sessions": {"some-model": MEDIUM_SESSION},
        }
    )
    if capturing_agent is not None:
        data["runs"].append(
            {
                "run_id": "legacy-capture",
                "member": "worker-a",
                "session_id": MEDIUM_SESSION,
                "agent": dict(capturing_agent),
            }
        )
    ledger.write("proj", data, version, repo)


def test_legacy_model_key_reuses_when_capture_configuration_matches(
    home: Path, repo: Path
) -> None:
    _write_legacy_member(repo, capturing_agent=MEDIUM_AGENT)

    dispatched = _dispatch(home, repo, 1)

    assert dispatched["session_id"] == MEDIUM_SESSION
    assert dispatched["argv"][dispatched["argv"].index("resume") + 1] == MEDIUM_SESSION


@pytest.mark.parametrize("capturing_agent", [None, MEDIUM_AGENT])
def test_legacy_model_key_starts_fresh_without_matching_capture_evidence(
    home: Path,
    repo: Path,
    capturing_agent: Mapping[str, object] | None,
) -> None:
    _write_legacy_member(repo, capturing_agent=capturing_agent)

    dispatched = _dispatch(home, repo, 1, config=_config(effort="xhigh"))

    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]
