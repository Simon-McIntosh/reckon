"""A replacement producer resumes from the states its predecessor published.

A producer restarts on every reload and keeps no memory of the run states its
predecessor put on the stream. On the first poll it therefore treats every live
run as new inventory and draws a baseline row, which is not the news a session
wakes on: a run that finished while the seat was down sits idle until something
else moves it. These cases hold the contract — the replacement seeds its
first-poll memory from each live run's last published state, so a run whose
state changed across the reload is delivered as a transition from that state,
while a run the stream never named still reads as a baseline. A run that
stands where the stream last published it draws no row on the appended stream,
which already carries its state, while the seat's own ticker still emits it as
inventory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs

PROJECT = "a-replacement-producer"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every case at a temporary crew home, and prove the real one is untouched.

    A watcher writes its registration, its pointers and its stream under the
    crew home it resolves, so a case that reached the operator's home would
    corrupt a live fleet's view rather than fail here. The receipt is taken on
    the real stream's own identity, before and after, because a filesystem
    assertion inside the case would pass while a peer session's producer was the
    thing that moved.
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


def _write_pointer(
    run_id: str,
    *,
    phase: str,
    manifest_status: str,
    process_alive: bool,
    created_at: datetime,
) -> dict[str, Any]:
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
        "created_at": created_at.astimezone(UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(log),
        "process_alive": process_alive,
        # Another host, so the classifier never probes this host's process table
        # for a pid this case did not create.
        "launcher_host": "a-host-that-is-not-this-one",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _row(
    run_id: str, *, to_state: str, from_state: str, at: datetime
) -> dict[str, Any]:
    """One stream transition record, in the shape the producer appends."""
    return {
        "project": PROJECT,
        "event": "transition" if from_state else "baseline",
        "observed_at": at.astimezone(UTC).isoformat(),
        "run_id": run_id,
        "node": run_id,
        "from_state": from_state or None,
        "to_state": to_state,
    }


def _write_stream(*lines: str) -> Path:
    """Replace the project's stream with exactly these lines."""
    path = runs.watch_stream_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _row_line(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True)


def _snapshot_state(pointer: dict[str, Any]) -> str:
    """The state word the ticker derives for one pointer, as the row carries it."""
    return str(
        recovery._watch_snapshot(
            pointer, moment=recovery._utc_seconds(), stall_seconds=3600
        )["state"]
    )


class _NoRowError(Exception):
    """The watcher reached a sleep with nothing to publish, when a row was owed."""


def _first_row() -> dict[str, Any]:
    """The first row a replacement producer yields, then close its seat."""

    def sleeper(_seconds: float) -> None:
        raise _NoRowError(
            "the replacement producer published nothing on its first poll"
        )

    stream = recovery.watch_follow(
        PROJECT, transitions=True, poll_interval=0, sleeper=sleeper
    )
    try:
        return next(stream)
    finally:
        stream.close()


NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=UTC)


def test_a_run_that_moved_across_the_reload_is_a_transition(home) -> None:
    """A run the stream published as working, now complete, wakes as a transition."""
    run_id = "r-moved"
    created = NOW - timedelta(hours=2)
    pointer = _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=created,
    )
    _write_stream(
        _row_line(
            _row(
                run_id,
                to_state="working",
                from_state="dispatched",
                at=NOW - timedelta(hours=1),
            )
        )
    )

    completed = _snapshot_state(pointer)
    assert completed != "working"

    row = _first_row()

    assert row["event"] == "transition", row
    assert row["run_id"] == run_id, row
    assert row["from_state"] == "working", row
    assert row["to_state"] == completed, row


def test_a_run_the_stream_never_named_is_a_baseline(home) -> None:
    """A run absent from the stream reads as a baseline with no from-state."""
    run_id = "r-unseen"
    _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=NOW - timedelta(hours=2),
    )
    # The stream names only another run, so this one was never published.
    _write_stream(
        _row_line(_row("r-someone-else", to_state="working", from_state="", at=NOW))
    )

    row = _first_row()

    assert row["event"] == "baseline", row
    assert row["run_id"] == run_id, row
    assert row["from_state"] is None, row


