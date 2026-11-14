"""A producer's completed reload is not reported as a stop row.

The producer stamps ``reload_started_at`` into its seat record just before it
replaces its own image, and the replacement clears the marker by rewriting the
record. A poll landing in that window reads the record mid-write and finds no
producer, so the follower reported a reload failure for a reload that
completed: measured on 2026-10-21, three such rows for three reloads whose logs
each recorded ``completed its reload; publishing continues``. The window this
node consults is the one the stale-producer path already grants, and only an
overrun, or a reload the producer's own log records as failed, is news.
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from reckon import cli
from reckon.crew import runs

PROJECT = "reloadpane"


class _StopDrivingError(Exception):
    """End the follower once the follower has polled as long as the case needs."""


def _instant(seconds_ago: float) -> str:
    moment = datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _plant_seat(*, pid: int | None, host: str, reload_started_at: str | None) -> None:
    """Write the seat record a producer leaves while its image is replaced.

    A record naming another host is judged by its transition stream rather than
    by this host's process table, so it reads not-live while the stream is
    quiet -- what a poll inside an image replacement sees. The record keeps the
    marker this node reads. ``pid`` names this host's own process for a
    completed replacement, which reads live against the local table.
    """
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "project": PROJECT,
        "pid": pid,
        "host": host,
        "stall_window": runs.DEFAULT_WATCH_STALL_WINDOW,
        "started_at": _instant(3600),
        "stream_path": str(runs.watch_stream_path(PROJECT)),
        "log_path": str(runs.watch_log_path(PROJECT)),
        "reckon_version": "0.0.0",
    }
    if pid is not None:
        record["pid_start_time"] = runs._process_start_time(pid)
    if reload_started_at is not None:
        record["reload_started_at"] = reload_started_at
    path.write_text(json.dumps(record))


def _drive(*, on_sleep, polls: int = 4) -> list[dict]:
    """Run the follower for a bounded number of polls and collect its events."""
    events: list[dict] = []
    slept = {"count": 0}

    def sleeper(seconds: float) -> None:
        slept["count"] += 1
        on_sleep(slept["count"])
        if slept["count"] >= polls:
            raise _StopDrivingError

    with suppress(_StopDrivingError):
        events.extend(
            cli._follow_watch_lines(
                PROJECT, sleeper=sleeper, sweep=None, poll_interval=0.0
            )
        )
    return events


def _reload_rows(events: list[dict]) -> list[dict]:
    return [
        event
        for event in events
        if event["event"] == cli.FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT
    ]


def _isolated_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    for name in (
        "RECKON_SESSION_HOST_SETTLE",
        "RECKON_FOLLOWER_OWNER",
        "RECKON_FOLLOWER_IMAGE_COMMIT",
        "RECKON_FOLLOWER_SOURCE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_a_reload_that_completed_inside_the_window_prints_no_row(
    tmp_path, monkeypatch
) -> None:
    """The replacement finished before the window elapsed: no reload row."""
    _isolated_home(tmp_path, monkeypatch)
    _plant_seat(pid=None, host="peer-node", reload_started_at=_instant(0))

    def completed(poll: int) -> None:
        if poll == 1:
            # The replacement finished: it cleared the marker by rewriting the
            # record, and this host's own process now holds the seat.
            _plant_seat(
                pid=os.getpid(), host=socket.gethostname(), reload_started_at=None
            )

    events = _drive(on_sleep=completed)
    assert _reload_rows(events) == []


def test_a_reload_that_overruns_the_window_prints_exactly_one_row(
    tmp_path, monkeypatch
) -> None:
    """A reload still not back after the window is news, and printed once."""
    _isolated_home(tmp_path, monkeypatch)
    _plant_seat(pid=None, host="peer-node", reload_started_at=_instant(120))
    events = _drive(on_sleep=lambda poll: None)
    rows = _reload_rows(events)
    assert len(rows) == 1, rows
    assert "stopped during its reload" in rows[0]["line"]


def test_a_reload_the_producer_logs_as_failed_is_reported_at_once(
    tmp_path, monkeypatch
) -> None:
    """The producer's own failure line is news inside the window, too."""
    _isolated_home(tmp_path, monkeypatch)
    _plant_seat(pid=None, host="peer-node", reload_started_at=_instant(0))
    log = runs.watch_log_path(PROJECT)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "reckon crew watch is stale and could not reload itself "
        "(OSError: probe); cycle it with: reckon crew watch --ensure\n"
    )
    events = _drive(on_sleep=lambda poll: None)
    rows = _reload_rows(events)
    assert len(rows) == 1, rows
