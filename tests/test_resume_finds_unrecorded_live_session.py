"""A worker whose supervisor wrote no record is still found by its session id.

A resumed worker is launched with the run's session id as one argument of its
command line. When its supervisor exits first it is reparented to init and
writes no attempt record, so no pid on the run names it: every pid the record
carries is dead while the worker goes on writing into the same worktree. Both
doors that can start a worker — a hand-typed ``crew resume`` and the automatic
resume sweep — read liveness through one owner, so the command line is the
place that still answers, and a second worker is refused while one is there.

The reading is a whole-argument match: a session id that merely appears inside
another argument belongs to no worker this run started. The caller and its
ancestors are never the worker, so they are excluded from the walk.

The declared mutation, applied verbatim in a scratch copy: with the whole-argv
session search removed, the first case's launch plan is built and the test
fails.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon.crew import resumption, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log.
DECLARED_MUTATION = "Removing the whole-argv session search permits the second launch."

PROJECT = "proj"
HOST = socket.gethostname()
SESSION = "3f9a1c62-5c2b-4a4e-9d3f-fixture-session"
OTHER_SESSION = "7b1e2d43-0a6f-4b8c-8e17-another-session"
RUN = "r-unrecorded-live-session"
OTHER_RUN = "r-unrecorded-other-session"
EMBEDDED_RUN = "r-embedded-session-argument"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _real_crew_home() -> Path:
    """The crew home a case writes to if its isolation is not in place."""
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases would leave under a crew home.

    Named for the run ids above, so the same list describes the real home and
    the stand-in: the assertion below is the receipt that this file wrote
    nothing into a live fleet's home while it ran.
    """
    run_ids = (RUN, OTHER_RUN, EMBEDDED_RUN)
    return [
        *(crew_home / "runs" / run_id for run_id in run_ids),
        *(crew_home / "live" / f"{run_id}.json" for run_id in run_ids),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [path for path in _case_artifacts(_real_crew_home()) if path.exists()]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway repository carrying the fleet script a launch composes with."""
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(source.read_text())
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


@contextmanager
def _session_process(argument: str):
    """A genuine live process whose command line carries one argument.

    The child sleeps on its own clock and is killed and waited on by pid on the
    way out, so it is a real process-table entry for the whole case and never
    outlives it.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", argument],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


def _pointer(
    tmp_path: Path,
    repo: Path,
    run_id: str,
    *,
    session_id: str,
) -> dict:
    """A stopped run whose pids are all dead and whose session id it records.

    The shape is the incident's: the pointer names a supervisor that has
    exited, its worker record names nothing, and the session id is the only
    thread back to the worker still driving the run.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: resume-finds-unrecorded-live-session\nstatus: waiting\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "pid": _absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": session_id,
        "created_at": "2026-09-29T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "phase": "working",
        "launcher_host": HOST,
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "",
            "time_budget": "30m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


# --- Both doors refuse while the session's process lives --------------------


def test_the_manual_resume_is_refused_beside_the_sessions_live_process(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """Every recorded pid is dead, and the command line still holds the worker."""
    _pointer(tmp_path, repo, RUN, session_id=SESSION)

    with _session_process(SESSION):
        assert runs.record_process_alive(runs.read_pointer(RUN)) is True
        with pytest.raises(CrewError) as raised:
            resume_plan(RUN, "continue the same task", config=CONFIG)

    refusal = str(raised.value)
    assert "still has a live process" in refusal, refusal
    assert "before resuming" in refusal, refusal


def test_the_sweeps_door_refuses_the_same_record_with_the_same_message(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The sweep's mirror of the launcher refuses the same record identically."""
    record = _pointer(tmp_path, repo, RUN, session_id=SESSION)
    with _session_process(SESSION):
        predicted = resumption._launcher_refusal(record, config=CONFIG)
        with pytest.raises(CrewError) as raised:
            resume_plan(RUN, "continue the same task", config=CONFIG)

    assert predicted is not None
    assert "still has a live process" in str(predicted), str(predicted)
    assert str(predicted) == str(raised.value)


# --- Once the process exits, both admit ------------------------------------


def test_both_doors_admit_once_the_sessions_process_exits(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The same record admits when the command line no longer names the session."""
    record = _pointer(tmp_path, repo, RUN, session_id=SESSION)
    with _session_process(SESSION):
        pass

    plan = resume_plan(RUN, "continue the same task", config=CONFIG)

    assert plan.resumed_session == SESSION
    assert resumption._launcher_refusal(record, config=CONFIG) is None


# --- A different session, and a session inside another argument ------------


def test_a_live_process_carrying_another_session_refuses_neither_door(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """Somebody else's live session is not this run's worker."""
    record = _pointer(tmp_path, repo, OTHER_RUN, session_id=OTHER_SESSION)

    with _session_process("a-session-that-is-not-this-run"):
        plan = resume_plan(OTHER_RUN, "continue the same task", config=CONFIG)
        predicted = resumption._launcher_refusal(record, config=CONFIG)

    assert plan.resumed_session == OTHER_SESSION
    assert predicted is None


def test_a_session_id_inside_another_argument_is_not_a_match(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The match is a whole argument, so an embedded id names no worker."""
    record = _pointer(tmp_path, repo, EMBEDDED_RUN, session_id=EMBEDDED_RUN)

    with _session_process(f"--resume={EMBEDDED_RUN}"):
        plan = resume_plan(EMBEDDED_RUN, "continue the same task", config=CONFIG)
        predicted = resumption._launcher_refusal(record, config=CONFIG)

    assert plan.resumed_session == EMBEDDED_RUN
    assert predicted is None
