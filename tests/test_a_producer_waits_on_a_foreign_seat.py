"""A producer finding its project's seat on another host waits instead of exiting.

A login-node watcher unit starts, finds the project's seat held by a producer on
another host, prints that a watcher is live and exits cleanly, so systemd
restarts it every few seconds and its log grows without bound. These cases hold
the contract: while the foreign producer's stream is still moving the watch
starts no restart, yields no row and takes no seat, and once that producer's
stream goes stale it takes the seat and publishes from the stream's current
tail. A seat held by a producer this host owns reads exactly as it did before.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery_watch, runs

PROJECT = "a-foreign-seat"
FOREIGN_HOST = "a-host-that-is-not-this-one"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=UTC)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every case at a temporary crew home, and prove the real one is untouched.

    The watch resolves its seat, stream and pointers under the crew home, so a
    case that reached the operator's home would corrupt a live fleet's view
    rather than fail here. The receipt is taken on the real stream's own
    identity, before and after.
    """
    real_stream = runs.watch_stream_path(PROJECT)
    before = _fingerprint(real_stream)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    assert runs.crew_home().is_relative_to(tmp_path)
    assert _fingerprint(real_stream) == before


def _fingerprint(path: Path) -> tuple[bool, int, int] | None:
    """A file's identity, or None when it is absent."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (True, stat.st_size, stat.st_mtime_ns)


def _seat_record(*, host: str) -> dict[str, Any]:
    """One producer seat record naming the host that issued it."""
    return {
        "project": PROJECT,
        "host": host,
        "pid": 4134058,
        "pid_start_time": 3_400_000.0,
        "stall_window": "30s",
        "started_at": "2026-10-13T03:47:50Z",
    }


def _read_seat() -> dict[str, Any]:
    with runs.watch_lock_path(PROJECT).open("rb") as handle:
        return runs._read_watch_record(handle)


def _hold_seat(*, host: str):
    """Write a seat record and hold its advisory lock, as a live producer does.

    The lock is the file lock the seat is claimed with, so a probe from this
    process conflicts on it exactly as a foreign producer's lock would. The
    returned handle stays open for as long as the seat is held.
    """
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    runs._write_watch_record(handle, _seat_record(host=host))
    return handle


def _release_seat(handle) -> None:
    """Drop the advisory lock and close, as a producer that has exited does."""
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _stream_path() -> Path:
    return runs.watch_stream_path(PROJECT)


def _install_transition_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the first sweep's transition read, drop only its background thread.

    Taking the seat runs this producer's first stream sweep, which reads the
    seed the seat's own ticker then takes. A case that stubbed the sweep away
    wholesale would leave the ticker with no seed and could not tell a fixed
    ticker from a broken one.
    """

    def sweep(project: str, records) -> None:
        runs._publish_watch_transitions(
            project, runs._WATCH_STREAM_PRODUCERS[project], records
        )

    monkeypatch.setattr(runs, "_publish_watch_stream", sweep)


def _write_stream(*lines: str) -> Path:
    path = _stream_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _append_stream(line: str) -> None:
    with _stream_path().open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def _make_stream_stale() -> None:
    """Age the stream past the seat's stall window."""
    past = NOW.timestamp() - 3600
    os.utime(_stream_path(), (past, past))


def _row_line(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True)


def _row(
    run_id: str, *, to_state: str, from_state: str, at: datetime
) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "event": "transition" if from_state else "baseline",
        "observed_at": at.astimezone(UTC).isoformat(),
        "run_id": run_id,
        "node": run_id,
        "from_state": from_state or None,
        "to_state": to_state,
    }


