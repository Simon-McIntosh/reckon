"""A dispatch-time session comes from the task's own runs, not from the roster.

The roster entry describes the member. Captured sessions live on run records,
and dispatch selects a continuation from its own task's runs. A legacy session
on the member, including one under a matching configuration, is never offered.
Capture preserves every member field while recording the session on its run.

The end-to-end cases drive `crew.dispatch` and read the launched argv back,
since a helper-level assertion is exactly how the resumed-conversation defect
stayed invisible.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import resumption

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
STORED_SESSION = "266f04b2-75c1-43f0-aa27-0d72a67b340f"
FIXTURE = Path(__file__).parent / "fixtures" / "backends" / "codex-turn.jsonl"
FIXTURE_SESSION = "019ff509-8a60-7723-94fd-65942a6d8faa"

NODE_ID = "session-scope-node"


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


def _node(home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="verify a roster session is not the dispatch-time authority",
        plan="plan-a",
        section="session-routing",
        spec_level="guided",
        done_when=(
            "pytest tests/test_worker_session_is_session_scoped.py reports every "
            "case passing"
        ),
        write_paths=[f"reckon/session-scope/{NODE_ID}.py"],
        time_budget="20m",
        manifest_path=str(home / f"{NODE_ID}.md"),
    )


def _dispatch(
    home: Path, repo: Path, *, session: str = "coordinator-1"
) -> dict[str, object]:
    return crew.dispatch(
        node=_node(home),
        project="proj",
        repo=repo,
        config=CONFIG,
        session=session,
        member="worker-a",
        launcher=lambda *args, **kwargs: 999921,
    )


def _complete(record: Mapping[str, object], session_id: str) -> None:
    Path(str(record["log_path"])).write_text(
        FIXTURE.read_text().replace(FIXTURE_SESSION, session_id)
    )
    observed = crew.observe(str(record["run_id"]))
    assert observed["phase"] == "complete"
    assert observed["session_id"] == session_id


def _stored_roster_session(repo: Path) -> None:
    """A roster entry holding a session this task never ran."""
    data, version = ledger.load("proj", repo)
    data["members"][0].update(
        {
            "session_id": STORED_SESSION,
            "session_model": "some-model",
            "sessions": {"some-model": STORED_SESSION},
        }
    )
    ledger.write("proj", data, version, repo)


def test_a_roster_session_no_run_of_this_task_left_is_withheld(
    home: Path, repo: Path
) -> None:
    """Asserted against the launched argv, not against the stored record."""
    _stored_roster_session(repo)

    dispatched = _dispatch(home, repo)

    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]


def test_the_absence_names_that_no_run_of_this_task_left_a_session(
    home: Path, repo: Path
) -> None:
    _stored_roster_session(repo)

    dispatched = _dispatch(home, repo)

    absence = dispatched["session_id_absent"]
    assert absence["point"] == "dispatch-no-same-task-session"
    assert absence["reason"]


def test_a_run_of_this_task_supplies_the_session_instead(
    home: Path, repo: Path
) -> None:
    """The run record is the authority; the roster entry is beside the point."""
    _stored_roster_session(repo)
    _complete(_dispatch(home, repo), STORED_SESSION)

    again = _dispatch(home, repo, session="coordinator-2")

    assert again["session_id"] == STORED_SESSION
    assert again["argv"][again["argv"].index("resume") + 1] == STORED_SESSION
    assert again["session_withheld"] is None


# ── capture persists the session on its run ────────────────────────────────


def test_the_capture_path_records_the_session_on_the_run(
    home: Path, repo: Path
) -> None:
    roster_path = repo / "docs" / "state" / "proj" / "crew.json"
    roster_before = roster_path.read_bytes()
    dispatched = _dispatch(home, repo)
    _complete(dispatched, STORED_SESSION)

    recorded = crew.read_pointer(str(dispatched["run_id"]))
    assert recorded["session_capture"]["captured"] is True
    assert recorded["session_capture"]["run_id"] == dispatched["run_id"]
    assert recorded["session_id"] == STORED_SESSION
    assert recorded["session_harness"] == "codex"
    assert recorded["session_model"] == "some-model"
    assert roster_path.read_bytes() == roster_before


# ── the run record still resolves a session ─────────────────────────────────


def test_a_run_whose_member_holds_no_matching_entry_still_resolves(
    repo: Path,
) -> None:
    """The roster is not the recovery path; the run's own record is."""
    pointer = {
        "run_id": "run-no-matching-entry",
        "project": "proj",
        "member": "worker-a",
        "session_id": STORED_SESSION,
        "launch": "cli",
        "log_path": str(repo / "absent-stream.jsonl"),
    }

    resolved = resumption.resolve_session("run-no-matching-entry", record=pointer)

    assert resolved["resolved"] is True
    assert resolved["session_id"] == STORED_SESSION
    assert resolved["source"] == "pointer"
