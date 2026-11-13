"""A watch seat held by a process that produces nothing is reclaimed.

``reckon crew watch --ensure`` reported ``registration.reason: seat-held``
beside an inactive unit for half an hour while no obligation row reached the
coordinator: a live process held the project's watch lock without producing.
Reporting the seat as held is not enough, because the seat is what admits every
dispatch — and stopping whatever holds it is not enough either, because the
holder is not always the project's producer.

These cases pin both halves against a real advisory lock in a temporary crew
home, held by real stub processes, so the holder table the command reads is the
kernel's own:

* a stale producer holding the seat is reported stuck, stopped, and the
  producer the arming starts holds the seat afterwards — identified by pid, not
  merely by the seat being held;
* a process that is not this project's producer is reported under
  ``seat-held-by-non-producer`` and left alive;
* a live producer with a fresh lease is left alone, as before;
* a process that merely holds the lock file open, beside a stale producer that
  flocks it, is neither named nor stopped.

The stop is the one thing the negative control removes: with it gone the first
case fails on the seat's holder still being the stale pid.
"""

from __future__ import annotations

import select
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import runs, watch_unit
from tests.test_crew_watch_ensure import FakeWatchService, _backend_bin, _config
from tests.test_watcher_arming_fallback import UnreachableWatchService

PROJECT = "seat-project"

# A stub process that takes the project's seat, or only opens the file. The
# second mode is the reader the holder table must ignore: it has the lock file
# open and holds nothing.
_HOLDER = """
import fcntl, sys, time
path, mode = sys.argv[1], sys.argv[2]
handle = open(path, "ab")
if mode == "hold":
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(300)
"""


class Holder:
    """A stub process holding (or merely opening) one lock file."""

    def __init__(self, path: Path, mode: str, *, wait: float = 15.0) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(path), mode],
            stdout=subprocess.PIPE,
            text=True,
        )
        # Bounded rather than a bare read: a stub asked to take a seat another
        # process still holds blocks on its flock, and a test that waited for
        # that would report nothing at all instead of the failure.
        ready, _, _ = select.select([self.process.stdout], [], [], wait)
        line = (self.process.stdout.readline() or "").strip() if ready else ""
        if line != "ready":
            self.process.kill()
            self.process.wait(timeout=10)
            raise AssertionError(f"stub holder never took its {mode} seat: {line!r}")

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def wait_gone(self, timeout: float = 10.0) -> int | None:
        """Collect the process, returning its exit status or None if it lives."""
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        if self.process.stdout is not None:
            self.process.stdout.close()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway crew home with a lease short enough to lapse in a test."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(runs.PRODUCER_LEASE_ENV, "5")
    return home


@pytest.fixture()
def lock_path(crew_home: Path) -> Path:
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def holders() -> list[Holder]:
    """Every stub this test started, stopped on the way out whatever happened."""
    started: list[Holder] = []
    yield started
    for holder in started:
        holder.stop()


@pytest.fixture()
def arming(tmp_path: Path) -> tuple[FakeWatchService, dict]:
    """The fallback arming path, whose producer seam is the caller's."""
    return UnreachableWatchService(), _config(_backend_bin(tmp_path))


def _registration(pid: int, *, renewed_ago: float) -> dict:
    """Record a producer for this project whose lease was renewed that long ago."""
    return runs.update_watch_registration(
        PROJECT,
        pid=pid,
        host=socket.gethostname(),
        started_at="2026-11-03T05:31:23Z",
        lease_renewed_at=time.time() - renewed_ago,
    )


def test_a_stale_producer_loses_the_seat_to_the_one_that_starts(
    lock_path: Path, arming, holders: list[Holder]
) -> None:
    """The stale producer is named, stopped, and the new producer holds the seat."""
    manager, config = arming
    stale = Holder(lock_path, "hold")
    holders.append(stale)
    _registration(stale.pid, renewed_ago=60.0)
    started: list[Holder] = []

    def producer(project: str) -> dict:
        started.append(Holder(lock_path, "hold"))
        holders.append(started[-1])
        return {"project": project, "watcher_live": True}

    result = runs.ensure_watcher_service(
        PROJECT, manager=manager, config=config, producer=producer
    )

    assert result["seat"]["reason"] == "seat-held-by-stuck-producer"
    assert [row["pid"] for row in result["seat"]["stopped"]] == [stale.pid]
    assert "SIGTERM" in result["seat"]["stopped"][0]["signals"]
    assert stale.wait_gone() is not None
    assert len(started) == 1
    # The holder identity, not only the fact that something holds the seat: the
    # kernel's lock table resolves to the producer this call started, and the
    # stale pid is gone from it.
    assert [row.pid for row in watch_unit.seat_lock_holders(PROJECT)] == [
        started[0].pid
    ]


def test_a_non_producer_holding_the_seat_is_reported_and_left_alive(
    lock_path: Path, arming, holders: list[Holder]
) -> None:
    """A holder that is not this project's producer is named and not stopped."""
    manager, config = arming
    squatter = Holder(lock_path, "hold")
    holders.append(squatter)
    _registration(424242, renewed_ago=60.0)

    def producer(project: str) -> dict:  # pragma: no cover - must not be called
        raise AssertionError("the non-producer seat was started over")

    result = runs.ensure_watcher_service(
        PROJECT, manager=manager, config=config, producer=producer
    )

    assert result["seat"]["reason"] == "seat-held-by-non-producer"
    assert [row["pid"] for row in result["seat"]["holders"]] == [squatter.pid]
    assert result["seat"]["holders"][0]["command"]
    assert result["registration"]["reason"] == "seat-held-by-non-producer"
    assert result["started"] is False
    assert manager.starts == []
    assert squatter.alive


def test_a_live_producer_with_a_fresh_lease_is_left_alone(
    lock_path: Path, arming, holders: list[Holder]
) -> None:
    """A seat its producer still holds, renewed inside the lease, is untouched."""
    _, config = arming
    # A manager that starts the unit successfully, so this case says what the
    # seat rule does and not what the bus fallback does.
    manager = FakeWatchService()
    live = Holder(lock_path, "hold")
    holders.append(live)
    _registration(live.pid, renewed_ago=0.0)

    def producer(project: str) -> dict:  # pragma: no cover - must not be called
        raise AssertionError("a live producer's seat was started over")

    result = runs.ensure_watcher_service(
        PROJECT, manager=manager, config=config, producer=producer
    )

    assert result["seat"] is None
    assert result["registration"]["reason"] == "seat-held"
    assert live.alive


def test_an_opener_is_neither_a_holder_nor_stopped(
    lock_path: Path, arming, holders: list[Holder]
) -> None:
    """A process with the lock file open but no flock is not the holder."""
    manager, config = arming
    opener = Holder(lock_path, "open")
    holders.append(opener)
    stale = Holder(lock_path, "hold")
    holders.append(stale)
    _registration(stale.pid, renewed_ago=60.0)
    started: list[Holder] = []

    def producer(project: str) -> dict:
        started.append(Holder(lock_path, "hold"))
        holders.append(started[-1])
        return {"project": project, "watcher_live": True}

    result = runs.ensure_watcher_service(
        PROJECT, manager=manager, config=config, producer=producer
    )

    named = [row["pid"] for row in result["seat"]["holders"]]
    assert named == [stale.pid]
    assert opener.pid not in named
    assert stale.wait_gone() is not None
    assert opener.alive
