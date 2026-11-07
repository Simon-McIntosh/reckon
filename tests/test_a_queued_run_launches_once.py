"""A run whose worker is live refuses a second launch at the supervisor's spawn.

A queued run admitted twice was launched into one worktree: attempt 1 recorded
its worker pid at 21:30:35Z, its dispatch claim was written again at 21:31:10Z,
and attempt 2 recorded its own worker pid at 21:32:07Z while attempt 1 ran on
until 22:04:47Z. Both wrote the same manifest in turn, so the run's record of
what landed was whatever the later writer held.

Every case works against a temporary crew home and a temporary run directory,
so none reads or writes the operator's configuration home.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import runs
from reckon.crew.node import CrewError

RUN_ID = "r-queued-launch-once"
SESSION_ID = "6f1c2d34-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


@pytest.fixture()
def queued_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run whose second attempt is starting."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "phase": "starting",
            "backend": "probe",
            "launch": "cli",
            "launcher_host": "this-host",
            "attempt": 2,
            "attempt_kind": "dispatch",
            "session": "session-1",
            "session_id": SESSION_ID,
            "worktree": str(directory),
            "manifest_path": str(directory / "manifest.md"),
            "log_path": str(directory / "stream.jsonl"),
            "stderr_path": str(directory / "stderr.log"),
            "node": {"id": "node-queued-launch-once", "plan": "fixture"},
        },
    )
    # The launcher publishes this attempt's identity before it starts the
    # supervisor, so the spawn seam reads the same marker a real launch does.
    runs._write_json(
        directory / "attempt.json",
        {
            "run_id": RUN_ID,
            "attempt": 2,
            "attempt_kind": "dispatch",
            "attempt_started_at": "2026-11-07T00:00:00Z",
        },
    )
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _spec(directory: Path) -> dict:
    """The supervisor's spec for a second attempt of the fixture run."""
    return {
        "run_id": RUN_ID,
        "run_directory": str(directory),
        "attempt": 2,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-11-07T00:00:00Z",
        "prompt_path": str(directory / "prompt.txt"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "plan": {
            "argv": [sys.executable, "-c", "pass"],
            "cwd": str(directory),
            "environment": {},
            "dialect": "",
        },
    }


def _write_worker_receipt(directory: Path, attempt: int, pid: int) -> None:
    """File an earlier attempt's immutable worker receipt, as a launch does."""
    runs._write_json(
        directory / f"attempt-{attempt}-worker.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "pid": pid,
            "launched_at": "2026-11-07T00:00:00Z",
            "backend": "probe",
            "argv": [],
        },
    )


def _live_child(*extra: str) -> subprocess.Popen:
    """A process that stays alive until the case stops it."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", *extra]
    )


def _dead_pid() -> int:
    """A pid whose process has exited and been collected."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def test_a_second_launch_beside_a_live_worker_is_refused(queued_run: Path) -> None:
    worker = _live_child()
    try:
        _write_worker_receipt(queued_run, 1, worker.pid)
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert f"still has a live worker (pid {worker.pid})" in str(refused.value), (
            refused.value
        )
        assert not (queued_run / "worker.json").exists(), (
            "the refused launch wrote a worker record"
        )
    finally:
        _stop(worker)


def test_a_second_launch_is_refused_through_the_session_walk(queued_run: Path) -> None:
    gone = _dead_pid()
    _write_worker_receipt(queued_run, 1, gone)
    stand_in = _live_child(SESSION_ID)
    try:
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert f"still has a live worker (pid {stand_in.pid})" in str(refused.value), (
            refused.value
        )
    finally:
        _stop(stand_in)


def test_a_launch_after_the_worker_exited_proceeds(queued_run: Path) -> None:
    _write_worker_receipt(queued_run, 1, _dead_pid())
    pid = dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
    assert pid > 0
    record = json.loads((queued_run / "worker.json").read_text(encoding="utf-8"))
    assert record["attempt"] == 2, "the launched attempt did not record its worker"


def test_removing_the_liveness_read_launches_the_second_worker(
    queued_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal above is the guard: with it gone the second worker starts."""
    if not hasattr(dispatch_launch_module, "_live_worker_beside_the_launch"):
        pytest.skip("this revision has no liveness read to remove")
    worker = _live_child()
    try:
        _write_worker_receipt(queued_run, 1, worker.pid)
        monkeypatch.setattr(
            dispatch_launch_module,
            "_live_worker_beside_the_launch",
            lambda *args, **kwargs: None,
        )
        pid = dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert pid > 0, "no second worker was launched without the liveness read"
    finally:
        _stop(worker)


def test_a_pid_only_liveness_read_launches_the_session_walk_case(
    queued_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that ignores the session id admits the reparented worker's run."""
    if not hasattr(dispatch_launch_module, "record_process_alive"):
        pytest.skip("this revision has no liveness read to stub")
    _write_worker_receipt(queued_run, 1, _dead_pid())
    stand_in = _live_child(SESSION_ID)
    try:
        monkeypatch.setattr(
            dispatch_launch_module,
            "record_process_alive",
            lambda record, *args, **kwargs: runs.process_alive(record.get("pid")),
        )
        pid = dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert pid > 0, "the pid-only read refused a launch it should admit"
    finally:
        _stop(stand_in)
