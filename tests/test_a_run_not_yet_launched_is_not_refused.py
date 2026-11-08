"""A placed run that never launched is not refused as if a worker held it.

A direct dispatch on the fleet writes its ``srun`` placement onto the pointer
before its first launch. The scheduler cannot be asked about that placement, so
the liveness read answers placed with liveness unknown. Refusing every such
reading refuses the run's own first launch, and the refusal removes the run.

Only a record that holds a recorded worker nothing has shown to be gone is a
reason to refuse. A record with no pointer pid, no worker record and no attempt
receipt has never launched, and one whose worker record names a pid this host
probes dead names a worker that is gone. Both proceed.

Every case works against a temporary crew home and run directory, and asserts
afterwards that the operator's own crew home is untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import process_liveness, runs
from reckon.crew.node import CrewError
from reckon.crew.recovery_liveness import _reading_host

RUN_ID = "r-run-not-yet-launched"
JOB_ID = "1291586"
PLACEMENT = {"partition": "betelgeuse", "nodes": 1}

PREPARE_TAIL = (
    "a launch that started now would retire its worker record, so the run "
    "would read as halted while its worker kept working; observe or stop it "
    "before launching again"
)


def _listing(directory: Path) -> list[str] | None:
    """The names directly under a directory, or None when it does not exist."""
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs.crew_home()
    listing_of_the_real_home_before = _listing(real)
    yield
    assert _listing(real) == listing_of_the_real_home_before, (
        "the case wrote into the real crew home"
    )


@pytest.fixture()
def placed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one never-launched placed run."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    # The fleet's placement carries no probe the scheduler can answer, which is
    # the reading that refused every first launch.
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    runs._write_json(runs.pointer_path(RUN_ID), _pointer())
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(**extra: object) -> dict:
    """The run's pointer as a direct dispatch leaves it before its first launch."""
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "launcher_host": _reading_host(),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "session": "session-1",
        "worktree": str(runs.run_dir(RUN_ID)),
        "manifest_path": str(runs.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs.run_dir(RUN_ID) / "stream.jsonl"),
        "stderr_path": str(runs.run_dir(RUN_ID) / "stderr.log"),
        "placement": dict(PLACEMENT),
        "job_id": JOB_ID,
        "node": {"id": "node-not-yet-launched", "plan": "fixture"},
        **extra,
    }


def _rewrite_pointer(**fields: object) -> None:
    """Replace fields of the pointer this case's launch reads."""
    pointer = json.loads(runs.pointer_path(RUN_ID).read_text(encoding="utf-8"))
    pointer.update(fields)
    runs._write_json(runs.pointer_path(RUN_ID), pointer)


def _spec(directory: Path, attempt: int = 1) -> dict:
    """The supervisor's spec for a first attempt of the fixture run."""
    return {
        "run_id": RUN_ID,
        "run_directory": str(directory),
        "attempt": attempt,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-11-08T00:00:00Z",
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


def _write_worker_record(directory: Path, pid: int) -> dict:
    """File a worker record naming the pid given, as a supervisor wrote it."""
    record = {
        "run_id": RUN_ID,
        "attempt": 1,
        "pid": pid,
        "launched_at": "2026-11-08T00:00:00Z",
        "backend": "probe",
        "argv": [],
    }
    runs._write_json(directory / "worker.json", record)
    return record


def _live_child() -> subprocess.Popen:
    """A process that stays alive until the case stops it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _dead_pid() -> int:
    """A pid whose process has exited and been collected."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _prepare(directory: Path, attempt: int = 1) -> None:
    dispatch_launch_module._prepare_attempt_records(
        directory,
        run_id=RUN_ID,
        attempt=attempt,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-08T00:00:00Z",
    )


def test_a_never_launched_placed_run_proceeds(placed_run: Path) -> None:
    """No pid, no worker record and no receipt is a run that never launched."""
    _prepare(placed_run)
    attempt = json.loads((placed_run / "attempt.json").read_text(encoding="utf-8"))
    assert attempt["attempt"] == 1
    assert attempt["attempt_kind"] == "dispatch"


def test_a_placed_run_whose_worker_is_probed_dead_proceeds(placed_run: Path) -> None:
    """The worker record names the worker itself, and this host sees it gone."""
    _write_worker_record(placed_run, _dead_pid())
    _prepare(placed_run, attempt=2)
    attempt = json.loads((placed_run / "attempt.json").read_text(encoding="utf-8"))
    assert attempt["attempt"] == 2


def test_a_placed_run_whose_worker_is_alive_is_refused(placed_run: Path) -> None:
    """A live worker recorded for itself is still refused, named by its pid."""
    worker = _live_child()
    try:
        _write_worker_record(placed_run, worker.pid)
        with pytest.raises(CrewError) as refused:
            _prepare(placed_run, attempt=2)
        assert str(refused.value) == (
            f"run {RUN_ID!r} still has a live worker (pid {worker.pid}): {PREPARE_TAIL}"
        )
        assert not (placed_run / "attempt.json").exists(), (
            "the refused launch wrote its attempt record"
        )
    finally:
        _stop(worker)


def test_a_placed_run_with_a_recorded_pid_and_no_worker_record_is_refused(
    placed_run: Path,
) -> None:
    """A pid nobody can probe is not evidence the worker is gone."""
    _rewrite_pointer(pid=os.getpid())
    with pytest.raises(CrewError) as refused:
        _prepare(placed_run)
    assert str(refused.value) == (
        f"run {RUN_ID!r} cannot read whether its worker is live "
        f"(placed on job {JOB_ID}, liveness unknown): {PREPARE_TAIL}"
    )
    assert not (placed_run / "attempt.json").exists(), (
        "the refused launch wrote its attempt record"
    )


def test_a_never_launched_placed_run_passes_the_supervisor_guard(
    placed_run: Path,
) -> None:
    """The same run passes the spawn seam and records the worker it started."""
    _prepare(placed_run)
    # The launcher publishes the supervisor's own pid before the spawn seam
    # runs; it is dropped from the read because it is not a worker.
    _rewrite_pointer(pid=os.getpid())
    pid = dispatch_launch_module._supervisor_spawn_worker(_spec(placed_run))
    try:
        assert pid > 0
        record = json.loads((placed_run / "worker.json").read_text(encoding="utf-8"))
        assert record["pid"] == pid, "the launched attempt did not record its worker"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
