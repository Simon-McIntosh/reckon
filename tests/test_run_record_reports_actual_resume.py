"""A run record distinguishes the lane's session-reuse setting from the fact.

``backends.<name>.session_reuse`` says a lane permits a run to continue an
earlier session. The run record carried that same setting under a bare
``session_reuse`` name, so every spawned run read as one that had resumed
whether or not it carried anything. Measured on 2026-09-17 across this
workstation's live pointers: true on 51 of 51, and 47 of those held no session
id at all; re-measured the same day while the corpus moved, true on 50 of 50,
46 without a session id, 2 whose argv actually carried a prior session.

The range below is the input domain rather than one backend: a lane permitting
reuse with nothing to reuse, the same lane with a captured session, a lane not
permitting it, and the lane-change writer that records the same pair.
"""

from __future__ import annotations

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
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "beta": {
            "launch": "cli",
            "command": "codex",
            "model": "another-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        # The same dialect and command, so a lane change can keep the session,
        # but a lane whose configuration forbids continuation.
        "no-reuse": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        },
        "native": {"launch": "in-harness", "time_budget": "25m"},
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


def _config(**backend_overrides: object) -> dict[str, object]:
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["alpha"].update(backend_overrides)
    return config


def _node(home: Path, sequence: int) -> crew.TaskNode:
    return crew.TaskNode(
        id="resume-observation",
        goal="record whether a run carried a prior session",
        plan="plan-a",
        section="session-routing",
        done_when=(
            "tests/test_run_record_reports_actual_resume.py passes: the run record "
            "reports what this run did rather than what its lane permits"
        ),
        write_paths=["reckon/observation.py"],
        time_budget="20m",
        spec_level="guided",
        manifest_path=str(home / f"resume-observation-{sequence}.md"),
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
        launcher=lambda *args, **kwargs: 999931,
    )


def _complete_stream(record: Mapping[str, object], session_id: str) -> None:
    original = "019ff509-8a60-7723-94fd-65942a6d8faa"
    Path(str(record["log_path"])).write_text(
        FIXTURE.read_text().replace(original, session_id)
    )
    observed = crew.observe(str(record["run_id"]))
    assert observed["phase"] == "complete"
    assert observed["session_id"] == session_id


def _register(repo: Path, harness: str = "alpha") -> None:
    ledger.register_member("proj", "worker-a", harness=harness, root=repo)


def test_a_lane_permitting_reuse_reports_no_resumption_without_a_session(
    home: Path, repo: Path
) -> None:
    """The lane permits continuation; this run had nothing to continue."""
    _register(repo)

    dispatched = _dispatch(home, repo, 1)

    assert dispatched["session_reuse_capable"] is True
    assert dispatched["session_resumed"] is False
    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]
    assert "session_reuse" not in dispatched


def test_a_captured_session_is_reported_as_a_resumption(home: Path, repo: Path) -> None:
    """The same lane, this time with a session the run actually carried."""
    _register(repo)
    _complete_stream(_dispatch(home, repo, 1), MEDIUM_SESSION)

    second = _dispatch(home, repo, 2)

    assert second["session_reuse_capable"] is True
    assert second["session_resumed"] is True
    assert second["session_id"] == MEDIUM_SESSION
    assert second["argv"][second["argv"].index("resume") + 1] == MEDIUM_SESSION


def _default_lane(backend: str) -> dict[str, object]:
    config = json.loads(json.dumps(CONFIG))
    config["default_backend"] = backend
    return config


def test_a_lane_without_the_capability_never_reports_a_resumption(
    home: Path, repo: Path
) -> None:
    """A captured session exists for this lane, and the lane forbids reuse."""
    _register(repo)
    _complete_stream(
        _dispatch(home, repo, 1, config=_default_lane("no-reuse")), MEDIUM_SESSION
    )

    dispatched = _dispatch(home, repo, 2, config=_default_lane("no-reuse"))

    assert dispatched["backend"] == "no-reuse"
    assert dispatched["session_reuse_capable"] is False
    assert dispatched["session_resumed"] is False
    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]
    assert "session_reuse" not in dispatched


def _live_run(home: Path, repo: Path, sequence: int) -> dict[str, object]:
    """A dispatched run whose pointer holds a session the lane change can use."""
    _register(repo)
    record = _dispatch(home, repo, sequence)
    pointer = crew.read_pointer(str(record["run_id"]))
    pointer.update({"phase": "working", "session_id": MEDIUM_SESSION, "pid": 41001})
    crew._write_json(crew.pointer_path(str(record["run_id"])), pointer)
    return pointer


def test_a_lane_change_that_keeps_the_session_reports_a_resumption(
    home: Path, repo: Path
) -> None:
    from reckon.crew.dispatch import change_lane

    pointer = _live_run(home, repo, 1)

    moved = change_lane(
        str(pointer["run_id"]),
        "beta",
        "the first lane is spent",
        config=CONFIG,
        advice="continue where you left off",
        launcher=lambda *args, **kwargs: 42002,
    )

    assert moved["lane_changes"][-1]["session"] == "continued"
    assert moved["session_reuse_capable"] is True
    assert moved["session_resumed"] is True
    assert moved["session_id"] == MEDIUM_SESSION
    assert "session_reuse" not in moved


def test_a_lane_change_to_a_harness_that_cannot_follow_reports_none(
    home: Path, repo: Path
) -> None:
    from reckon.crew.dispatch import change_lane

    pointer = _live_run(home, repo, 1)

    moved = change_lane(
        str(pointer["run_id"]),
        "native",
        "the calling harness will hold the run",
        config=CONFIG,
        advice="continue where you left off",
        launcher=lambda *args, **kwargs: 42003,
    )

    assert moved["lane_changes"][-1]["session"] == "fresh"
    assert moved["session_reuse_capable"] is False
    assert moved["session_resumed"] is False
