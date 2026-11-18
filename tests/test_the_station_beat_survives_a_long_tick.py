"""A heartbeat stays fresh while a tick holds the database and publication."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import obligation_snapshot as snapshots
from reckon.crew import station, station_obligations, station_store
from reckon.crew.watch_unit import ProducerSnapshotNotices, watcher_ensure_line

PROJECT = "contended-station"
SESSION = "contended-session"
START = datetime(2026, 11, 16, tzinfo=UTC)


def _until(predicate, message):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(message)


@pytest.mark.parametrize("held", ["store", "home", "store-and-home"])
def test_a_long_tick_cannot_hold_the_local_beat(held, tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    home = tmp_path / "config"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    monkeypatch.setenv("RECKON_HOME", str(home))
    clock = [START]
    monkeypatch.setattr(snapshots, "_utc_now", lambda: clock[0])
    home_lock = threading.Lock()
    attempted = threading.Event()
    published = threading.Event()
    publish = snapshots.publish_station_reading

    def slow_publication(markers, *, home=None):
        attempted.set()
        with home_lock:
            result = publish(markers, home=home)
        published.set()
        return result

    monkeypatch.setattr(snapshots, "publish_station_reading", slow_publication)
    with station_store.StationStore(root) as store:
        producer = station.Station(store, clock=lambda: clock[0])
        producer._enter("per-project-refresh", started=START)
        store.heartbeat(now=START)
        store.write_obligation(
            SESSION,
            {"document": {"project": PROJECT, "session": SESSION, "obligations": []}},
            changed_at=START,
        )
        station_obligations.export_snapshot(
            store, project=PROJECT, home=producer.home, now=START, force=True
        )
        export = snapshots.station_snapshot_path(PROJECT, home=producer.home)
        original = export.read_bytes()
        foreign = tmp_path / "foreign-runtime"
        foreign.mkdir()

        def remote_advanced():
            reading = snapshots.read_station_answer(
                PROJECT, SESSION, environ={"XDG_RUNTIME_DIR": str(foreign)}
            )
            return reading is not None and reading["heartbeat_age_seconds"] == 0

        beat = station.StationBeat(root, beat_seconds=0.01, clock=lambda: clock[0])
        beat.start()
        threads = (beat._thread, beat._publisher)
        try:
            assert published.wait(3), "positive control never published a beat"
            initial = snapshots.read_station_answer(PROJECT, SESSION)
            assert initial is not None and initial["heartbeat_age_seconds"] == 0
            if "home" in held:
                home_lock.acquire()
                attempted.clear()
                assert attempted.wait(3), "publication was never held"
            if "store" in held:
                store.connect().execute("BEGIN IMMEDIATE")
                store.connect().execute(
                    "UPDATE station SET value = value WHERE key = ?", ("heartbeat",)
                )
            notices = ProducerSnapshotNotices(PROJECT, SESSION, clock=lambda: 0)
            for window in range(3):
                clock[0] += timedelta(seconds=snapshots.STATION_STALE_SECONDS + 1)

                def advanced():
                    reading = snapshots.read_station_answer(PROJECT, SESSION)
                    return reading is not None and reading["heartbeat_age_seconds"] == 0

                _until(
                    advanced, f"heartbeat went stale behind {held} in window {window}"
                )
                reading = snapshots.read_station_tick()
                assert (
                    reading is not None and reading["state"] == snapshots.STATION_SLOW
                )
                rows = notices.poll()
                assert all(row["event"] != "producer-lost" for row in rows), rows
                assert all("remedy" not in row for row in rows), rows
                notices.sampled_at = None
                if held == "store":
                    _until(
                        remote_advanced, "database contention held the off-node beat"
                    )
        finally:
            store.connect().rollback()
            if home_lock.locked():
                home_lock.release()
            try:
                _until(remote_advanced, "publication did not catch up after release")
            finally:
                beat.stop()
        assert beat._thread is None
        assert export.read_bytes() == original, "a beat rewrote the obligation export"
        assert all(thread is not None and not thread.is_alive() for thread in threads)
        path = root / snapshots.STATION_HEARTBEAT_NAME
        current = path.read_bytes()
        markers = json.loads(current)
        for damage in (
            {**markers, "store_identity": "another-store"},
            {**markers, "heartbeat": "unreadable"},
            {**markers, "heartbeat": (START - timedelta(seconds=1)).isoformat()},
            [],
            "{",
        ):
            path.write_text(damage if isinstance(damage, str) else json.dumps(damage))
            reading = snapshots.read_station_tick()
            assert reading is not None and reading["state"] == snapshots.STATION_STOPPED
        path.write_bytes(current)
        guard = station_store.StoreWriteGuard()
        guard.mark_lost("the station lease belongs to a successor")
        beat.guard = guard
        with pytest.raises(station_store.StationLeaseLostError, match="successor"):
            beat.beat_once()
        assert path.read_bytes() == current, "a fenced station wrote a heartbeat"
        clock[0] += timedelta(seconds=snapshots.STATION_STALE_SECONDS + 1)
        stopped = snapshots.read_station_answer(PROJECT, SESSION)
        assert stopped is not None and stopped["state"] == snapshots.STATION_STOPPED
        rows = ProducerSnapshotNotices(PROJECT, SESSION).poll()
        assert [row["event"] for row in rows] == ["producer-lost"], rows
        assert rows[0]["remedy"] == watcher_ensure_line(PROJECT)


def test_stop_waits_for_its_blocked_publisher(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    beat = station.StationBeat(tmp_path, beat_seconds=0.01)
    monkeypatch.setattr(beat, "_local_beat", lambda: ({}, None))

    def publish(*_args, **_kwargs):
        entered.set()
        assert release.wait(10), "the test did not release publication"

    monkeypatch.setattr(snapshots, "publish_station_reading", publish)
    beat.start()
    threads = (beat._thread, beat._publisher)

    def stop():
        beat.stop()
        returned.set()

    stopper = threading.Thread(target=stop)
    try:
        assert entered.wait(3), "publication never started"
        stopper.start()
        assert not returned.wait(1.5), "stop returned while its publisher was blocked"
        assert beat._publisher is threads[1], "stop discarded its live publisher"
    finally:
        release.set()
        if stopper.ident is not None:
            stopper.join(3)
        else:
            beat.stop()
    assert returned.is_set()
    assert all(thread is not None and not thread.is_alive() for thread in threads)
    assert beat._thread is None and beat._publisher is None
