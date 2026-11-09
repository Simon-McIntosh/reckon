"""A launch refuses beside a worker issued on another host until it is shown ended.

A pid means nothing on a machine that did not issue it, so the liveness read
answers ``unknown`` at once for a run whose pointer names a different
``launcher_host``. The launch guard used to treat that honest unknown as "there
is no worker", readmitting a record that may hold a live remote worker and
launching a second one beside it.

Off-host only an exit receipt, written when the worker exits and carried on the
shared run directory, shows the launch ended. Nothing this host can probe
answers for a pid it did not issue, and age never frees a launch.

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

RUN_ID = "r-foreign-launch"
#: A host that is never the one running this case.
FOREIGN_HOST = f"{_reading_host()}-elsewhere"
#: The consequence every spawn-guard refusal carries.
SPAWN_TAIL = (
    "a launch that started now would run a second worker beside it; "
    "observe or stop it before launching again"
)
#: What a refused foreign launch tells the reader to do instead.
FOREIGN_REMEDY = f"launch from {FOREIGN_HOST}, or stop the run there"


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
def run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run's directory and pointer."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    # The fleet's placement carries no probe the scheduler can answer.
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(**extra: object) -> dict:
    """A run's pointer, launched on whatever host the case names."""
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "launcher_host": FOREIGN_HOST,
        "attempt": 2,
        "attempt_kind": "resume",
        "session": "session-1",
        "worktree": str(runs.run_dir(RUN_ID)),
        "manifest_path": str(runs.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs.run_dir(RUN_ID) / "stream.jsonl"),
        "stderr_path": str(runs.run_dir(RUN_ID) / "stderr.log"),
        "node": {"id": "node-foreign-launch", "plan": "fixture"},
        **extra,
    }


def _write_pointer(**extra: object) -> None:
    """Publish the pointer this case's launch reads."""
    runs._write_json(runs.pointer_path(RUN_ID), _pointer(**extra))


def _spec(directory: Path, attempt: int = 2) -> dict:
    """The supervisor's spec for the attempt this case launches."""
    return {
        "run_id": RUN_ID,
        "run_directory": str(directory),
        "attempt": attempt,
        "attempt_kind": "resume",
        "attempt_started_at": "2026-11-09T16:00:00Z",
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


def _write_canonical_worker(directory: Path, pid: int, attempt: int = 1) -> None:
    """File the worker record a launch leaves in front of its run directory."""
    runs._write_json(
        directory / "worker.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "pid": pid,
            "pid_start_time": None,
            "launched_at": "2026-11-09T15:00:00Z",
            "backend": "probe",
            "argv": [],
        },
    )