def _write_pointer(run_id: str, *, phase: str, manifest_status: str) -> None:
    """One live pointer whose row reads the state its manifest supports."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: {manifest_status}\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    log = directory / "stream.jsonl"
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": run_id, "plan": "a-plan", "time_budget": "20m"},
        "phase": phase,
        "created_at": (NOW - timedelta(hours=2)).astimezone(UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(log),
        "process_alive": False,
        # Another host, so the classifier never probes this host's process table
        # for a pid this case did not create.
        "launcher_host": FOREIGN_HOST,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)


def _ticker(sleeper: Callable[[float], None]):
    return recovery_watch.watch_ticker(
        PROJECT, stall_window="30s", poll_interval=1.0, sleeper=sleeper
    )


class _StillWaitingError(Exception):
    """The watch was still re-reading the seat when the case stopped it."""


def test_a_live_foreign_seat_is_waited_out_not_restarted(home, monkeypatch) -> None:
    """A fresh foreign stream keeps the watch re-reading the seat, not exiting."""
    _install_transition_sweep(monkeypatch)
    interval_calls: list[float] = []
    seat = _hold_seat(host=FOREIGN_HOST)
    _write_stream(_row_line(_row("r-other", to_state="working", from_state="", at=NOW)))

    def sleeper(seconds: float) -> None:
        interval_calls.append(seconds)
        if len(interval_calls) >= 3:
            raise _StillWaitingError

    stream = _ticker(sleeper)
    try:
        with pytest.raises(_StillWaitingError):
            next(stream)
    finally:
        stream.close()
        _release_seat(seat)

    # Three or more re-read intervals elapsed with no row yielded and no seat
    # taken, so the unit has nothing to restart for.
    assert len(interval_calls) >= 3, interval_calls
    assert _read_seat()["host"] == FOREIGN_HOST


def test_a_stale_stream_takes_the_seat_and_seeds_after_the_wait(
    home, monkeypatch
) -> None:
    """Once the foreign stream is stale the watch takes the seat and publishes.

    The row appended while the watch was waiting must be part of the seed it
    publishes from: the seed is read after the seat is taken, so the run reads
    as a transition from the state that row named rather than as fresh
    inventory.
    """
    _install_transition_sweep(monkeypatch)
    run_id = "r-moved"
    _write_pointer(run_id, phase="complete", manifest_status="complete")
    seat = _hold_seat(host=FOREIGN_HOST)
    _write_stream(_row_line(_row(run_id, to_state="dispatched", from_state="", at=NOW)))
    calls: list[float] = []

    def sleeper(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 3:
            # The foreign producer dies: a row it wrote before it stopped is the
            # tail the seed must now read, its stream stops moving, and its seat
            # is released.
            _append_stream(
                _row_line(
                    _row(run_id, to_state="working", from_state="dispatched", at=NOW)
                )
            )
            _release_seat(seat)
            _make_stream_stale()

    stream = _ticker(sleeper)
    try:
        row = next(stream)
    finally:
        stream.close()

    assert row["event"] == "transition", row
    assert row["run_id"] == run_id, row
    assert row["from_state"] == "working", row
    assert _read_seat()["host"] == socket.gethostname()

    # Taking the seat ran this producer's first stream sweep, which appended its
    # own row for the run. The ticker's row and that appended row carry the same
    # transition because both were seeded by the one read the sweep took before
    # it appended: a ticker that read the stream itself would find this
    # producer's own row and announce the run as unchanged.
    published = [
        json.loads(line)
        for line in _stream_path().read_text().splitlines()
        if line.strip()
    ]
    moved = [item for item in published if item.get("run_id") == run_id]
    assert moved, published
    assert moved[-1]["event"] == "transition", moved
    assert moved[-1]["from_state"] == row["from_state"], (moved[-1], row)
    assert moved[-1]["to_state"] == row["to_state"], (moved[-1], row)


def test_a_seat_held_on_this_host_is_reported_as_before(home, monkeypatch) -> None:
    """A seat this host owns reads as a watcher-live row with no waiting."""
    _install_transition_sweep(monkeypatch)
    waited: list[float] = []
    seat = _hold_seat(host=socket.gethostname())
    _write_stream(_row_line(_row("r-other", to_state="working", from_state="", at=NOW)))

    def sleeper(seconds: float) -> None:
        waited.append(seconds)

    stream = _ticker(sleeper)
    try:
        row = next(stream)
        with pytest.raises(StopIteration):
            next(stream)
    finally:
        stream.close()
        _release_seat(seat)

    assert row["event"] == "watcher-live", row
    assert waited == [], waited


def test_the_watch_asks_the_shared_foreign_seat_judgement(home, monkeypatch) -> None:
    """The foreign-seat judgement is the shared one, not a copy composed here."""
    _install_transition_sweep(monkeypatch)
    asked: list[str] = []

    def spy(project: str) -> bool:
        asked.append(project)
        return False

    monkeypatch.setattr(recovery_watch, "seat_names_a_live_foreign_producer", spy)
    seat = _hold_seat(host=FOREIGN_HOST)
    _write_stream(_row_line(_row("r-other", to_state="working", from_state="", at=NOW)))

    stream = _ticker(lambda _seconds: None)
    try:
        row = next(stream)
    finally:
        stream.close()
        _release_seat(seat)

    assert asked == [PROJECT], asked
    assert row["event"] == "watcher-live", row
