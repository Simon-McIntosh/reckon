"""A station working through a slow tick is not a producer that was lost.

Reported on 15 November: a pane printed ``producer reckon lost: snapshot age
167-364s exceeds 120s; re-arm with: reckon crew watch --ensure`` in the same
minute the obligations hook reported the station as alive but slow, three
minutes into a per-project-refresh tick. The producer process never died, and
running the remedy changed nothing, so both the row and its remedy were wrong.

Two reads of one station diverged. The hook reads the station store and sees a
fresh beat beside a tick that has outlived the window. The pane fell back to the
exported snapshot, which carries the heartbeat and no tick stage, so its beat is
stale by construction and the tick that explains it is invisible.

These cases drive the pane's own producer-health check over that state: one slow
row naming the stage and duration with no remedy, no repeat while the same tick
runs, one recovery row when it ends, and a genuine loss still a loss. Each
fixture asserts the reading it builds is the one under test -- the fallback was
taken, the store's verdict is slow or stopped -- so an empty or missing read
cannot pass for the behaviour being claimed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, station_store
from reckon.crew.watch_unit import ProducerSnapshotNotices, watcher_ensure_line

PROJECT = "slow-tick-fixture"
SESSION = "slow-tick-session"
INSTANT = datetime(2026, 11, 15, 12, 20, tzinfo=UTC)
BOUND = obligation_snapshot.FRESHNESS_WINDOW_SECONDS
SAMPLE = obligation_snapshot.STAT_IDENTITY_INTERVAL_SECONDS
STAGE = "per-project-refresh"
TICK_RUNNING_SECONDS = 180
SLOW_SPAN = "3m0s"


class Clock:
    """The pane's monotonic clock, which the fixture's wall clock follows."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Clock:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    reading = Clock()
    monkeypatch.setattr(
        obligation_snapshot, "_utc_now", lambda: INSTANT + timedelta(seconds=reading())
    )
    return reading


def _stamp(clock: Clock, offset: float) -> str:
    """One fixture instant, as the store and the export write it."""
    return (INSTANT + timedelta(seconds=clock() + offset)).isoformat()


def _store(tmp_path: Path) -> station_store.StationStore:
    store = station_store.StationStore(tmp_path / "runtime")
    store.connect()
    return store


def _markers(
    store: station_store.StationStore,
    *,
    beat: str,
    stage: str = STAGE,
    started_at: str,
    finished_at: str,
) -> None:
    store.set_station_value(obligation_snapshot.STATION_TICK_STAGE_KEY, stage)
    store.set_station_value(obligation_snapshot.STATION_TICK_STARTED_AT_KEY, started_at)
    store.set_station_value(
        obligation_snapshot.STATION_TICK_FINISHED_AT_KEY, finished_at
    )
    store.set_station_value("heartbeat", beat)


