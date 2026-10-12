"""A producer seated on another node reads as live through the watch seat.

The snapshot records the pid, its start time, the start instant and the code
stamp of the producer that wrote it, but no host, so a pid on a producer's own
node means nothing to a reader on another. The seat crosses the shared home, so
:func:`freshness` consults it: a project whose seat names a foreign host whose
stream is still fresh is a live producer, and a pid this host cannot see with no
such seat behind it is not. The local cases are kept so the foreign reading is
shown against the two it must not disturb -- a local live producer still reads
fresh, and a dead local pid still reads as no producer.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import UTC, datetime

from reckon.crew import obligation_snapshot
from reckon.crew.run_paths import watch_lock_path, watch_stream_path

PROJECT = "remote-seat-fixture"
FOREIGN_HOST = "another-node.invalid"
STALL_WINDOW = "10m"


def _write_seat(project: str, *, host: str) -> None:
    """Record a seat naming ``host`` as the project's producer's."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": project,
                "host": host,
                "pid": 1,
                "stall_window": STALL_WINDOW,
            }
        ),
        encoding="utf-8",
    )


def _touch_stream(project: str, *, age_seconds: float) -> None:
    """Create the seat's transition stream and stamp it ``age_seconds`` back."""
    path = watch_stream_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    written = time.time() - age_seconds
    os.utime(path, (written, written))


def _document(*, pid: int, start: str | None, stamp: str, computed_at: datetime):
    """A snapshot carrying one producer record, as a reader holds it."""
    return {
        "computed_at": obligation_snapshot._iso(computed_at),
        "producer": {
            "pid": pid,
            "pid_start_time": start,
            "started_at": None,
            "code_stamp": stamp,
        },
    }


def _dead_pid() -> int:
    """A pid that has finished, so this host's process table denies it."""
    finished = subprocess.Popen(["true"])
    finished.wait()
    return finished.pid


def test_a_foreign_seat_with_a_fresh_stream_reads_as_live() -> None:
    """A seat on another host whose stream is moving is a live producer."""
    now = datetime.now(tz=UTC)
    assert socket.gethostname() != FOREIGN_HOST
    _write_seat(PROJECT, host=FOREIGN_HOST)
    _touch_stream(PROJECT, age_seconds=0)
    document = _document(pid=_dead_pid(), start="0", stamp="stamp-one", computed_at=now)

    assert (
        obligation_snapshot.freshness(
            document, project=PROJECT, current_stamp="stamp-one"
        )
        == obligation_snapshot.FRESH
    )


def test_a_foreign_seat_with_a_stale_stream_reads_as_no_producer() -> None:
    """A foreign seat whose stream has stalled names no live producer."""
    now = datetime.now(tz=UTC)
    _write_seat(PROJECT, host=FOREIGN_HOST)
    _touch_stream(PROJECT, age_seconds=3600)
    document = _document(pid=_dead_pid(), start="0", stamp="stamp-one", computed_at=now)

    assert (
        obligation_snapshot.freshness(
            document, project=PROJECT, current_stamp="stamp-one"
        )
        == obligation_snapshot.NO_PRODUCER
    )


def test_a_dead_local_producer_with_no_foreign_seat_reads_as_no_producer() -> None:
    """A pid this host denies, with no foreign seat behind it, is absent."""
    now = datetime.now(tz=UTC)
    document = _document(pid=_dead_pid(), start="0", stamp="stamp-one", computed_at=now)

    assert (
        obligation_snapshot.freshness(
            document, project=PROJECT, current_stamp="stamp-one"
        )
        == obligation_snapshot.NO_PRODUCER
    )


def test_a_live_local_producer_still_reads_fresh() -> None:
    """The positive control: a pid this host carries the start time of is live."""
    now = datetime.now(tz=UTC)
    live = os.getpid()
    start = obligation_snapshot.process_start_time(live)
    assert start is not None
    document = _document(pid=live, start=start, stamp="stamp-one", computed_at=now)

    assert (
        obligation_snapshot.freshness(
            document, project=PROJECT, current_stamp="stamp-one"
        )
        == obligation_snapshot.FRESH
    )
