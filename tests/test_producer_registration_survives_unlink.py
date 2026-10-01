"""A producer recreates the seat record an unlink took its path out from under.

A per-project ``crew watch`` producer is detached and reparented to init, so
nothing but its own lease ends it. The seat record at ``watch_lock_path`` is the
file ``crew unwatch`` opens to find it: the record holds the advisory lock the
process table reads liveness from and carries the pid unwatch signals. When that
file is unlinked under a running producer nothing recreates it, so unwatch opens
a fresh inode, takes a lock the producer believes it still holds, and answers
"nothing to stop" while the producer runs on unwatched — reachable only by pid.
The cases below pin the repair: a producer rewrites its record at the same path
on its next wake-up, naming its own pid, so unwatch finds and stops it.

The file unwatch reads the pid from is the seat record at
``runs.watch_lock_path(project)`` — not the separate lease registration the
producer's lease lives in (``runs.watch_registration_path``). The lease
registration is written read-modify-write by two writers and records
``lease_renewed_at``; unwatch never opens it, so unlinking it cannot blind
unwatch. The seat record is the one whose disappearance does.

The producer runs as a real child in its own session, because unwatch signals
the recorded pid's process group and refuses to signal a pid that shares the
caller's own group. A thread would exercise the re-linking but not the stop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.recovery import unwatch

# Long enough that the producer cannot lapse while the test works, so the cases
# below measure the unlink, never the lease.
LEASE_SECONDS = 60
PROJECT = "registration-unlink-sample"

# A producer armed with this poll interval sleeps long enough for a replacement
# to take the vacated seat before the superseded producer's next wake-up, so the
# superseded case measures the takeover rather than which process wins a race.
SUPERSEDED_POLL_SECONDS = 8.0

DRIVER = textwrap.dedent(
    """
    import sys

    from reckon.crew.recovery import watch_ticker

    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    for _ in watch_ticker(sys.argv[1], poll_interval=interval):
        pass
    """
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", str(LEASE_SECONDS))
    return config_home


def _seat_record(project: str) -> dict:
    """Read the project's seat record without taking its lock."""
    path = runs.watch_lock_path(project)
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return runs._read_watch_record(handle)


def _await(predicate, *, timeout: float = 20.0, message: str):
    """Poll ``predicate`` until it is truthy, or fail naming what never came."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def _write_driver(tmp_path: Path) -> Path:
    """Write the producer driver beside the test's own temporary files."""
    script = tmp_path / "producer_driver.py"
    script.write_text(DRIVER, encoding="utf-8")
    return script


def _spawn_producer(root: Path, script: Path, poll_interval: float) -> subprocess.Popen:
    """Start a ``crew watch`` producer in its own session.

    The producer is a real process rather than a thread: unwatch signals the
    recorded pid's process group and refuses to signal its own, so only a child
    in a separate session exercises the stop as well as the re-linking.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root), environment.get("PYTHONPATH", "")]
    )
    return subprocess.Popen(
        [sys.executable, str(script), PROJECT, str(poll_interval)],
        env=environment,
        cwd=str(root),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _await_completed_a_poll(project: str) -> None:
    """Wait until the armed producer has finished a poll and reached its sleep.

    The lease registration gains ``bytes_parsed_last_poll`` only once the loop
    has run past its first wake-up, so its presence proves the producer is at or
    past the sleep and will not revisit the seat path until that sleep ends.
    This is what lets the superseded case take the path without racing the
    producer's own recreate.
    """

    def polled() -> bool:
        return "bytes_parsed_last_poll" in runs.read_watch_registration(project)

    _await(polled, message="the producer never completed its first poll pass")


@pytest.fixture()
def producer(tmp_path: Path, home: Path):
    """A live ``crew watch`` producer in its own session, stopped afterwards."""
    del home  # requested for its environment, not its path
    root = Path(__file__).resolve().parents[1]
    script = _write_driver(tmp_path)
    child = _spawn_producer(root, script, 0.1)
    try:
        _await(
            lambda: _seat_record(PROJECT).get("pid") == child.pid,
            message="the producer never took its seat",
        )
        yield child
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_a_producer_recreates_its_registration_after_an_unlink(producer) -> None:
    """The seat record an unlink removed is back, naming the producer, next wake."""
    seat = runs.watch_lock_path(PROJECT)
    assert _seat_record(PROJECT).get("pid") == producer.pid

    seat.unlink()
    # The path is gone now, so the record read below cannot be the one that was
    # there before: its presence again is the producer's own rewrite. An inode
    # comparison would not say this — the filesystem reuses a freed inode — so
    # the absence is asserted directly instead.
    assert not seat.exists()

    def recreated() -> bool:
        return seat.is_file() and _seat_record(PROJECT).get("pid") == producer.pid

    _await(
        recreated,
        message="the producer never recreated its registration after the unlink",
    )


def test_unwatch_finds_and_stops_a_producer_after_an_unlink(producer) -> None:
    """After the unlink, unwatch stops the producer instead of finding nothing."""
    seat = runs.watch_lock_path(PROJECT)
    seat.unlink()

    def recreated() -> bool:
        return seat.is_file() and _seat_record(PROJECT).get("pid") == producer.pid

    _await(recreated, message="the producer never recreated its registration")

    result = unwatch(PROJECT)
    assert result["stopped"] is True, result
    assert result["registration_released"] is True
    assert result["reason"] == "stopped", result

    _await(
        lambda: producer.poll() is not None,
        timeout=10.0,
        message="the producer outlived the unwatch that stopped it",
    )


def test_a_superseded_producer_ends_without_overwriting_the_replacement(
    tmp_path: Path, home: Path
) -> None:
    """A producer whose vacated seat a replacement took ends rather than overwrite it.

    The seat file is unlinked, so a later arming opens a fresh inode and the
    superseded producer's held handle now names a file no path reaches. A
    replacement producer takes the path first. On its next wake-up the
    superseded producer must end without writing over the replacement's record,
    and the replacement must stay the seat unwatch finds and stops.
    """
    del home  # requested for its environment, not its path
    root = Path(__file__).resolve().parents[1]
    script = _write_driver(tmp_path)
    superseded = _spawn_producer(root, script, SUPERSEDED_POLL_SECONDS)
    replacement = None
    try:
        _await(
            lambda: _seat_record(PROJECT).get("pid") == superseded.pid,
            message="the superseded producer never took its seat",
        )
        _await_completed_a_poll(PROJECT)

        seat = runs.watch_lock_path(PROJECT)
        seat.unlink()
        assert not seat.exists()

        replacement = _spawn_producer(root, script, 0.1)
        _await(
            lambda: _seat_record(PROJECT).get("pid") == replacement.pid,
            message="the replacement producer never took the vacated seat",
        )

        # The superseded producer wakes to find the path held by a replacement's record.
        _await(
            lambda: superseded.poll() is not None,
            message="the superseded producer outlived the replacement that took its seat",
        )

        # And it ended without overwriting the record it no longer owns.
        record = _seat_record(PROJECT)
        assert record.get("pid") == replacement.pid, record

        result = unwatch(PROJECT)
        assert result["stopped"] is True, result
        assert result["reason"] == "stopped", result
        _await(
            lambda: replacement.poll() is not None,
            timeout=10.0,
            message="the replacement producer outlived the unwatch that stopped it",
        )
    finally:
        for child in (superseded, replacement):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=10)
