"""``crew watch --stop`` reads the reconciled seat, not a registration mid-write.

A producer rewrites its seat record in place — truncate, then write — so a
reader in that window sees an empty record. unwatch read that record's absent
``project`` as a foreign registration and refused a producer that was its own,
which is the refusal a test hit while tearing down beside a live producer.

The seat's project is now decided the way ``watch_state`` decides it: from the
live holder this project's lease names, not from the record alone. A seat that
positively names another project, or a foreign host, is still refused.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import recovery_watch, runs, watch_unit
from reckon.crew.node import CrewError

PROJECT = "unwatch-reconciled-sample"
OTHER_PROJECT = "unwatch-reconciled-other"
FOREIGN_HOST = "some-other-host"
LEASE_SECONDS = 60

DRIVER = """\
import sys

from reckon.crew.recovery import watch_ticker

interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
for _ in watch_ticker(sys.argv[1], poll_interval=interval):
    pass
"""

# Holds one project's seat lock in a process of its own, so a held seat exists
# without the test process owning the holder's lifetime.
LOCK_DRIVER = """\
import fcntl
import sys
import time

handle = open(sys.argv[1], "a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
while True:
    time.sleep(0.2)
"""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the temporary directory the test owns."""
    config_home = tmp_path / "config"
    config_home.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", str(LEASE_SECONDS))
    return config_home


def _seat_record(project: str) -> dict:
    """Read a project's seat record without taking its lock."""
    path = runs.watch_lock_path(project)
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return runs._read_watch_record(handle)


def _write_seat(project: str, record: dict) -> None:
    """Replace a seat record's content, leaving its inode and lock alone."""
    path = runs.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        runs._write_watch_record(handle, record)


def _await(predicate, *, timeout: float = 20.0, message: str) -> None:
    """Wait for a predicate, bounded, so a failure names what never happened."""
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _spawn(tmp_path: Path, name: str, source: str, *args: str):
    """Run a helper driver from the repository root, in its own session."""
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(script), *args],
        env=dict(os.environ),
        cwd=str(root),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


@pytest.fixture()
def producer(tmp_path: Path, home: Path):
    """A live ``crew watch`` producer of this project, stopped afterwards."""
    del home  # requested for its environment, not its path
    child = _spawn(tmp_path, "producer_driver.py", DRIVER, PROJECT, "0.1")
    try:
        _await(
            lambda: _seat_record(PROJECT).get("pid") == child.pid,
            message="the producer never took its seat",
        )
        _await(
            lambda: runs.read_watch_registration(PROJECT).get("pid") == child.pid,
            message="the producer never wrote its registration",
        )
        yield child
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def _hold_seat(tmp_path: Path, project: str, record: dict):
    """Write a seat record, then hold its lock from a process of its own."""
    _write_seat(project, record)
    lock = str(runs.watch_lock_path(project))
    holder = _spawn(tmp_path, "lock_driver.py", LOCK_DRIVER, lock)
    assert holder.stdout.readline().strip() == "held", (
        "the lock holder never took the seat"
    )
    return holder


def test_unwatch_stops_a_live_producer_whose_seat_is_mid_write(
    producer, home: Path
) -> None:
    """The seat names no project while this project's producer holds it.

    This is the record a reader sees between the truncate and the write of an
    in-place seat rewrite. The live holder is this project's own producer, so
    the reconciled reading is this project's seat and unwatch stops it; the raw
    registration refuses the stop outright, and even past that the emptied
    record carries no pid for the signal guard.
    """
    del home
    _write_seat(PROJECT, {})
    assert _seat_record(PROJECT) == {}, "the seat record was not emptied"

    result = recovery_watch.unwatch(PROJECT)

    assert result["stopped"] is True, result
    assert result["watcher"]["pid"] == producer.pid, result
    _await(
        lambda: producer.poll() is not None,
        message="the producer outlived the unwatch that stopped it",
    )


def test_unwatch_reconciles_the_seat_through_the_shared_reading(
    producer, home: Path
) -> None:
    """The reconciled seat is the shared reading, not a second copy of its shape.

    unwatch decides a mid-write seat the way the rest of the codebase reads one,
    so the shape of a reconciled record has a single definition rather than one
    per reader that must be kept in step.
    """
    del home
    _write_seat(PROJECT, {})
    assert _seat_record(PROJECT) == {}, "the seat record was not emptied"

    reconciled = recovery_watch._reconciled_seat_record(PROJECT)
    shared = watch_unit.watch_state(PROJECT)["watcher"]

    assert reconciled["pid"] == producer.pid == shared["pid"]
    assert reconciled["host"] == shared["host"]
    assert reconciled["job"] == shared["job"]


def test_unwatch_still_refuses_a_seat_naming_another_project(
    tmp_path: Path, home: Path
) -> None:
    """A seat that positively names a different project is not reconciled away."""
    del home
    holder = _hold_seat(
        tmp_path,
        PROJECT,
        {"project": OTHER_PROJECT, "host": socket.gethostname(), "pid": os.getpid()},
    )
    try:
        with pytest.raises(CrewError) as refusal:
            recovery_watch.unwatch(PROJECT)
        assert f"the locked registration names {OTHER_PROJECT!r}" in str(refusal.value)
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_unwatch_still_refuses_a_seat_naming_a_foreign_host(
    tmp_path: Path, home: Path
) -> None:
    """A seat naming another host is refused, as it was before reconciliation."""
    del home
    holder = _hold_seat(
        tmp_path,
        PROJECT,
        {"project": PROJECT, "host": FOREIGN_HOST, "pid": os.getpid()},
    )
    try:
        with pytest.raises(CrewError) as refusal:
            recovery_watch.unwatch(PROJECT)
        assert "producer seat names" in str(refusal.value)
    finally:
        holder.kill()
        holder.wait(timeout=10)
