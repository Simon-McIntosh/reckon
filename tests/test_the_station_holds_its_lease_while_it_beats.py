"""A station keeps its writer lease while it beats, and writes nothing once it loses it.

The lease used to be renewed between ticks, while a tick can walk every mounted
project and outrun the lease's whole stale window. For most of such a tick the
lease read stale, so the supervisor's station check or a hand-run station was
free to claim it and start a second writer beside the live one. The facts
measured here are the ones that failure needs, in order:

* a tick held open past the stale window leaves the lease fresh throughout;
* during that tick a second station is refused, and the supervisor's station
  check starts no second process;
* with the heartbeat write blocked past the window the lease is still renewed,
  which is what says the renewal runs on a thread that does no store work;
* a lease replaced partway through one project's refresh costs the first
  station every store write from that moment, and its loop stops.

The clock is a fast one: the stale window passes in a couple of real seconds,
so no arm waits out the product's thirty.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from reckon.crew import fleet_supervisor, station, station_store
from reckon.crew.host_lease import LEASE_STALE_SECONDS, HostLease, local_host

PROJECT = "station-lease-fixture"

# How long the stale window takes in real seconds under the fast clock below.
STALE_WINDOW_REAL_SECONDS = 2.0
CLOCK_SPEED = LEASE_STALE_SECONDS / STALE_WINDOW_REAL_SECONDS

# The renewal cadence the arms run at. It is short so a fast clock cannot
# outrun it: each renewal moves the lease a fraction of a window ahead.
RENEW_SECONDS = 0.02

# Every store method that writes. A station that loses its lease may call none
# of them successfully.
WRITE_METHODS = (
    "set_station_value",
    "heartbeat",
    "rebuild",
    "tick",
    "ingest_pointer",
    "ingest_stream",
    "classify_tick",
    "write_cursor",
    "write_obligation",
    "delete_obligation",
    "write_event",
    "record_tick",
    "sweep_retention",
)


class _FastClock:
    """A monotonic clock that runs the lease's stale window in two real seconds."""

    def __init__(self) -> None:
        self._origin = time.monotonic()
        self._base = time.time()

    def __call__(self) -> float:
        return self._base + (time.monotonic() - self._origin) * CLOCK_SPEED


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The crew home every reader in one arm resolves, kept out of the real one."""
    root = tmp_path / "home"
    (root / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(root))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(root / "mounts.json"))
    (root / "mounts.json").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node-local root the store, the lease and the supervisor all resolve."""
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root


def _lease(runtime: Path, clock: _FastClock, pid: int | None = None) -> HostLease:
    """A station lease. A second station is another process, not another seat."""
    return HostLease(
        runtime,
        "station",
        local_host(),
        os.getpid() if pid is None else pid,
        "",
        clock=clock,
    )


def _station(
    store: station_store.StationStore, home: Path, tmp_path: Path
) -> station.Station:
    return station.Station(
        store,
        home=home,
        projects=lambda: {PROJECT: tmp_path / "docs"},
    )


def _run_in_thread(target) -> tuple[threading.Thread, list[BaseException]]:
    """Run a station in a thread, keeping whatever it raised for the arm to read."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            target()
        except BaseException as error:  # noqa: BLE001 - the arm asserts on it
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


class _Hold:
    """Hold the tick inside one project's refresh until the arm releases it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        original = station.station_obligations.publish

        def publish(*args, **kwargs):
            self.reached.set()
            assert self.release.wait(timeout=20), "the arm never released the tick"
            return original(*args, **kwargs)

        monkeypatch.setattr(station.station_obligations, "publish", publish)

    def release_and_join(self, thread: threading.Thread) -> None:
        self.release.set()
        thread.join(timeout=20)


def _fresh_samples(lease: HostLease, *, windows: float = 1.6) -> tuple[int, int]:
    """Sample the lease's holder for ``windows`` stale windows.

    Returns how many samples were taken and how many of them read stale, so an
    arm can show it watched for long enough to have seen a lapse.
    """
    deadline = time.monotonic() + STALE_WINDOW_REAL_SECONDS * windows
    samples = 0
    stale = 0
    while time.monotonic() < deadline:
        samples += 1
        if lease.holder() is None:
            stale += 1
        time.sleep(0.01)
    return samples, stale


