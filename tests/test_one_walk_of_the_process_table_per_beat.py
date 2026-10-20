"""One walk of the process table answers every run's environment mark in a beat.

The liveness read finds a run's reparented worker by the environment mark its
launcher wrote into it, and it is reached once per live run in a beat. Each
reach used to walk the whole of ``/proc`` again — listing the table, reading
every process's start tick and stack base and opening the environments that had
changed — so a producer with several runs needing the scan listed ``/proc`` once
per such run per beat. The walk now maps every pair it sees, a run mark and the
crew home that process's own environment places it in, to the pids carrying it,
and every run asked in that beat is answered from that map under its own run id
and crew home. A later beat walks again, so a worker that started after the walk
is found on the next beat rather than never.

The stop path is deliberately not a reader of that map: a map up to one beat old
must never select a process to signal, so the stop walks the table afresh at
signal time, keeping its start-tick floor and crew-home checks.

Every case starts a real, live child carrying a run id, and waits until its
environment is readable, so the instrument is shown to see a process known to be
present before any absence is believed. The declared negative control makes the
stop path read the beat's map instead of walking: a worker that started after
the beat's walk is then missed, and the stop that must signal it fails.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon.crew import process_signal as ps
from reckon.crew.process_signal import RUN_ID_ENV

NEGATIVE_CONTROL_MUTATION = (
    "a stop issued for a run whose worker started after the beat's walk finds "
    "and signals that worker"
)

# Far longer than any single read waits; every case reaps the child it starts.
_CHILD_SLEEP_SECONDS = 300.0
_MARK_WAIT_SECONDS = 10.0


def _unique_run_id() -> str:
    """A run id no other process on this machine carries."""
    return f"r-onebeat-{os.getpid()}-{os.urandom(6).hex()}"


RECKON_HOME_ENV = "RECKON_HOME"


def _child_env(run_id: str, home: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(RECKON_HOME_ENV, None)
    env[RUN_ID_ENV] = run_id
    env[RECKON_HOME_ENV] = home
    return env


def _start_child(run_id: str, home: Path) -> subprocess.Popen:
    """A live child carrying ``run_id`` and ``RECKON_HOME`` as its own home."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECONDS})"],
        env=_child_env(run_id, str(home)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(child: subprocess.Popen) -> None:
    """Stop a child and wait for it, escalating only if it will not end."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def _wait_for_environ_mark(pid: int, run_id: str) -> None:
    """Wait until the child's own environment is readable and carries the mark.

    The positive control the beat assertions rest on: a child spawned but not
    yet executed has no readable environment, so a map that does not name it
    proves nothing. This reads ``/proc`` directly, so it does not advance or
    read the beat's map.
    """
    needle = f"{RUN_ID_ENV}={run_id}".encode()
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            if needle in (Path("/proc") / str(pid) / "environ").read_bytes():
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} never showed the mark for {run_id!r}")


def _crew_home_of(home: Path) -> str:
    """The crew home ``_processes_owning_run`` is asked about for ``home``."""
    return str(home / "crew")


@pytest.fixture(autouse=True)
def empty_state() -> Iterator[None]:
    """Each case starts and ends with no beat map and no environment index."""
    ps._MARKED_PROCESSES_BEAT.clear()
    ps._ENVIRON_INDEX.clear()
    yield
    ps._MARKED_PROCESSES_BEAT.clear()
    ps._ENVIRON_INDEX.clear()


@pytest.fixture
def beat_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """A monotonic clock the case advances by hand to cross a beat."""
    state = {"now": 1000.0}
    monkeypatch.setattr(ps, "_beat_clock", lambda: state["now"])
    return state


@pytest.fixture
def proc_listings(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every listing of ``/proc``, counted at the one walk that takes one."""
    listings: list[int] = []
    real = Path.iterdir

    def counted(self: Path):
        if str(self) == "/proc":
            listings.append(1)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", counted)
    return listings


def test_the_walk_names_the_live_child(tmp_path: Path) -> None:
    """The positive control: the walk sees a child known to be carrying the mark."""
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, home)
    try:
        _wait_for_environ_mark(child.pid, run_id)
        assert ps._processes_owning_run(run_id, _crew_home_of(home)) == [child.pid]
    finally:
        _reap(child)