def test_a_run_standing_where_it_last_did_keeps_its_baseline(home) -> None:
    """A run whose state equals its last published state reads as a baseline."""
    run_id = "r-still"
    pointer = _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=NOW - timedelta(hours=2),
    )
    standing = _snapshot_state(pointer)
    _write_stream(
        _row_line(_row(run_id, to_state=standing, from_state="dispatched", at=NOW))
    )

    row = _first_row()

    assert row["event"] == "baseline", row
    assert row["run_id"] == run_id, row
    assert row["from_state"] is None, row
    assert row["to_state"] == standing, row


def test_the_seed_stops_before_a_malformed_head_it_never_needs(home) -> None:
    """The seed reads backwards and stops once every live run is found.

    The stream opens with a line that cannot be read. A seed that walked the
    stream forwards would reach it; reading backwards from the end stops as soon
    as every live run is named, so the malformed line is never reached and the
    state it follows is still recovered from the rows near the end.
    """
    run_id = "r-tail"
    _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=NOW - timedelta(hours=2),
    )
    _write_stream(
        "{ this opening line is not readable",
        _row_line(
            _row(
                run_id,
                to_state="working",
                from_state="dispatched",
                at=NOW - timedelta(minutes=30),
            )
        ),
    )

    row = _first_row()

    assert row["event"] == "transition", row
    assert row["from_state"] == "working", row


def _sweep_rows() -> list[dict[str, Any]]:
    """Drive one production first sweep and return the rows the stream holds.

    This is the path a session's follower reads and a coordinator wakes on: the
    producer registered for the project with no memory, as a replacement has,
    then a live listing, which is what appends to the project's event stream.
    The sweep runs off the transition path, so the thread it starts is joined
    before the rows are read rather than left to outlive the case.
    """
    producer = runs._WatchStreamProducer(
        path=runs.watch_stream_path(PROJECT), known={}, stall_window="1h"
    )
    runs._WATCH_STREAM_PRODUCERS[PROJECT] = producer
    try:
        runs.list_live(project=PROJECT)
        if producer.sweep_thread is not None:
            producer.sweep_thread.join(timeout=10)
    finally:
        runs._WATCH_STREAM_PRODUCERS.pop(PROJECT, None)
    return list(runs.read_stream_events(producer.path))


def _appended_row(run_id: str, *, before: int) -> dict[str, Any]:
    """The row one sweep appended for a run, past the rows already published."""
    rows = _sweep_rows()
    assert len(rows) > before, rows
    appended = [row for row in rows[before:] if row.get("run_id") == run_id]
    assert appended, rows
    return appended[-1]


def test_the_sweep_appends_a_transition_for_a_run_that_moved(home) -> None:
    """The stream a follower reads carries the transition, not a baseline."""
    run_id = "r-swept-moved"
    pointer = _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=NOW - timedelta(hours=2),
    )
    _write_stream(
        _row_line(
            _row(
                run_id,
                to_state="working",
                from_state="dispatched",
                at=NOW - timedelta(hours=1),
            )
        )
    )

    completed = _snapshot_state(pointer)
    assert completed != "working"

    row = _appended_row(run_id, before=1)

    assert row["event"] == "transition", row
    assert row["from_state"] == "working", row
    assert row["to_state"] == completed, row


def test_the_sweep_appends_a_baseline_for_a_run_the_stream_never_named(home) -> None:
    """A run absent from the stream is inventory on the stream too."""
    run_id = "r-swept-unseen"
    _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=NOW - timedelta(hours=2),
    )
    # The stream names only another run, so this one was never published.
    _write_stream(
        _row_line(
            _row("r-swept-someone-else", to_state="working", from_state="", at=NOW)
        )
    )

    row = _appended_row(run_id, before=1)

    assert row["event"] == "baseline", row
    assert row["from_state"] is None, row


def test_the_sweep_appends_nothing_for_a_run_standing_where_it_last_did(
    home,
) -> None:
    """A run the stream already published at its current state draws no row.

    The stream carries the row for that state, so a fresh baseline here would
    draw the run again with no change between the rows; the sweep leaves it out.
    """
    run_id = "r-swept-still"
    pointer = _write_pointer(
        run_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=NOW - timedelta(hours=2),
    )
    standing = _snapshot_state(pointer)
    _write_stream(
        _row_line(_row(run_id, to_state=standing, from_state="dispatched", at=NOW))
    )

    rows = _sweep_rows()

    assert len(rows) == 1, rows
    assert rows[0]["run_id"] == run_id, rows
    assert rows[0]["to_state"] == standing, rows
