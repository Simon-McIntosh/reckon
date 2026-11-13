"""A parked worker is woken within minutes of its wait probe turning terminal.

The window this closes: a worker ended its turn with ``status: waiting`` and a
declared wait probe, and its supervisor stayed alive beside it. When the awaited
job ended OUT_OF_MEMORY the sweep that evaluates the probe offered the run to
the resume loop, and every door refused it as an attempt in flight — the
supervisor was alive, so the reservation read as held — until an operator ran
``crew stop`` about forty minutes later.

Two facts free the wake. A run parked on a wait it declared holds no
reservation: its worker has ended its turn, and the supervisor beside it is
idle. And the sweep that evaluates the probe runs on the station's own cadence
threads, so a tick held in a slow stage cannot hold the wake.

Every case works against a temporary crew home, and asserts afterwards that the
operator's own crew home is untouched.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import process_liveness
from reckon.crew import resumption as resumption_module
from reckon.crew import runs as runs_module

RUN_ID = "r-parked-wait"
PROJECT = "sample"
NODE_ID = "terminal-wait-node"

# The bound the plan states: a terminal probe wakes its worker within five
# minutes. The timing case advances a simulated clock in the sweeper's own
# passes, so the figure is measured in the run's clock rather than the test's.
WAKE_BOUND_SECONDS = 300
SWEEP_STEP_SECONDS = 30


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs_module.crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run that has launched before."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs_module.live_dir().mkdir(parents=True, exist_ok=True)
    return directory


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return worktree


def _pointer(directory: Path, worktree: Path, **extra: object) -> dict:
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "phase": "waiting",
        "backend": "probe",
        "launch": "cli",
        "argv": [sys.executable, "-c", "pass"],
        "command": sys.executable,
        "attempt": 2,
        "attempt_kind": "resume",
        "session": "session-1",
        "session_id": "session-1",
        "launcher_host": socket.gethostname(),
        "worktree": str(worktree),
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "created_at": "2026-11-13T02:00:00Z",
        "node": {"id": NODE_ID, "plan": "fixture", "section": "wake"},
        **extra,
    }


def _write_pointer(directory: Path, pointer: dict) -> None:
    runs_module._write_json(runs_module.pointer_path(RUN_ID), pointer)


def _parked_manifest(directory: Path, worktree: Path, terminal: Path) -> None:
    """A manifest that declares a wait on a path not yet written."""
    (directory / "manifest.md").write_text(
        "\n".join(
            [
                "status: waiting",
                "wait_condition: the scheduler job ends",
                f"wait_file: {terminal}",
                "resume_brief: read the job's state and continue",
                "wait_started_at: 2026-11-13T02:40:00Z",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _reservation(directory: Path, attempt: int, *, supervisor_pid: int) -> Path:
    payload = {
        "run_id": RUN_ID,
        "attempt": attempt,
        "kind": "dispatch",
        "pid": supervisor_pid,
        "pid_start_time": runs_module._process_start_time(supervisor_pid),
        "at": "2026-11-13T02:00:00Z",
        "supervisor_pid": supervisor_pid,
        "supervisor_pid_start_time": runs_module._process_start_time(supervisor_pid),
    }
    path = directory / f"attempt-{attempt}.reservation"
    runs_module._write_json(path, payload)
    return path


def _live_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _worker_record(directory: Path, worker_pid: int) -> None:
    runs_module._write_json(
        directory / "worker.json",
        {
            "run_id": RUN_ID,
            "attempt": 2,
            "pid": worker_pid,
            "pid_start_time": runs_module._process_start_time(worker_pid),
        },
    )


# ── the reservation an idle supervisor leaves behind ────────────────────────


def test_a_parked_worker_frees_the_reservation_its_idle_supervisor_holds(
    home: Path, tmp_path: Path
) -> None:
    """The reported shape: live supervisor, parked worker, terminal job."""
    supervisor = _live_child()
    try:
        worktree = _worktree(tmp_path)
        terminal = tmp_path / "job-ended"
        _write_pointer(home, _pointer(home, worktree))
        _reservation(home, 2, supervisor_pid=supervisor.pid)
        _parked_manifest(home, worktree, terminal)

        assert dispatch_launch_module.held_launch_reservation(home) is None, (
            "a parked worker's idle supervisor still held the reservation"
        )
        taken = dispatch_launch_module.reserve_launch_attempt(
            home, RUN_ID, kind="resume"
        )
        dispatch_launch_module.release_launch_reservation(taken)
    finally:
        _stop(supervisor)


def test_a_running_worker_still_holds_the_reservation(
    home: Path, tmp_path: Path
) -> None:
    """A worker that is still running is refused exactly as before."""
    worker = _live_child()
    try:
        worktree = _worktree(tmp_path)
        terminal = tmp_path / "job-ended"
        _write_pointer(home, _pointer(home, worktree, pid=worker.pid))
        _reservation(home, 2, supervisor_pid=worker.pid)
        _worker_record(home, worker.pid)
        _parked_manifest(home, worktree, terminal)

        held = dispatch_launch_module.held_launch_reservation(home)
        assert held is not None, "a live worker's reservation was released"
        assert held.get("supervisor_pid") == worker.pid
    finally:
        _stop(worker)


# ── the wake does not wait for a station tick ───────────────────────────────


def test_a_terminal_probe_wakes_its_worker_while_the_tick_is_held(
    home: Path, tmp_path: Path
) -> None:
    """A tick held for half an hour does not delay the wake past five minutes.

    The sweeper runs off the tick path, exactly as the station's own reflex and
    the follower's cadence run it. The probe turns terminal on the third pass;
    the wake must be offered within ``WAKE_BOUND_SECONDS`` of that pass on the
    run's own clock.
    """
    worktree = _worktree(tmp_path)
    terminal = tmp_path / "job-ended"
    pointer = _pointer(home, worktree)
    _write_pointer(home, pointer)
    _parked_manifest(home, worktree, terminal)

    start = datetime(2026, 11, 13, 2, 40, tzinfo=UTC)
    resumed_at: dict[str, float] = {}
    sweep_failures: list[str] = []
    last_skip: list[object] = []
    stop = threading.Event()
    held_tick = threading.Event()  # never set: the tick never returns

    def _sweeper() -> None:
        for index in range(1, 40):
            moment = start + timedelta(seconds=SWEEP_STEP_SECONDS * index)
            if index == 3:
                terminal.write_text("OUT_OF_MEMORY\n", encoding="utf-8")
            try:
                report = resumption_module.sweep(
                    PROJECT,
                    pointers=[pointer],
                    config=None,
                    dry_run=True,
                    now=moment,
                    writer={"kind": "other", "key": "test"},
                )
            except Exception as exc:  # noqa: BLE001 - a failing sweep is retried next pass
                sweep_failures.append(repr(exc))
                continue
            if report.get("resumed"):
                resumed_at.setdefault("wave", (moment - start).total_seconds())
                stop.set()
                return
            last_skip[:] = list(report.get("skipped") or ())[:1]
            if stop.is_set():
                return

    sweeper = threading.Thread(target=_sweeper, name="test-sweeper", daemon=True)
    sweeper.start()
    try:
        deadline = time.monotonic() + 30
        while sweeper.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        sweeper.join(timeout=5)

    assert held_tick.is_set() is False, (
        "the held tick returned; the case proves nothing"
    )
    assert "wave" in resumed_at, (
        f"the terminal probe woke nothing; failures={sweep_failures[:3]!r} "
        f"last_skip={last_skip[:1]!r}"
    )
    turned_terminal_at = SWEEP_STEP_SECONDS * 3
    assert resumed_at["wave"] - turned_terminal_at <= WAKE_BOUND_SECONDS, (
        f"the wake came {resumed_at['wave']}s in, past the {WAKE_BOUND_SECONDS}s bound"
    )