def _record_writes(
    store: station_store.StationStore, clock: _FastClock
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Record every store write this handle completes, and every one it refused."""
    completed: list[tuple[str, float]] = []
    refused: list[tuple[str, float]] = []
    for name in WRITE_METHODS:
        original = getattr(store, name)

        def wrapper(*args, _original=original, _name=name, **kwargs):
            try:
                result = _original(*args, **kwargs)
            except station_store.StationLeaseLostError:
                refused.append((_name, clock()))
                raise
            completed.append((_name, clock()))
            return result

        setattr(store, name, wrapper)
    return completed, refused


def _replace_lease(path: Path, clock: _FastClock) -> float:
    """Hand the lease to a successor the way a second writer would take it."""
    successor = {
        "host": local_host(),
        "pid": os.getpid(),
        "job": "",
        "token": uuid4().hex,
    }
    temporary = path.parent / ".lease-successor"
    temporary.write_text(json.dumps(successor), encoding="utf-8")
    at = clock()
    os.utime(temporary, (at, at))
    os.replace(temporary, path)
    return at


def test_a_held_open_tick_keeps_the_lease_fresh_and_refuses_a_second_station(
    home: Path, runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FastClock()
    store = station_store.StationStore(runtime)
    hold = _Hold(monkeypatch)
    lease = _lease(runtime, clock)
    thread, errors = _run_in_thread(
        lambda: station.run_station(
            store=store,
            station_instance=_station(store, home, tmp_path),
            lease=lease,
            once=True,
            beat_seconds=0.1,
            renew_seconds=RENEW_SECONDS,
        )
    )
    try:
        assert hold.reached.wait(timeout=20), "the tick never reached a project refresh"

        samples, stale = _fresh_samples(lease)
        assert samples > 10
        assert stale == 0, f"the lease read stale in {stale} of {samples} samples"
        assert lease.holder() is not None

        # A second station asked for while the first is held open is refused by
        # name, and the supervisor's own check starts no process at all.
        with pytest.raises(station.StationHeldError) as refusal:
            station.run_station(
                store=station_store.StationStore(runtime),
                lease=_lease(runtime, clock, pid=os.getpid() + 1),
                once=True,
                renew_seconds=RENEW_SECONDS,
            )
        assert str(lease.path) in str(refusal.value)

        process = fleet_supervisor.StationProcess(
            runtime, environ={"XDG_RUNTIME_DIR": str(runtime)}
        )
        try:
            process.ensure()
            assert process.process is None, "a second station was started"
        finally:
            if process.process is not None:
                process.process.kill()
                process.process.wait(timeout=10)
    finally:
        hold.release_and_join(thread)
        store.close()
    assert errors == []


def test_the_lease_is_renewed_while_the_heartbeat_write_is_blocked(
    home: Path, runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FastClock()
    store = station_store.StationStore(runtime)
    lease = _lease(runtime, clock)
    blocked = threading.Event()
    unblock = threading.Event()
    original = station_store.StationStore.heartbeat

    def heartbeat(self, *, now: datetime | None = None) -> None:
        blocked.set()
        assert unblock.wait(timeout=20), "the arm never released the heartbeat write"
        original(self, now=now)

    # Patched on the class, so the beat thread's own handle is blocked too: the
    # arm holds every heartbeat in the process, not just the tick's.
    monkeypatch.setattr(station_store.StationStore, "heartbeat", heartbeat)
    thread, errors = _run_in_thread(
        lambda: station.run_station(
            store=store,
            station_instance=_station(store, home, tmp_path),
            lease=lease,
            once=True,
            beat_seconds=0.1,
            renew_seconds=RENEW_SECONDS,
        )
    )
    try:
        assert blocked.wait(timeout=20), "no heartbeat write was attempted"

        samples, stale = _fresh_samples(lease)
        assert samples > 10
        assert stale == 0, f"the lease read stale in {stale} of {samples} samples"
    finally:
        unblock.set()
        thread.join(timeout=20)
        store.close()
    assert errors == []


def test_a_replaced_lease_refuses_every_write_and_stops_the_loop(
    home: Path, runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FastClock()
    store = station_store.StationStore(runtime)
    completed, refused = _record_writes(store, clock)
    hold = _Hold(monkeypatch)
    lease = _lease(runtime, clock)
    thread, errors = _run_in_thread(
        lambda: station.run_station(
            store=store,
            station_instance=_station(store, home, tmp_path),
            lease=lease,
            once=True,
            # The beat is far out of the way, so every write this arm attributes
            # to the tick is the tick's.
            beat_seconds=600.0,
            renew_seconds=RENEW_SECONDS,
        )
    )
    try:
        assert hold.reached.wait(timeout=20), "the tick never reached a project refresh"
        completed_before = len(completed)
        assert completed_before > 0, "the tick wrote nothing before the replacement"

        replaced_at = _replace_lease(lease.path, clock)
        deadline = time.monotonic() + 10.0
        while not store.guard.lost() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.guard.lost(), "the renewal thread never noticed the replacement"
    finally:
        hold.release_and_join(thread)
        store.close()

    assert not thread.is_alive(), (
        "the station's loop kept running after losing its lease"
    )
    assert errors and isinstance(errors[0], station.StationHeldError), errors
    late = [(name, at) for name, at in completed if at > replaced_at]
    assert late == [], f"store writes completed after the lease was replaced: {late}"
    assert refused, "the tick was never refused a write, so the guard never fired"
