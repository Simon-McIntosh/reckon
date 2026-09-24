"""A dispatch continues a session keyed to the task, not to the roster entry.

The reuse question is "has this task a session to continue?", answered from the
run records of that task. Keying it to the roster's resolved agent
configuration answered a different question — a member holding one session
under one configuration offered it to every node dispatched at that
configuration, so a new node resumed another node's conversation. The
configuration still selects the lane; it no longer selects the session.

Each case drives `crew.dispatch` and reads the composed argv back, because a
helper-level assertion is how the resumed-conversation defect stayed invisible.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from reckon import crew, ledger

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}
MEDIUM_SESSION = "066f04b2-75c1-43f0-aa27-0d72a67b340f"
XHIGH_SESSION = "166f04b2-75c1-43f0-aa27-0d72a67b340f"
FIXTURE = Path(__file__).parent / "fixtures" / "backends" / "codex-turn.jsonl"
FIXTURE_SESSION = "019ff509-8a60-7723-94fd-65942a6d8faa"
PLAN = "plan-a"

_SESSIONS = itertools.count(1)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
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
    ledger.register_member("proj", "worker-a", harness="alpha", root=root)
    return root


def _config(**backend_overrides: object) -> dict[str, object]:
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["alpha"].update(backend_overrides)
    return config


def _node(node_id: str, home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="verify that a dispatch continues only its own task's session",
        plan=PLAN,
        section="session-routing",
        done_when="pytest tests/test_crew_session_keying.py reports every case passing",
        write_paths=[f"reckon/session-keying/{node_id}.py"],
        time_budget="20m",
        spec_level="guided",
        manifest_path=str(home / f"session-keying-{node_id}.md"),
    )


def _dispatch(
    home: Path,
    repo: Path,
    node_id: str,
    *,
    config: Mapping[str, object] = CONFIG,
) -> dict[str, object]:
    return crew.dispatch(
        node=_node(node_id, home),
        project="proj",
        repo=repo,
        config=config,
        session=f"coordinator-{next(_SESSIONS)}",
        member="worker-a",
        launcher=lambda *args, **kwargs: 999911,
    )


def _complete(record: Mapping[str, object], session_id: str) -> None:
    Path(str(record["log_path"])).write_text(
        FIXTURE.read_text().replace(FIXTURE_SESSION, session_id)
    )
    observed = crew.observe(str(record["run_id"]))
    assert observed["phase"] == "complete"
    assert observed["session_id"] == session_id


def _resumed(argv: list[str]) -> str | None:
    return argv[argv.index("resume") + 1] if "resume" in argv else None


def test_two_dispatches_of_one_node_resume_one_session(home: Path, repo: Path) -> None:
    """The task, not the member, is what a session is continued for."""
    _complete(_dispatch(home, repo, "session-node"), MEDIUM_SESSION)

    again = _dispatch(home, repo, "session-node")

    assert again["session_id"] == MEDIUM_SESSION
    assert _resumed(list(again["argv"])) == MEDIUM_SESSION


def test_another_node_at_the_same_configuration_starts_fresh(
    home: Path, repo: Path
) -> None:
    """The configuration keyed the lane; it must not key the session."""
    _complete(_dispatch(home, repo, "session-node"), MEDIUM_SESSION)

    other = _dispatch(home, repo, "other-node")

    assert other["session_id"] is None
    assert _resumed(list(other["argv"])) is None


def test_a_changed_configuration_still_continues_the_same_task(
    home: Path, repo: Path
) -> None:
    """A moved lane is the same task: the session is offered again."""
    _complete(_dispatch(home, repo, "session-node"), MEDIUM_SESSION)

    moved = _dispatch(home, repo, "session-node", config=_config(effort="xhigh"))

    assert moved["session_id"] == MEDIUM_SESSION
    assert _resumed(list(moved["argv"])) == MEDIUM_SESSION


def test_a_roster_session_the_task_never_ran_is_not_offered(
    home: Path, repo: Path
) -> None:
    """A session set on the roster by hand proves nothing about this task."""
    data, version = ledger.load("proj", repo)
    data["members"][0].update(
        {"session_id": MEDIUM_SESSION, "session_model": "some-model"}
    )
    ledger.write("proj", data, version, repo)

    dispatched = _dispatch(home, repo, "session-node")

    assert dispatched["session_id"] is None
    assert _resumed(list(dispatched["argv"])) is None
