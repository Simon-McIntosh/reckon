"""A follower keeps drawing after its producer returns, and records what it drew.

Reported on 15 November: one session's pane drew nothing for half an hour while
seven of its runs changed state, and the follower was alive the whole time. Its
main thread sat in ``StationSubscriber.wake``-loop, sleeping in its poll
interval. Nothing recorded what that follower emitted, so a lost row could be
blamed on the follower or on the reader and neither could be shown.

Two gates can swallow a follower's rows across the producer's return.

The first is the generation gate. The subscriber holds one connection to the
station store, and the store is node-local: when the node's runtime directory is
re-created -- an out-of-memory restart or a reboot takes it with it -- a fresh
station process opens a *new* database at the same path. The follower's handle
still points at the file that was unlinked, and ``PRAGMA``-style freshness never
arrives again: ``PRAGMA data_version`` does not move for a connection whose
database file was replaced, so ``generation_moved`` answers no for as long as
the follower lives.

The second is the place. A fresh store begins its own id space, so the rows it
writes carry ids below the cursor the follower reached in the store that is
gone. A read after that cursor returns nothing even once the follower is looking
at the right file.

The cases below drive one subscriber through a producer restart done the way the
station does it: a fresh station process takes the seat and writes to the store.
They assert the rows that come back, and that every row the follower emitted is
on the session-host log with a sequence number, its time, its run and its
transition, so a later report of a missing row can be placed on the follower or
on the reader.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import obligation_snapshot, station_store
from reckon.crew.station_subscriber import StationSubscriber

PROJECT = "follower-restart-proj"
SESSION = "s-follower-restart"
BASE = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)

# The station process each phase runs. A fresh interpreter is what makes the
# restart real: the store identity, the code and the connection are the ones a
# new station has, not this process's.
_STATION = r"""
import sys
from datetime import UTC, datetime, timedelta

from reckon.crew import station_store

root, project, session, minute, runs = sys.argv[1:6]
base = datetime(2026, 11, 15, 9, 0, tzinfo=UTC) + timedelta(minutes=int(minute))
store = station_store.StationStore(root)
store.heartbeat(now=base)
for offset, run in enumerate(runs.split(",")):
    at = base + timedelta(seconds=offset)
    payload = {
        "project": project,
        "event": "transition",
        "run_id": run,
        "node": run,
        "session": session,
        "from_state": "queued",
        "to_state": "working",
        "observed_at": at.isoformat(),
    }
    store.write_event(
        audience=session,
        at=at,
        project=project,
        run=run,
        kind="transition",
        from_state="queued",
        to_state="working",
        payload=payload,
    )
store.close()
"""


class _Clock:
    """The subscriber's own clock, so a row's window is read at its own time."""

    def __init__(self, at: datetime) -> None:
        self.now = at.timestamp()

    def __call__(self) -> float:
        return self.now


def _station(root: Path, *, minute: int, runs: list[str]) -> None:
    """Run one station process that writes ``runs`` to the store at ``root``."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            _STATION,
            str(root),
            PROJECT,
            SESSION,
            str(minute),
            ",".join(runs),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


class _OnePass:
    def __init__(self) -> None:
        self.passes = 0

    def is_set(self) -> bool:
        return self.passes >= 1


def _draw(
    subscriber: StationSubscriber, clock: _Clock, *, sink=None
) -> list[tuple[str, str]]:
    """One pass of the subscriber's own loop, as the follower drives it."""
    stop = _OnePass()

    def sleeper(_seconds: float) -> None:
        stop.passes += 1
        clock.now += 1.0

    return [
        (str(event.get("run_id") or ""), str(event.get("to_state") or ""))
        for event in subscriber.wake(
            stop=stop, interval=0.0, sleeper=sleeper, sink=sink
        )
    ]


def _emissions(path: Path) -> list[dict[str, object]]:
    """The follower's own record of what it emitted, as the session host holds it."""
    if not path.is_file():
        return []
    found: list[dict[str, object]] = []
    for line in path.read_text().splitlines():
        marker = line.find("follower-emission ")
        if marker < 0:
            continue
        found.append(json.loads(line[marker:].split(" ", 1)[1]))
    return found


