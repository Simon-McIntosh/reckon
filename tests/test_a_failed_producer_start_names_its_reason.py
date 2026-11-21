"""A producer that cannot start names its reason and holds no launch.

A project's watcher can fail at start: the backend command a project routes work
to stops resolving, so every new producer exits before its first sweep. The
supervising wrapper used to send that producer's standard error to the null
device, so the watch log held only "producer crashed repeatedly" and never the
cause; a registration left by a producer that had taken its seat went on naming
the dead producer beside a lease that read healthy; and an arming caller held the
arming lock while it waited for a producer that never registers live, so every
other launch met that held lock and was refused.

Each fact is measured against a producer command that writes a message on
standard error and exits non-zero, in one of the two shapes the failure takes: it
never reaches the seat (``before-seat``), or it takes the seat and then dies
(``register``). At the base revision no start failure is recorded at all, so the
log carries no message, no session's obligations name it, the registration keeps
the dead pid, and a launch is refused as a project with no live watcher.
"""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_watch, obligations, runs, watch_unit

PROJECT = "failed-start-fixture"
SESSION = "failed-start-session"
QUEUED_RUN_ID = "r-failed-start-queued"
MESSAGE = "launch resolution failed: backend command 'no-such-backend' not found"

pytestmark = pytest.mark.arms_watch_producer

_PRODUCER_SCRIPT = """
import sys
from reckon.crew import recovery_watch

project = sys.argv[1]
message = sys.argv[2]
mode = sys.argv[3]
if mode == 'register':
    with recovery_watch._watch_registration(project, '1m') as (acquired, _watcher):
        if not acquired:
            raise SystemExit(3)
        sys.stderr.write(message + '\\n')
        raise SystemExit(1)
sys.stderr.write(message + '\\n')
raise SystemExit(1)
"""


def _producer_argv(log: Path, mode: str) -> list[str]:
    """The supervisor argv the arming path is handed, with a fast backoff."""
    return [
        sys.executable,
        "-c",
        dispatch_watch._WATCH_PRODUCER_SUPERVISOR,
        dispatch_watch.WATCH_LOG_ENV,
        str(log),
        "0.05",
        "0.1",
        "5.0",
        PROJECT,
        str(Path(dispatch_watch.__file__).resolve().parents[2]),
        sys.executable,
        "-c",
        _PRODUCER_SCRIPT,
        PROJECT,
        MESSAGE,
        mode,
    ]


def _arm(tmp_path, monkeypatch, mode: str) -> dict:
    """Run the arming supervisor to its restart limit, and report what it left."""
    log = tmp_path / "watch.log"
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(dispatch_watch.__file__).resolve().parents[2])
    )
    monkeypatch.setattr(dispatch_watch, "_read_fleet_record", lambda: None)
    monkeypatch.setattr(
        dispatch_watch,
        "_watch_producer_argv",
        lambda _project: _producer_argv(log, mode),
    )
    monkeypatch.setenv("RECKON_WATCH_ARMING", "on")
    supervisor = dispatch_watch._start_watch_producer(PROJECT)
    supervisor.wait(timeout=90)
    return {"log": log, "supervisor": supervisor}


@pytest.fixture()
def failing_start(tmp_path, monkeypatch):
    """A producer that never reaches its seat and exits naming its reason."""
    return _arm(tmp_path, monkeypatch, "before-seat")


@pytest.fixture()
def registered_failure(tmp_path, monkeypatch):
    """A producer that takes its seat and then dies naming its reason."""
    return _arm(tmp_path, monkeypatch, "register")


def _queued_pointer() -> dict:
    """A queued request in the project, held by the session under test."""
    directory = runs.runs_dir() / QUEUED_RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": QUEUED_RUN_ID,
        "project": PROJECT,
        "repo": str(runs.runs_dir()),
        "worktree": str(directory / "tree"),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "codex",
        "role": "implement",
        "session": SESSION,
        "session_id": SESSION,
        "created_at": "2026-01-01T00:00:00Z",
        "attempt_started_at": "2026-01-01T00:00:00Z",
        "attempt": 1,
        "phase": "queued",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(directory / "manifest.md"),
        "node": {"id": QUEUED_RUN_ID},
    }
    runs._write_json(runs.pointer_path(QUEUED_RUN_ID), record)
    return record


def test_the_watch_log_carries_the_producer_message(failing_start):
    """The supervising wrapper no longer discards the reason the producer gave."""
    assert MESSAGE in failing_start["log"].read_text()


def test_the_sessions_queued_work_names_the_failed_start(failing_start):
    """The duties of the session holding a queued request name the failed start."""
    _queued_pointer()
    rows = [
        row
        for row in obligations.obligations(PROJECT, SESSION)["obligations"]
        if row.get("kind") == "producer-start-failed"
    ]
    assert len(rows) == 1, rows
    assert MESSAGE in rows[0]["reason"]
    assert PROJECT in rows[0]["next_command"]


def test_the_registration_names_no_live_producer(registered_failure):
    """The record names no producer once it is gone, and keeps no fresher lease."""
    state = watch_unit.watch_state(PROJECT)
    assert state["watcher_live"] is False
    registration = runs.read_watch_registration(PROJECT)
    assert not registration.get("pid"), registration
    failure = watch_unit.read_producer_start_failure(PROJECT)
    assert failure is not None
    lease = registration.get("lease_renewed_at")
    assert lease is None or lease <= failure["failed_at"]


def test_a_launch_while_the_start_fails_is_admitted(failing_start, monkeypatch):
    """A launch met by a failing producer start proceeds with none armed."""
    monkeypatch.setattr(dispatch_watch, "WATCHER_LOAD_BOUND_SECONDS", 1.0)
    lock = dispatch_watch.watch_stream_path(PROJECT).with_suffix(".arm.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("wb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = dispatch_watch._ensure_watch_producer(PROJECT, session=SESSION)
    assert state["watcher_live"] is False
    assert state["producer_armed"] is False
    assert state["producer_start_failure"]["message"] == MESSAGE
    warning = dispatch_watch._watcher_delivery_admission(
        PROJECT, state, session=SESSION, launch_kind="cli"
    )
    assert warning and "no watch producer" in warning