def _stale_export(*, beat_age: float) -> Path:
    """The export a fallback reads: a beat, no tick stage, one session document."""
    path = obligation_snapshot.station_snapshot_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "station": {
                    "heartbeat": (INSTANT - timedelta(seconds=beat_age)).isoformat()
                },
                "sessions": {
                    SESSION: {
                        "project": PROJECT,
                        "computed_at": (
                            INSTANT - timedelta(seconds=beat_age)
                        ).isoformat(),
                        "obligations": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _pane_snapshot(*, age: float = 200.0) -> None:
    """The pane's own producer snapshot, past its freshness window."""
    obligation_snapshot.write_snapshot(
        PROJECT,
        SESSION,
        {
            "project": PROJECT,
            "session": SESSION,
            "computed_at": (INSTANT - timedelta(seconds=age)).isoformat(),
        },
    )


def _assert_fell_back() -> None:
    """Prove the pane's read took the fallback this node repairs.

    The reproduction recorded for this fixture is a live station store that
    cannot answer the session's document, so the answer comes from the export.
    Asserted rather than assumed: a station-sourced answer would exercise a
    different branch and would leave the defect these cases measure untested.
    """
    answer = obligation_snapshot.read_station_answer(PROJECT, SESSION)
    assert answer is not None, "the fixture published no answer at all"
    assert answer["source"] == "snapshot", answer
    assert answer["stale"] is True, answer


def _slow_tick_fixture(clock: Clock, tmp_path: Path) -> station_store.StationStore:
    """A fresh beat, a tick in its third minute, a stale export, an aged snapshot."""
    store = _store(tmp_path)
    _markers(
        store,
        beat=_stamp(clock, 0),
        started_at=_stamp(clock, -TICK_RUNNING_SECONDS),
        finished_at=_stamp(clock, -TICK_RUNNING_SECONDS - 900),
    )
    _stale_export(beat_age=364)
    _pane_snapshot()
    return store


def test_a_slow_tick_draws_one_row_that_names_stage_and_duration(
    clock: Clock, tmp_path: Path
) -> None:
    """The pane says what the station is doing, and offers no remedy."""
    _slow_tick_fixture(clock, tmp_path)
    _assert_fell_back()

    rows = ProducerSnapshotNotices(PROJECT, SESSION, clock=clock).poll()

    assert [row["event"] for row in rows] == ["producer-slow"], rows
    row = rows[0]
    assert row["project"] == PROJECT and row["session"] == SESSION
    assert row["stage"] == STAGE
    assert row["age_seconds"] == TICK_RUNNING_SECONDS
    assert row["bound_seconds"] == obligation_snapshot.STATION_STALE_SECONDS
    assert STAGE in row["line"] and SLOW_SPAN in row["line"], row["line"]
    assert "remedy" not in row, row
    assert "re-arm" not in row["line"], row["line"]

    # The row is the store's own reading rather than a guess about it: the
    # station's beat is fresh and its tick is the one the fixture started.
    reading = obligation_snapshot.read_station_tick()
    assert reading is not None and reading["state"] == obligation_snapshot.STATION_SLOW
    assert reading["tick_stage"] == STAGE, reading
    assert reading["tick_age_seconds"] == TICK_RUNNING_SECONDS, reading


def test_the_same_tick_is_reported_once(clock: Clock, tmp_path: Path) -> None:
    """Sampling inside the tick draws nothing, however long it runs."""
    _slow_tick_fixture(clock, tmp_path)
    check = ProducerSnapshotNotices(PROJECT, SESSION, clock=clock)

    assert [row["event"] for row in check.poll()] == ["producer-slow"]
    for _ in range(2):
        clock.value += SAMPLE + 1
        assert check.poll() == []

    # The store's own reading is what the check read past its sampling
    # interval, so silence above is the dedupe rather than a missed sample.
    assert obligation_snapshot.read_station_tick()["tick_age_seconds"] > (
        TICK_RUNNING_SECONDS
    )


def test_the_tick_finishing_draws_one_recovery_row(
    clock: Clock, tmp_path: Path
) -> None:
    """The slow stretch ends visibly, once."""
    store = _slow_tick_fixture(clock, tmp_path)
    check = ProducerSnapshotNotices(PROJECT, SESSION, clock=clock)
    assert [row["event"] for row in check.poll()] == ["producer-slow"]

    clock.value += SAMPLE + 1
    assert check.poll() == []
    clock.value += SAMPLE + 1
    _markers(
        store,
        beat=_stamp(clock, 0),
        started_at=_stamp(clock, -TICK_RUNNING_SECONDS),
        finished_at=_stamp(clock, 0),
    )
    recovered = check.poll()

    assert [row["event"] for row in recovered] == ["producer-restored"], recovered
    assert recovered[0]["project"] == PROJECT
    assert recovered[0]["age_seconds"] == 0, recovered[0]
    assert "remedy" not in recovered[0], recovered[0]
    assert check.poll() == []


def test_a_stale_beat_with_no_tick_running_is_still_a_loss(
    clock: Clock, tmp_path: Path
) -> None:
    """The reading added for the slow tick must not swallow a real loss."""
    store = _store(tmp_path)
    _markers(
        store,
        beat=_stamp(clock, -400),
        started_at=_stamp(clock, -2000),
        finished_at=_stamp(clock, -1900),
        stage="publish",
    )
    _stale_export(beat_age=364)
    _pane_snapshot()

    rows = ProducerSnapshotNotices(PROJECT, SESSION, clock=clock).poll()

    assert [row["event"] for row in rows] == ["producer-lost"], rows
    row = rows[0]
    assert row["remedy"] == watcher_ensure_line(PROJECT), row
    assert row["remedy"] in row["line"], row["line"]

    # The loss is the store's own verdict rather than the export's: the beat
    # is stale and no tick is running, so nothing here reads as a slow tick.
    reading = obligation_snapshot.read_station_tick()
    assert (
        reading is not None and reading["state"] == obligation_snapshot.STATION_STOPPED
    )
    assert reading["tick_age_seconds"] is None, reading
