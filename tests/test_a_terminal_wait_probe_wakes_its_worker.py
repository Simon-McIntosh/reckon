"""A parked worker is woken within minutes of its wait probe turning terminal.

The shape this closes: a worker ended its turn with ``status: waiting`` and a
declared wait probe, and its supervisor stayed alive beside it. The sweep that
evaluates the probe offered the run to the resume loop, and every door refused
it as an attempt in flight — the supervisor was alive, so the reservation read
as held — so the probe turning terminal woke nothing for as long as that
supervisor lived.

Three facts free the wake. A run parked on a wait it declared holds no
reservation: its worker has ended its turn and the supervisor beside it is
idle. A reservation that names a later attempt than the run's worker record is
still held, because the worker record has not yet caught up with the resume
that took it. And the sweep that evaluates the probe runs on the station's own
threads, so a tick held in a slow stage cannot hold the wake.

Every case works against a temporary crew home and a temporary station store,
and asserts afterwards that the operator's own crew home is untouched.
"""

from __future__ import annotations

import itertools
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
from reckon.crew import process_liveness, station, station_obligations, station_store
from reckon.crew import resumption as resumption_module
from reckon.crew import runs as runs_module
from reckon.crew.run_paths import crew_home

RUN_ID = "r-parked-wait"
PROJECT = "sample"
SESSION = "session-1"
NODE_ID = "terminal-wait-node"

# The bound the plan states: a terminal probe wakes its worker within five
# minutes. The station's clock is injected, so the figure is measured on the
# run's own clock rather than on this machine's wall clock.
WAKE_BOUND_SECONDS = 300
# How long the tick is held, on the station's own clock. The case advances the
# run clock that far while the tick is blocked, so the hold it proves is the
# half hour the plan describes rather than a few milliseconds of this case.
HOLD_SECONDS = 1800
SWEEP_BEAT_SECONDS = 120.0
# Virtual seconds each reflex arm advances the station's clock, and the real
# interval between arms. The ratio sets how many virtual minutes pass per
# second of this case's own running time.
CLOCK_STEP_SECONDS = 10.0
REFLEX_BEAT_SECONDS = 0.05


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
        "session": SESSION,
        "session_id": SESSION,
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


def _parked_manifest(directory: Path, terminal: Path) -> None:
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


def _dead_pid() -> tuple[int, float | None]:
    """A pid this host has already reaped, with the start time it was recorded with."""
    child = _live_child()
    start_time = runs_module._process_start_time(child.pid)
    _stop(child)
    return child.pid, start_time


def _worker_record(
    directory: Path, attempt: int, pid: int, start: float | None
) -> None:
    runs_module._write_json(
        directory / "worker.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "pid": pid,
            "pid_start_time": start,
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
        dead_pid, dead_start = _dead_pid()
        _write_pointer(home, _pointer(home, worktree))
        _reservation(home, 2, supervisor_pid=supervisor.pid)
        _worker_record(home, 2, dead_pid, dead_start)
        _parked_manifest(home, tmp_path / "job-ended")

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
        _write_pointer(home, _pointer(home, worktree, pid=worker.pid))
        _reservation(home, 2, supervisor_pid=worker.pid)
        _worker_record(home, 2, worker.pid, runs_module._process_start_time(worker.pid))
        _parked_manifest(home, tmp_path / "job-ended")

        held = dispatch_launch_module.held_launch_reservation(home)
        assert held is not None, "a live worker's reservation was released"
        assert held.get("supervisor_pid") == worker.pid
    finally:
        _stop(worker)


def test_a_resume_whose_worker_has_not_landed_is_still_held(
    home: Path, tmp_path: Path
) -> None:
    """The window between a resume being admitted and its worker record landing.

    The worker record still describes the attempt that parked, so a reading
    taken from the record alone would free the reservation the resume had just
    taken and admit a second launch beside a worker that is running nowhere on
    disk yet. The reservation names the attempt it took, which is what keeps it
    held.
    """
    supervisor = _live_child()
    try:
        worktree = _worktree(tmp_path)
        dead_pid, dead_start = _dead_pid()
        _write_pointer(home, _pointer(home, worktree))
        _worker_record(home, 2, dead_pid, dead_start)
        _parked_manifest(home, tmp_path / "job-ended")
        # The resume took attempt 3 and has not written its worker record yet.
        _reservation(home, 3, supervisor_pid=supervisor.pid)

        held = dispatch_launch_module.held_launch_reservation(home)
        assert held is not None, (
            "a resume that has not written its worker record read as parked"
        )
        with pytest.raises(dispatch_launch_module.CrewError):
            dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="resume")
    finally:
        _stop(supervisor)


# ── the wake does not wait for a station tick ───────────────────────────────