def test_a_follower_keeps_drawing_after_its_producer_returns(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    log = tmp_path / "session-host" / "follower.log"
    log.parent.mkdir()
    monkeypatch.setenv("RECKON_SESSION_HOST_LOG_DIR", str(log.parent))

    clock = _Clock(BASE + timedelta(minutes=2))
    _station(root, minute=0, runs=["r-before-a", "r-before-b"])
    subscriber = StationSubscriber(
        station_store.StationStore(root), PROJECT, session=SESSION, clock=clock
    )

    with log.open("a") as handle, redirect_stderr(handle):
        first = _draw(subscriber, clock)
        assert first == [("r-before-a", "working"), ("r-before-b", "working")]

        # The producer is lost past the bound a pane judges it by: the store's
        # own beat is older than the staleness window, and the follower keeps
        # polling through it yielding nothing.
        beat = station_store.StationStore(root).station_value(
            station_store.HEARTBEAT_KEY
        )
        lost_at = BASE + timedelta(
            seconds=obligation_snapshot.STATION_STALE_SECONDS + 60
        )
        age = obligation_snapshot.station_heartbeat_age(beat, now=lost_at)
        assert age is not None
        assert age > obligation_snapshot.STATION_STALE_SECONDS
        clock.now = lost_at.timestamp()
        assert _draw(subscriber, clock) == []

        # A fresh station process takes the seat. The store it opens is a new
        # one at the same path, as a re-created runtime directory leaves it.
        shutil.rmtree(root)
        clock.now = (BASE + timedelta(minutes=30)).timestamp()
        _station(
            root,
            minute=30,
            runs=["r-after-1", "r-after-2", "r-after-3", "r-after-4", "r-after-5"],
        )
        later = _draw(subscriber, clock)

    assert later == [
        ("r-after-1", "working"),
        ("r-after-2", "working"),
        ("r-after-3", "working"),
        ("r-after-4", "working"),
        ("r-after-5", "working"),
    ]
    assert not {run for run, _state in later} & {run for run, _state in first}

    emitted = _emissions(log)
    assert [(str(row["run_id"]), str(row["to_state"])) for row in emitted] == (
        first + later
    )
    assert [str(row["from_state"]) for row in emitted] == ["queued"] * len(emitted)
    sequence = [int(row["seq"]) for row in emitted]
    assert sequence == sorted(sequence)
    assert len(set(sequence)) == len(sequence) == len(first + later)
    times = [datetime.fromisoformat(str(row["time"])) for row in emitted]
    assert times == sorted(times)
    assert times[0] == BASE
    assert times[-1] == BASE + timedelta(minutes=30, seconds=4)


def test_a_restart_on_the_same_store_draws_only_what_is_new(tmp_path: Path) -> None:
    """A fresh station that inherits the store continues the id space.

    This is the restart that is not a replacement: the store survives, so the
    rows written after it carry ids above the place this pane reached and the
    ordinary gate delivers them. Nothing is drawn twice and nothing is skipped.
    """
    root = tmp_path / "runtime"
    clock = _Clock(BASE + timedelta(minutes=2))
    _station(root, minute=0, runs=["r-before-a"])
    subscriber = StationSubscriber(
        station_store.StationStore(root), PROJECT, session=SESSION, clock=clock
    )

    assert _draw(subscriber, clock) == [("r-before-a", "working")]

    _station(
        root,
        minute=30,
        runs=["r-after-1", "r-after-2", "r-after-3", "r-after-4", "r-after-5"],
    )
    clock.now = (BASE + timedelta(minutes=30)).timestamp()

    assert _draw(subscriber, clock) == [
        ("r-after-1", "working"),
        ("r-after-2", "working"),
        ("r-after-3", "working"),
        ("r-after-4", "working"),
        ("r-after-5", "working"),
    ]
    assert _draw(subscriber, clock) == []


def test_a_new_store_numbers_after_the_day_files_it_inherits(tmp_path: Path) -> None:
    """A store whose database was lost numbers its rows above those already held.

    The counter lives in the database and the rows in the day files, so a store
    opened over surviving day files must open at the highest id they hold. It
    otherwise writes its first event at id 1 into a day file that already has
    one -- the station's own insert fails on the primary key, and a follower's
    cursor names a row it has already read.
    """
    root = tmp_path / "runtime"
    _station(root, minute=0, runs=["r-before-a", "r-before-b"])

    store = station_store.StationStore(root)
    held = [int(row["id"]) for row in store.events_for(project=PROJECT, audiences=())]
    assert held == [1, 2]
    store.close()

    (root / station_store.DATABASE_NAME).unlink()
    _station(root, minute=30, runs=["r-after-1"])

    reopened = station_store.StationStore(root)
    later = [
        int(row["id"])
        for row in reopened.events_for(project=PROJECT, audiences=())
        if row["run"] == "r-after-1"
    ]
    assert later == [3]
