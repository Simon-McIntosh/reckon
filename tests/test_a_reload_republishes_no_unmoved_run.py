"""A replacement producer republishes no run that has not moved.

Every merge into main reloads the producer, and the replacement seeds its first
sweep from the stream's last published state for each live run. A run that moved
while the seat was down is a transition from that state. A run that has not
moved needs no row at all: the stream already carries its state, so a fresh
baseline would draw the run again with no change between the rows.

The producer's first sweep appends to the project's event stream. These cases
write a stream naming one run complete and another working, keep the first
complete and move the second to complete, run that first sweep and read the
rows it appended: the unchanged run draws nothing, the moved run draws a
transition from working, and a run the stream never named draws a baseline.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs

PROJECT = "a-reload-republishes-no-unmoved-run"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=UTC)


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
        "launcher_host": "a-host-that-is-not-this-one",
        "manifest_path": str(manifest),
        "log_path": str(log),
        "process_alive": process_alive,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _row(
    run_id: str, *, to_state: str, from_state: str, at: datetime
) -> dict[str, Any]:
    """One stream row, in the shape the producer appends."""
    return {
        "project": PROJECT,
        "event": "transition" if from_state else "baseline",
        "observed_at": at.astimezone(UTC).isoformat(),
        "run_id": run_id,
        "node": run_id,
        "from_state": from_state or None,
        "to_state": to_state,
    }


def _row_line(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True)


def _write_stream(*lines: str) -> Path:
    """Replace the project's stream with exactly these lines."""
    path = runs.watch_stream_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _snapshot_state(pointer: dict[str, Any]) -> str:
    """The state word the ticker derives for one pointer, as the row carries it."""
    return str(
        recovery._watch_snapshot(
            pointer, moment=recovery._utc_seconds(), stall_seconds=3600
        )["state"]
    )


def _sweep_rows() -> list[dict[str, Any]]:
    """Drive one production first sweep and return the rows the stream holds.

    This is the path the producer appends on: a producer registered for the
    project with no memory, as a replacement has, then a live listing, which is
    what appends to the project's event stream. The sweep runs off the
    transition path, so the thread it starts is joined before the rows are read
    rather than left to outlive the case.
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


def test_a_reload_republishes_only_the_run_that_moved(home) -> None:
    """An unchanged run draws no row; a moved run a transition; an unseen a baseline."""
    still_id = "r-still"
    moved_id = "r-moved"
    unseen_id = "r-unseen"
    created = NOW - timedelta(hours=2)

    still_pointer = _write_pointer(
        still_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=created,
    )
    moved_pointer = _write_pointer(
        moved_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=created,
    )
    _write_pointer(
        unseen_id,
        phase="complete",
        manifest_status="complete",
        process_alive=False,
        created_at=created,
    )

    still_state = _snapshot_state(still_pointer)
    moved_state = _snapshot_state(moved_pointer)
    assert moved_state != "working"

    # The stream's last word for the first run is its current state, so it has
    # not moved; for the second it is working, so it has. The third is absent.
    _write_stream(
        _row_line(
            _row(
                still_id,
                to_state=still_state,
                from_state="dispatched",
                at=NOW - timedelta(hours=1),
            )
        ),
        _row_line(
            _row(
                moved_id,
                to_state="working",
                from_state="dispatched",
                at=NOW - timedelta(minutes=50),
            )
        ),
    )

    rows = _sweep_rows()

    appended = rows[2:]
    assert [row for row in appended if row.get("run_id") == still_id] == [], rows

    moved = [row for row in appended if row.get("run_id") == moved_id]
    assert len(moved) == 1, rows
    assert moved[0]["event"] == "transition", moved
    assert moved[0]["from_state"] == "working", moved
    assert moved[0]["to_state"] == moved_state, moved

    unseen = [row for row in appended if row.get("run_id") == unseen_id]
    assert len(unseen) == 1, rows
    assert unseen[0]["event"] == "baseline", unseen
    assert unseen[0]["from_state"] is None, unseen