def test_a_terminal_probe_wakes_its_worker_while_the_tick_is_held(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick held for half an hour does not delay the wake past five minutes.

    The station runs through ``run_station``, its real entry point, with its
    own sweep thread and reflex thread. The tick is held inside its
    per-project refresh stage on an event this case never sets, and the
    station's clock is injected so the hold can be half an hour of the run's
    own time while this case runs for seconds. The wake is the resume the
    sweep offers, recorded in place of the launch it would compose.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    worktree = _worktree(tmp_path)
    terminal = tmp_path / "job-ended"
    pointer = _pointer(home, worktree)
    _write_pointer(home, pointer)
    _parked_manifest(home, terminal)
    dead_pid, dead_start = _dead_pid()
    _worker_record(home, 2, dead_pid, dead_start)

    start = datetime(2026, 11, 13, 2, 40, tzinfo=UTC)
    steps = itertools.count()
    clock_reads: list[datetime] = []

    def clock() -> datetime:
        moment = start + timedelta(seconds=CLOCK_STEP_SECONDS * next(steps))
        clock_reads.append(moment)
        return moment

    def now_of_run_clock() -> datetime:
        return start + timedelta(seconds=CLOCK_STEP_SECONDS * next(steps))

    sweep_failures: list[str] = []

    def recorded_sweep(*args: object, **kwargs: object) -> dict:
        """The station's own sweep, with any failure kept for the assertion."""
        try:
            return resumption_module.sweep(*args, **kwargs)
        except Exception as exc:
            sweep_failures.append(repr(exc))
            raise

    sweeps = station.SweepBeat(sweep=recorded_sweep, beat_seconds=SWEEP_BEAT_SECONDS)
    node = station.Station(
        station_store.StationStore(runtime),
        home=crew_home(),
        projects=dict,
        sweeps=sweeps,
        reflex_beat_seconds=REFLEX_BEAT_SECONDS,
        clock=clock,
    )

    gate = threading.Event()
    entered = threading.Event()
    released = threading.Event()
    held_from: list[datetime] = []

    def held_publish(*args: object, **kwargs: object) -> None:
        held_from.append(now_of_run_clock())
        entered.set()
        gate.wait(timeout=60)
        # Recorded after the block ends, so a tick that timed out or was let
        # go is visible to the wake assertion rather than merely suspected.
        released.set()

    monkeypatch.setattr(station_obligations, "publish", held_publish)

    woken: list[dict] = []
    resumed_clock: list[datetime] = []

    def recording_resume(run_id, record, *, config, launcher=None, advice="") -> dict:
        resumed_clock.append(now_of_run_clock())
        woken.append({"run_id": run_id, "advice": advice})
        return {"run_id": run_id, "resumed": True}

    monkeypatch.setattr(resumption_module, "_resume", recording_resume)

    def run_the_station() -> None:
        station.run_station(station_instance=node, store=node.store, once=True)

    ticker = threading.Thread(target=run_the_station, name="held-tick", daemon=True)
    ticker.start()
    try:
        assert entered.wait(timeout=20), "the tick never entered its refresh stage"
        # Advance the station's clock half an hour while the tick is blocked.
        # Each read is a read of the run's own clock, so the hold asserted
        # below is thirty minutes of the station's time, not of this case's.
        hold_deadline = time.monotonic() + 5
        while (
            now_of_run_clock() - held_from[0]
        ).total_seconds() < HOLD_SECONDS and time.monotonic() < hold_deadline:
            pass
        assert (now_of_run_clock() - held_from[0]).total_seconds() >= HOLD_SECONDS, (
            "the station clock did not reach the half hour the case holds the tick for"
        )
        # The probe turns terminal while the tick is held. The clock the case
        # reads here is the same one the station's own threads read.
        turned_terminal = now_of_run_clock()
        terminal.write_text("OUT_OF_MEMORY\n", encoding="utf-8")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not woken:
            time.sleep(0.01)
        assert ticker.is_alive() and not released.is_set(), (
            "the tick returned before the wake; the case proves nothing about a held tick"
        )
        assert woken, (
            "the terminal probe woke nothing while the tick was held; "
            f"the station clock reached {max(clock_reads[-5:], default=None)}"
        )
        woken_at = resumed_clock[0]
        delay = (woken_at - turned_terminal).total_seconds()
        assert 0 <= delay <= WAKE_BOUND_SECONDS, (
            f"the wake came {delay:.0f}s of the run's own clock after the probe "
            f"turned terminal, past the {WAKE_BOUND_SECONDS}s bound"
        )
        held_span = (woken_at - held_from[0]).total_seconds()
        assert held_span >= HOLD_SECONDS, (
            f"the tick was held for {held_span:.0f}s of the run's own clock when "
            f"the wake happened, short of the {HOLD_SECONDS}s the case holds it for"
        )
    finally:
        gate.set()
        node.reflex.stop()
        ticker.join(timeout=30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and sweeps._in_flight:
            time.sleep(0.01)

    assert not sweep_failures, f"a station sweep raised: {sweep_failures[:3]!r}"
