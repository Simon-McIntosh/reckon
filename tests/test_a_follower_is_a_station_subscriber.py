"""A follower reads its session's rows from the station after a stored cursor.

The pane's place is a station-wide sequence number and the UTC day it belongs
to, held in the station store's own row. These cases drive the subscriber
directly, with events written through a second connection as the station's own
process writes them, so what is measured is the delivery path rather than the
station's folding. A coordinator row is held for the ticker's settle window, so
each case advances the subscriber's clock past that window and flushes before
reading what the pane received.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import station_store, ticker
from reckon.crew.station_subscriber import (
    SKIPPED_DAYS_TEXT,
    StationSubscriber,
    stale_heartbeat_line,
)

PROJECT = "proj"
SESSION = "session-a"
RUN = "r-one"

BASE = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class _Clock:
    """A clock the case advances, so a settle window is measured not waited out."""

    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else float(now)

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path) -> station_store.StationStore:
    return station_store.StationStore(tmp_path / "runtime")


def _event(run: str = RUN, **overrides):
    event = {
        "project": PROJECT,
        "event": "transition",
        "run_id": run,
        "node": run,
        "session": SESSION,
        "from_state": "blocked",
        "to_state": "completed_unpromoted",
        "observed_at": BASE.isoformat(),
    }
    event.update(overrides)
    return event


def _write(store, at: datetime, event=None, *, audience=SESSION):
    payload = event or _event(observed_at=at.isoformat())
    return store.write_event(
        audience=audience,
        at=at,
        project=PROJECT,
        run=str(payload.get("run_id") or ""),
        kind=str(payload.get("event") or "transition"),
        from_state=payload.get("from_state"),
        to_state=payload.get("to_state"),
        payload=payload,
    )


def _subscriber(store, clock: _Clock) -> StationSubscriber:
    return StationSubscriber(store, PROJECT, session=SESSION, clock=clock)


def _deliver(subscriber: StationSubscriber, clock: _Clock) -> list[dict]:
    """Every row the pane receives now: the poll, then the settle hold's release."""
    rows = list(subscriber.poll())
    clock.now += ticker.SETTLE_WINDOW + 1
    rows += list(subscriber.flush())
    return rows


def test_a_row_is_delivered_exactly_once_across_a_subscriber_restart(tmp_path) -> None:
    """The place is the store's, so a fresh process continues rather than replays.

    The row is a lift grant, which the pane draws every time it is fed: a run
    transition repeated after a restart would be suppressed by the pane's own
    memory of the state it shows, so only a row the memory does not suppress
    can show that the place itself survived.
    """
    store = _store(tmp_path)
    writer = _store(tmp_path)
    writer.heartbeat()
    _write(writer, BASE, _event(from_state="", to_state="lift-granted"))
    clock = _Clock()

    first = _subscriber(store, clock)
    delivered = _deliver(first, clock)
    assert [row["to_state"] for row in delivered] == ["lift-granted"]
    assert first.cursor > 0

    restarted = _subscriber(store, clock)
    assert _deliver(restarted, clock) == [], "a restart must not redeliver the row"
    assert restarted.cursor == first.cursor


def test_a_re_armed_subscriber_draws_no_new_row_for_a_run_the_pane_already_drew(
    tmp_path,
) -> None:
    """The pane's memory survives the re-arm, so an unchanged run is not news."""
    store = _store(tmp_path)
    writer = _store(tmp_path)
    writer.heartbeat()
    _write(writer, BASE)
    clock = _Clock()

    first = _subscriber(store, clock)
    assert len(_deliver(first, clock)) == 1

    re_armed = _subscriber(store, clock)
    assert re_armed.path.reported.get(RUN) == "completed_unpromoted"
    # A later row that announces the same state the pane already shows asks
    # nothing of the reader.
    _write(
        writer,
        BASE + timedelta(minutes=1),
        _event(
            event="baseline",
            from_state=None,
            observed_at=(BASE + timedelta(minutes=1)).isoformat(),
        ),
    )
    assert _deliver(re_armed, clock) == []