def test_several_runs_in_one_beat_list_proc_once(
    tmp_path: Path, beat_clock: dict[str, float], proc_listings: list[int]
) -> None:
    """Several runs asked in one beat cost one listing of ``/proc``.

    Three asks in the same beat are answered from the map the first one walked,
    so ``/proc`` is listed once rather than once per run. The child carrying one
    of the run ids is the control that the walk saw the live table.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, home)
    try:
        _wait_for_environ_mark(child.pid, run_id)
        proc_listings.clear()

        assert ps._processes_owning_run(
            run_id, _crew_home_of(home), from_beat_map=True
        ) == [child.pid]
        assert (
            ps._processes_owning_run(
                _unique_run_id(), _crew_home_of(home), from_beat_map=True
            )
            == []
        )
        assert (
            ps._processes_owning_run(
                _unique_run_id(), _crew_home_of(home), from_beat_map=True
            )
            == []
        )

        assert len(proc_listings) == 1
    finally:
        _reap(child)


def test_the_next_beat_lists_proc_again(
    tmp_path: Path, beat_clock: dict[str, float], proc_listings: list[int]
) -> None:
    """A beat after the walk lists ``/proc`` afresh."""
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, home)
    try:
        _wait_for_environ_mark(child.pid, run_id)
        ps._processes_owning_run(run_id, _crew_home_of(home), from_beat_map=True)
        after_first = len(proc_listings)

        beat_clock["now"] += ps.BEAT_SECONDS
        ps._processes_owning_run(run_id, _crew_home_of(home), from_beat_map=True)

        assert len(proc_listings) == after_first + 1
    finally:
        _reap(child)


def test_a_process_started_after_the_walk_is_found_on_the_next_beat(
    tmp_path: Path, beat_clock: dict[str, float]
) -> None:
    """A worker that starts after a beat's walk is found on the following beat.

    The beat's map was walked before the child existed, so the same beat does
    not name it and the next beat does.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()

    # Walk the beat before the worker exists.
    assert (
        ps._processes_owning_run(run_id, _crew_home_of(home), from_beat_map=True) == []
    )

    child = _start_child(run_id, home)
    try:
        _wait_for_environ_mark(child.pid, run_id)
        # Same beat: the map is current and predates the worker.
        assert (
            ps._processes_owning_run(run_id, _crew_home_of(home), from_beat_map=True)
            == []
        )

        beat_clock["now"] += ps.BEAT_SECONDS
        assert ps._processes_owning_run(
            run_id, _crew_home_of(home), from_beat_map=True
        ) == [child.pid]
    finally:
        _reap(child)


def test_a_mark_under_another_crew_home_is_not_answered(
    tmp_path: Path, beat_clock: dict[str, float]
) -> None:
    """A process carrying the run id under another crew home is not this run's.

    The key is the pair of run mark and crew home, so the same run id under a
    different home is answered for that home and never for this one.
    """
    this_home = tmp_path / "this"
    other_home = tmp_path / "other"
    this_home.mkdir()
    other_home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, other_home)
    try:
        _wait_for_environ_mark(child.pid, run_id)

        assert (
            ps._processes_owning_run(
                run_id, _crew_home_of(this_home), from_beat_map=True
            )
            == []
        )
        assert ps._processes_owning_run(
            run_id, _crew_home_of(other_home), from_beat_map=True
        ) == [child.pid]
    finally:
        _reap(child)


def test_the_stop_path_keeps_its_start_tick_floor(
    tmp_path: Path, beat_clock: dict[str, float]
) -> None:
    """A fresh stop walk with a floor above the worker's tick excludes it.

    The floor is unchanged by the beat map: the stop reads the start tick of
    each named pid and drops any that began before the run's own launch.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, home)
    try:
        _wait_for_environ_mark(child.pid, run_id)
        tick = ps._start_tick(child.pid)
        assert tick is not None

        assert ps._processes_owning_run(
            run_id, _crew_home_of(home), started_no_earlier_than=tick
        ) == [child.pid]
        assert (
            ps._processes_owning_run(
                run_id, _crew_home_of(home), started_no_earlier_than=tick + 1
            )
            == []
        )
    finally:
        _reap(child)


def test_the_stop_path_finds_a_worker_that_started_after_the_beat_walk(
    tmp_path: Path, beat_clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop walks afresh and signals a worker the current beat map lacks.

    The beat's map is walked before the worker exists and is still current when
    the stop is issued. A stop that read that map would find nothing to signal;
    the stop walks the table afresh, names the worker and delivers the signal,
    which is what the declared negative control removes.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(RECKON_HOME_ENV, str(home))
    run_id = _unique_run_id()

    # The beat's map is current and was walked before the worker existed.
    assert (
        ps._processes_owning_run(run_id, _crew_home_of(home), from_beat_map=True) == []
    )

    child = _start_child(run_id, home)
    try:
        _wait_for_environ_mark(child.pid, run_id)
        assert (
            ps._processes_owning_run(run_id, _crew_home_of(home), from_beat_map=True)
            == []
        )

        ended = ps.signal_worker(
            None,
            signal.SIGTERM,
            reason="beat-map stop control",
            run_id=run_id,
            reach_run=True,
        )

        assert child.pid in ended
        child.wait(timeout=10)
        assert child.poll() is not None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child.pid, signal.SIGKILL)
        _reap(child)
