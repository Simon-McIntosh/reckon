"""A pane that does not hold its session's claim still draws each row once.

Two followers of one session share the session's stored place in the station,
and only the one holding the session's registration writes it. The other read
from that stored place on every poll, so each time the station committed it
redrew every row since it attached: one halted row was drawn fifteen times on
7 November. The reading position now advances in memory whether or not the pane
may write the stored place.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import station_store, ticker
from reckon.crew.station_subscriber import StationSubscriber

PROJECT = "proj"
SESSION = "session-a"
BASE = datetime(2026, 11, 7, 21, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _write(store, at: datetime, *, run: str, to_state: str) -> None:
    payload = {
        "project": PROJECT,
        "event": "transition",
        "run_id": run,
        "node": run,
        "session": SESSION,
        "from_state": "",
        "to_state": to_state,
        "observed_at": at.isoformat(),
    }
    store.write_event(
        audience=SESSION,
        at=at,
        project=PROJECT,
        run=run,
        kind="transition",
        from_state="",
        to_state=to_state,
        payload=payload,
    )


def _drawn(subscriber: StationSubscriber, clock: _Clock) -> list[tuple[str, str]]:
    rows = list(subscriber.poll())
    clock.now += ticker.SETTLE_WINDOW + 1
    rows += list(subscriber.flush())
    return [(str(row.get("run_id")), str(row.get("to_state"))) for row in rows]


def test_a_pane_that_may_not_write_its_place_draws_each_row_once(
    tmp_path: Path,
) -> None:
    writer = station_store.StationStore(tmp_path / "runtime")
    writer.heartbeat()
    clock = _Clock()
    pane = StationSubscriber(
        station_store.StationStore(tmp_path / "runtime"),
        PROJECT,
        session=SESSION,
        clock=clock,
    )
    pane.persist = False

    _write(writer, BASE, run="r-held", to_state="lift-granted")
    first = _drawn(pane, clock)
    _write(writer, BASE + timedelta(minutes=5), run="r-next", to_state="lift-granted")
    second = _drawn(pane, clock)
    third = _drawn(pane, clock)

    assert first == [("r-held", "lift-granted")]
    assert second == [("r-next", "lift-granted")], "the earlier row was drawn again"
    assert third == []
    assert pane.cursor == 0, "a pane without the claim never writes the stored place"


def test_a_pane_that_holds_its_place_still_writes_it(tmp_path: Path) -> None:
    writer = station_store.StationStore(tmp_path / "runtime")
    writer.heartbeat()
    clock = _Clock()
    pane = StationSubscriber(
        station_store.StationStore(tmp_path / "runtime"),
        PROJECT,
        session=SESSION,
        clock=clock,
    )

    _write(writer, BASE, run="r-held", to_state="lift-granted")
    assert _drawn(pane, clock) == [("r-held", "lift-granted")]
    assert pane.cursor > 0