def _write_canonical_exit(directory: Path, attempt: int = 1) -> None:
    """File the exit receipt the supervisor writes when its worker exits."""
    runs._write_json(
        directory / "exit.json",
        {
            "run_id": RUN_ID,
            "attempt": 1,
            "exit_code": 0,
            "detail": "finished during working",
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
            "launched_at": "2026-11-09T15:00:00Z",
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


def _recorded_worker(directory: Path) -> dict:
    """The worker record the launch wrote for the attempt it started."""
    return json.loads((directory / "worker.json").read_text(encoding="utf-8"))


def _write_attempt_marker(directory: Path, attempt: int) -> None:
    """Publish the attempt the launcher is about to start.

    The launcher writes this before its supervisor runs, and the supervisor
    exposes the canonical worker record only while it names the current
    attempt.
    """
    runs._write_json(
        directory / "attempt.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "attempt_kind": "resume",
            "attempt_started_at": "2026-11-09T16:00:00Z",
        },
    )


def _spawn_and_stop(directory: Path, attempt: int = 2) -> int:
    """Drive the spawn seam and stop the worker it started."""
    _write_attempt_marker(directory, attempt)
    pid = dispatch_launch_module._supervisor_spawn_worker(_spec(directory, attempt))
    try:
        return pid
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def _refused(directory: Path, attempt: int = 2) -> str:
    """Drive the spawn seam and return the refusal text it raised."""
    with pytest.raises(CrewError) as refused:
        dispatch_launch_module._supervisor_spawn_worker(_spec(directory, attempt))
    return str(refused.value)


# ── Off-host shapes: (a) refused without an exit receipt, (b) proceed with one ──


def test_a_canonical_worker_on_another_host_refuses_a_relaunch(
    run_directory: Path,
) -> None:
    """(a1) A foreign canonical worker with no exit receipt is still a holder.

    This host cannot probe a pid the other machine issued, so the absence of an
    exit receipt is the only fact that matters: nothing has shown the worker
    ended, and a launch now could run a second worker beside it.
    """
    worker_pid = _dead_pid()
    pointer_pid = _dead_pid()
    _write_canonical_worker(run_directory, worker_pid)
    _write_pointer(pid=pointer_pid)

    text = _refused(run_directory)

    assert text == (
        f"run {RUN_ID!r} cannot read whether its worker is live "
        f"(launched on host {FOREIGN_HOST}, pid {pointer_pid}): "
        f"{SPAWN_TAIL}; {FOREIGN_REMEDY}"
    ), "the refusal did not name the recording host and pid"
    assert _recorded_worker(run_directory)["pid"] == worker_pid, (
        "the refused launch rewrote the worker record"
    )


def test_a_retired_worker_on_another_host_refuses_a_relaunch(
    run_directory: Path,
) -> None:
    """(a2) A foreign retired worker whose pid is dead here is still a holder.

    The dead number is a fact about this host's process table, not about the
    machine that issued it. Without an exit receipt the launch is unshown to
    have ended.
    """
    pointer_pid = _dead_pid()
    _write_retired_worker(run_directory, 1, _dead_pid())
    _write_pointer(pid=pointer_pid)

    text = _refused(run_directory)

    assert text == (
        f"run {RUN_ID!r} cannot read whether its worker is live "
        f"(launched on host {FOREIGN_HOST}, pid {pointer_pid}): "
        f"{SPAWN_TAIL}; {FOREIGN_REMEDY}"
    ), "the refusal did not name the recording host and pid"
    assert not (run_directory / "worker.json").exists(), (
        "the refused launch recorded a worker for itself"
    )


def _truncate(path: Path, text: str) -> None:
    """Leave a half-written receipt where a complete one would be filed."""
    path.write_text(text, encoding="utf-8")


def test_a_truncated_exit_receipt_on_another_host_still_refuses(
    run_directory: Path,
) -> None:
    """(a1) A half-written exit record is absent, not proof the worker ended.

    A receipt truncated mid-write, or one naming another run, is not evidence
    that the launch ended; a file on disk is not a receipt. The refusal stands
    exactly as it does with no receipt at all.
    """
    pointer_pid = _dead_pid()
    _write_canonical_worker(run_directory, _dead_pid())
    _truncate(run_directory / "exit.json", f'{{"run_id": "{RUN_ID}", "exit_c')
    _write_pointer(pid=pointer_pid)

    text = _refused(run_directory)

    assert text == (
        f"run {RUN_ID!r} cannot read whether its worker is live "
        f"(launched on host {FOREIGN_HOST}, pid {pointer_pid}): "
        f"{SPAWN_TAIL}; {FOREIGN_REMEDY}"
    ), "a truncated exit receipt was read as the worker's end"


def test_a_truncated_retired_exit_receipt_on_another_host_still_refuses(
    run_directory: Path,
) -> None:
    """(a2) A truncated retired exit receipt is read by the same rules."""
    pointer_pid = _dead_pid()
    _write_retired_worker(run_directory, 1, _dead_pid())
    _truncate(
        run_directory / "attempt-1-exit.json",
        f'{{"run_id": "{RUN_ID}", "exit_c',
    )
    _write_pointer(pid=pointer_pid)

    text = _refused(run_directory)

    assert text == (
        f"run {RUN_ID!r} cannot read whether its worker is live "
        f"(launched on host {FOREIGN_HOST}, pid {pointer_pid}): "
        f"{SPAWN_TAIL}; {FOREIGN_REMEDY}"
    ), "a truncated retired exit receipt was read as the worker's end"


def test_an_exit_receipt_for_another_run_on_another_host_still_refuses(
    run_directory: Path,
) -> None:
    """(b1) A receipt naming a different run is absent, as the owner reads it."""
    pointer_pid = _dead_pid()
    _write_canonical_worker(run_directory, _dead_pid())
    _truncate(
        run_directory / "exit.json",
        json.dumps({"run_id": "r-some-other-run", "exit_code": 0}),
    )
    _write_pointer(pid=pointer_pid)

    text = _refused(run_directory)

    assert text == (
        f"run {RUN_ID!r} cannot read whether its worker is live "
        f"(launched on host {FOREIGN_HOST}, pid {pointer_pid}): "
        f"{SPAWN_TAIL}; {FOREIGN_REMEDY}"
    ), "another run's exit receipt was read as this run's end"


def test_a_canonical_worker_on_another_host_with_its_exit_spawns(
    run_directory: Path,
) -> None:
    """(b1) A canonical exit receipt beside a foreign worker shows it ended."""
    _write_canonical_worker(run_directory, _dead_pid())
    _write_canonical_exit(run_directory)
    _write_pointer(pid=_dead_pid())

    pid = _spawn_and_stop(run_directory)

    assert pid > 0
    assert _recorded_worker(run_directory)["pid"] == pid


def test_a_retired_worker_on_another_host_with_its_exit_spawns(
    run_directory: Path,
) -> None:
    """(b2) A retired exit receipt shows the foreign attempt ended."""
    _write_retired_worker(run_directory, 1, _dead_pid())
    _write_retired_exit(run_directory, 1)
    _write_pointer(pid=_dead_pid())

    pid = _spawn_and_stop(run_directory)

    assert pid > 0
    assert _recorded_worker(run_directory)["pid"] == pid


def test_a_run_on_another_host_that_never_launched_spawns(
    run_directory: Path,
) -> None:
    """(c) No pointer pid, worker record or attempt receipt is a fresh launch."""
    _write_pointer()

    pid = _spawn_and_stop(run_directory, 1)

    assert pid > 0
    assert _recorded_worker(run_directory)["pid"] == pid


# ── (d) The same shapes on this host read exactly as they did before ──────────


def test_a_live_worker_on_this_host_still_refuses_a_relaunch(
    run_directory: Path,
) -> None:
    """(d) A local worker probed alive refuses, and the refusal names its pid.

    The positive control for every case above: the guard still reads this
    host's process table where the pid was issued here.
    """
    worker = _live_child()
    try:
        _write_canonical_worker(run_directory, worker.pid)
        _write_pointer(launcher_host=_reading_host(), pid=_dead_pid())

        text = _refused(run_directory)

        assert text == (
            f"run {RUN_ID!r} still has a live worker (pid {worker.pid}): {SPAWN_TAIL}"
        )
        assert FOREIGN_REMEDY not in text, (
            "a locally launched run was told to launch from another host"
        )
    finally:
        _stop(worker)


def test_a_dead_worker_on_this_host_still_spawns(run_directory: Path) -> None:
    """(d) A local worker record naming a pid this host probes dead proceeds."""
    _write_canonical_worker(run_directory, _dead_pid())
    _write_pointer(launcher_host=_reading_host(), pid=_dead_pid())

    pid = _spawn_and_stop(run_directory)

    assert pid > 0
    assert _recorded_worker(run_directory)["pid"] == pid


def test_a_retired_worker_on_this_host_with_a_dead_pid_still_spawns(
    run_directory: Path,
) -> None:
    """(d) A local retired worker receipt answering dead to this host proceeds."""
    _write_retired_worker(run_directory, 1, _dead_pid())
    _write_pointer(launcher_host=_reading_host(), pid=_dead_pid())

    pid = _spawn_and_stop(run_directory)

    assert pid > 0
    assert _recorded_worker(run_directory)["pid"] == pid


def test_a_retired_exit_on_this_host_still_spawns(run_directory: Path) -> None:
    """(d) A local retired exit receipt still shows the attempt ended."""
    _write_retired_worker(run_directory, 1, _dead_pid())
    _write_retired_exit(run_directory, 1)
    _write_pointer(launcher_host=_reading_host(), pid=_dead_pid())

    pid = _spawn_and_stop(run_directory)

    assert pid > 0
    assert _recorded_worker(run_directory)["pid"] == pid
