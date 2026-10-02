"""``crew unwatch`` leaves a sender record before it signals the watcher.

A watcher has no run directory, so before this the stop it issued recorded
nothing: the producer's stream simply ended and the next unattributed watcher
death had no directory to be read from. The seat registration is the file
unwatch reads the pid from, so the record lands beside it, written before the
signal. These cases arm a real producer in a temporary ``RECKON_HOME``, run
unwatch, and assert the record exists, names the producer and the unwatch
reason, and was written while the producer was still alive.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, routing, runs

PROJECT = "unwatch-sender-sample"
LEASE_SECONDS = 60

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


def _sender_records(project: str) -> list[dict]:
    """The sender records written beside one project's watch registration."""
    path = runs.watch_lock_path(project).parent / routing.SENDER_RECORD_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _await(predicate, *, timeout: float = 20.0, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


@pytest.fixture()
def producer(tmp_path: Path, home: Path):
    """A live ``crew watch`` producer in its own session, stopped afterwards."""
    del home  # requested for its environment, not its path
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "producer_driver.py"
    script.write_text(DRIVER, encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root), environment.get("PYTHONPATH", "")]
    )
    child = subprocess.Popen(
        [sys.executable, str(script), PROJECT, "0.1"],
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


def test_unwatch_writes_a_sender_record_before_it_signals(
    producer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is on disk, and names the live producer, when the signal goes out.

    The signal home is wrapped so the two orderings are read at the instant of
    signalling: the record must already exist and the producer must still be
    alive. A write ordered after the signal could satisfy neither.
    """
    observed: dict = {}
    real = recovery._signal_process_group

    def spy(pid, start_time, **kwargs):
        observed["pid"] = pid
        observed["records_at_signal"] = _sender_records(PROJECT)
        observed["alive_at_signal"] = producer.poll() is None
        return real(pid, start_time, **kwargs)

    monkeypatch.setattr(recovery, "_signal_process_group", spy)

    result = recovery.unwatch(PROJECT)
    assert result["stopped"] is True, result

    assert observed["pid"] == producer.pid
    assert observed["records_at_signal"], (
        "no sender record on disk when unwatch signalled the producer"
    )
    assert observed["alive_at_signal"] is True, (
        "the producer had already exited before unwatch signalled it"
    )

    records = _sender_records(PROJECT)
    assert len(records) == 1
    record = records[0]
    assert record["target_pid"] == producer.pid
    assert record["reason"] == "unwatch"
    assert record["sender_pid"] == os.getpid()
    assert record["signal"] == "SIGTERM"
    assert record["time"]

    _await(
        lambda: producer.poll() is not None,
        timeout=10.0,
        message="the producer outlived the unwatch that stopped it",
    )


def test_unwatch_with_nothing_to_stop_leaves_no_record(home: Path) -> None:
    """No signal means no record: the writer belongs to the stop, not the command.

    A project with no registered watcher signals nothing, so nothing is
    attributed. This keeps the record a claim about a signal rather than a
    claim about a command that ran.
    """
    del home
    result = recovery.unwatch(PROJECT)
    assert result["reason"] == "nothing-to-stop", result
    assert _sender_records(PROJECT) == []