def test_a_cursor_spanning_a_utc_rollover_delivers_every_event_exactly_once(
    tmp_path,
) -> None:
    """Events in two day files are read together and never twice."""
    store = _store(tmp_path)
    writer = _store(tmp_path)
    writer.heartbeat()
    before = datetime(2026, 10, 20, 23, 59, tzinfo=UTC)
    after = datetime(2026, 10, 21, 0, 1, tzinfo=UTC)
    _write(writer, before, _event(run="r-before", observed_at=before.isoformat()))
    _write(writer, after, _event(run="r-after", observed_at=after.isoformat()))
    assert len(writer.day_files()) == 2
    clock = _Clock()

    subscriber = _subscriber(store, clock)
    delivered = _deliver(subscriber, clock)
    assert [row["run_id"] for row in delivered] == ["r-before", "r-after"]
    assert subscriber.cursor_day == "2026-10-21"
    assert _deliver(subscriber, clock) == []


def test_a_cursor_older_than_retention_resumes_with_one_skip_line(tmp_path) -> None:
    """A place the store no longer holds resumes at its oldest event."""
    store = _store(tmp_path)
    writer = _store(tmp_path)
    writer.heartbeat()
    gone = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    kept = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    _write(writer, gone, _event(run="r-gone", observed_at=gone.isoformat()))
    _write(writer, kept, _event(run="r-kept", observed_at=kept.isoformat()))
    writer.day_files()[0].unlink()
    assert len(writer.day_files()) == 1

    store.write_cursor(
        SESSION, cursor=0, day=gone.date().isoformat(), at=datetime.now(tz=UTC)
    )
    clock = _Clock()
    subscriber = _subscriber(store, clock)
    delivered = _deliver(subscriber, clock)

    assert delivered[0]["event"] == "station-gap"
    assert delivered[0]["skipped_days"] == 16
    assert delivered[0]["line"] == SKIPPED_DAYS_TEXT.format(days=16)
    assert [row["run_id"] for row in delivered[1:]] == ["r-kept"]
    assert _deliver(subscriber, clock) == [], "the skip line is not repeated"


def test_a_stale_heartbeat_prints_one_line(tmp_path) -> None:
    """One line, once, naming the command that restarts the station."""
    store = _store(tmp_path)
    store.heartbeat(now=datetime.now(tz=UTC) - timedelta(seconds=600))

    line = stale_heartbeat_line(store)
    assert line is not None
    assert "reckon crew station" in line

    subscriber = _subscriber(store, _Clock())
    stop = threading.Event()
    received: list[dict] = []

    def reader() -> None:
        received.extend(subscriber.wake(stop=stop, interval=0.001))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not received:
        time.sleep(0.005)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert [event["event"] for event in received] == ["station-stale"]


def test_a_subscriber_wakes_on_a_day_file_event_and_on_nothing_else(tmp_path) -> None:
    """With the heartbeat silent, only a committed event row wakes the pane.

    The heartbeat is never written, so nothing but an event can move the
    generation the subscriber waits on, and the pane's own cursor writes are
    commits to the main database it must not read as news. The row is a lift
    grant, which the pane draws on arrival: a run transition is held for the
    settle window, and this case is about what wakes the loop rather than about
    when a held row is released.
    """
    store = _store(tmp_path)
    writer = _store(tmp_path)
    clock = _Clock()

    subscriber = _subscriber(store, clock)
    stop = threading.Event()
    received: list[dict] = []

    def reader() -> None:
        received.extend(subscriber.wake(stop=stop, interval=0.005))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        time.sleep(0.2)
        assert received == [], "an idle store wakes the pane with nothing"

        _write(writer, BASE, _event(from_state="", to_state="lift-granted"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not received:
            time.sleep(0.01)
        assert [row["run_id"] for row in received] == [RUN]

        # The subscriber's own place and checkpoint are committed to the main
        # database after that row, and neither is an event to draw again.
        time.sleep(0.3)
        assert len(received) == 1, "a cursor write reads as a row of its own"
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_registration_is_the_subscribers_own_row(tmp_path) -> None:
    """Attaching and detaching are writes to the pane's cursor row, not to GPFS."""
    store = _store(tmp_path)
    subscriber = _subscriber(store, _Clock())
    subscriber.register(pid=4321, host="node-a", delivery="stream", scope={"runs": []})
    row = store.read_cursor(SESSION)
    assert row["registration"]["pid"] == 4321
    assert row["registration"]["delivery"] == "stream"

    subscriber.detach()
    assert store.read_cursor(SESSION)["registration"] is None
