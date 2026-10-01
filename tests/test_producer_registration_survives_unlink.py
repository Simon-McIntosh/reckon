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

DRIVER = textwrap.dedent(
    """
    import sys

    from reckon.crew.recovery import watch_ticker

    for _ in watch_ticker(sys.argv[1], poll_interval=0.1):
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


@pytest.fixture()
def producer(tmp_path: Path, home: Path):
    """A live ``crew watch`` producer in its own session, stopped afterwards."""
    del home  # requested for its environment, not its path
    root = Path(__file__).resolve().parents[1]
    script = _write_driver(tmp_path)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root), environment.get("PYTHONPATH", "")]
    )
    child = subprocess.Popen(
        [sys.executable, str(script), PROJECT],
        env=environment,
        cwd=str(root),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
