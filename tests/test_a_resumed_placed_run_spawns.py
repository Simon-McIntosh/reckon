"""A resumed placed run spawns once its previous attempt has ended.

A placed run's liveness cannot be read from the scheduler on the fleet's
``srun`` placement. A resume retires the canonical worker record into
``attempt-N-worker.json`` before the spawn guard runs, so the guard finds
attempt receipts and no canonical worker record. Refusing that reading refuses
every resume of a placed run, even when the previous attempt's own receipts show
it ended and its worker is gone.

While a canonical worker record exists the guard answers as before. When none
exists it reads the newest attempt receipts: an exit receipt, or a worker
receipt whose pid this host probes dead, is the replaced attempt shown to have
ended.

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

RUN_ID = "r-resumed-placed"
JOB_ID = "1291586"
PLACEMENT = {"partition": "betelgeuse", "nodes": 1}

SPAWN_TAIL = (
    "a launch that started now would run a second worker beside it; "
    "observe or stop it before launching again"
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
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


@pytest.fixture()
def placed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one placed run mid-resume."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    # The fleet's placement carries no probe the scheduler can answer.
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    runs._write_json(runs.pointer_path(RUN_ID), _pointer())
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(**extra: object) -> dict:
    """The run's pointer as a resume leaves it: placed, no canonical worker."""
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "launcher_host": _reading_host(),
        "attempt": 2,
        "attempt_kind": "resume",
        "session": "session-1",
        "worktree": str(runs.run_dir(RUN_ID)),
        "manifest_path": str(runs.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs.run_dir(RUN_ID) / "stream.jsonl"),
        "stderr_path": str(runs.run_dir(RUN_ID) / "stderr.log"),
        "placement": dict(PLACEMENT),
        "job_id": JOB_ID,
        "node": {"id": "node-resumed-placed", "plan": "fixture"},
        **extra,
    }


def _rewrite_pointer(**fields: object) -> None:
    """Replace fields of the pointer this case's launch reads."""
    pointer = json.loads(runs.pointer_path(RUN_ID).read_text(encoding="utf-8"))
    pointer.update(fields)
    runs._write_json(runs.pointer_path(RUN_ID), pointer)


def _spec(directory: Path, attempt: int = 2) -> dict:
    """The supervisor's spec for the resumed attempt of the fixture run."""
    return {
        "run_id": RUN_ID,
        "run_directory": str(directory),
        "attempt": attempt,
        "attempt_kind": "resume",
        "attempt_started_at": "2026-11-08T17:40:00Z",
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


def _write_attempt_marker(directory: Path, attempt: int) -> None:
    """Publish the attempt a resumed launch is about to start."""
    runs._write_json(
        directory / "attempt.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "attempt_kind": "resume",
            "attempt_started_at": "2026-11-08T17:40:00Z",
        },
    )


def _write_retired_worker(directory: Path, attempt: int, pid: int) -> None:
    """File the worker receipt a retired attempt left behind."""
    runs._write_json(
        directory / f"attempt-{attempt}-worker.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "pid": pid,
            "launched_at": "2026-11-08T16:00:00Z",
            "backend": "probe",
            "argv": [],
        },
    )


def _write_retired_exit(directory: Path, attempt: int, exit_code: int = 0) -> None:
    """File the exit receipt a retired attempt left behind."""
    runs._write_json(
        directory / f"attempt-{attempt}-exit.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "exit_code": exit_code,
            "detail": "finished during working",
        },
    )


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


def _prepare(directory: Path, attempt: int) -> None:
    """Drive the first launch guard for the attempt about to start."""
    dispatch_launch_module._prepare_attempt_records(
        directory,
        run_id=RUN_ID,
        attempt=attempt,
        attempt_kind="resume",
        attempt_started_at="2026-11-08T18:00:00Z",
    )


def _recorded_worker(directory: Path) -> dict:
    """The worker record the launch wrote for the attempt it started."""
    return json.loads((directory / "worker.json").read_text(encoding="utf-8"))


def _spawn_and_stop(directory: Path, attempt: int = 2) -> int:
    """Drive the spawn seam and stop the worker it started."""
    pid = dispatch_launch_module._supervisor_spawn_worker(_spec(directory, attempt))
    try:
        return pid
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def test_a_resumed_run_whose_previous_attempt_ended_spawns(
    placed_run: Path,
) -> None:
    """An exit receipt for the newest attempt shows it ended."""
    _write_attempt_marker(placed_run, 2)
    _write_retired_worker(placed_run, 1, _dead_pid())
    _write_retired_exit(placed_run, 1)
    _rewrite_pointer(pid=_dead_pid())

    pid = _spawn_and_stop(placed_run)

    assert pid > 0
    assert _recorded_worker(placed_run)["pid"] == pid, (
        "the launched attempt did not record its worker"
    )


def test_a_resumed_run_whose_previous_worker_is_probed_dead_spawns(
    placed_run: Path,
) -> None:
    """A worker receipt with no exit receipt still shows a worker that is gone."""
    _write_attempt_marker(placed_run, 2)
    _write_retired_worker(placed_run, 1, _dead_pid())
    _rewrite_pointer(pid=_dead_pid())

    pid = _spawn_and_stop(placed_run)

    assert pid > 0
    assert _recorded_worker(placed_run)["pid"] == pid


def test_a_resumed_run_whose_previous_worker_is_alive_is_refused(
    placed_run: Path,
) -> None:
    """A retired worker receipt naming a live pid is still a holder."""
    worker = _live_child()
    try:
        _write_attempt_marker(placed_run, 2)
        _write_retired_worker(placed_run, 1, worker.pid)
        _rewrite_pointer(pid=_dead_pid())
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._supervisor_spawn_worker(_spec(placed_run, 2))
        assert str(refused.value) == (
            f"run {RUN_ID!r} cannot read whether its worker is live "
            f"(placed on job {JOB_ID}, liveness unknown): {SPAWN_TAIL}"
        )
        assert not (placed_run / "worker.json").exists(), (
            "the refused launch recorded a worker"
        )
    finally:
        _stop(worker)


def test_a_never_launched_placed_run_still_spawns(placed_run: Path) -> None:
    """No pointer pid, worker record or attempt receipt is a fresh launch."""
    _prepare(placed_run, 1)
    # The launcher publishes the supervisor's own pid before the spawn seam;
    # it is dropped from the read because it is not a worker.
    _rewrite_pointer(pid=os.getpid())

    pid = _spawn_and_stop(placed_run, 1)

    assert pid > 0
    assert _recorded_worker(placed_run)["pid"] == pid


def test_a_further_resume_after_a_refused_spawn_passes_the_first_guard(
    placed_run: Path,
) -> None:
    """An attempt refused at spawn leaves only its exit receipt behind."""
    _write_attempt_marker(placed_run, 2)
    _write_retired_exit(placed_run, 2, exit_code=1)
    _rewrite_pointer(pid=_dead_pid(), attempt=3)

    dispatch_launch_module._prepare_attempt_records(
        placed_run,
        run_id=RUN_ID,
        attempt=3,
        attempt_kind="resume",
        attempt_started_at="2026-11-08T18:00:00Z",
    )
    marker = json.loads((placed_run / "attempt.json").read_text(encoding="utf-8"))
    assert marker["attempt"] == 3

    pid = _spawn_and_stop(placed_run, 3)
    assert pid > 0
    assert _recorded_worker(placed_run)["pid"] == pid
