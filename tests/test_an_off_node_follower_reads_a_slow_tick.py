"""Off-node station health advances independently of obligation derivation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot as snapshots
from reckon.crew import station, station_obligations, station_store
from reckon.crew.watch_unit import ProducerSnapshotNotices, watcher_ensure_line

PROJECT = "off-node-fixture"
SESSION = "off-node-session"
START = datetime(2026, 11, 16, 10, tzinfo=UTC)
STAGE = "per-project-refresh"


@pytest.fixture()
def reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "station-runtime"
    foreign = tmp_path / "follower-runtime"
    foreign.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(foreign))
    clock = [START]
    monkeypatch.setattr(snapshots, "_utc_now", lambda: clock[0])
    with station_store.StationStore(root) as store:
        producer = station.Station(store, clock=lambda: clock[0])
        producer._enter(STAGE, started=START)
        store.heartbeat(now=START)
        store.write_obligation(
            SESSION,
            {
                "document": {
                    "project": PROJECT,
                    "session": SESSION,
                    "computed_at": START.isoformat(),
                    "obligations": [],
                }
            },
            changed_at=START,
        )
        export = station_obligations.export_snapshot(
            store, project=PROJECT, home=producer.home, now=START, force=True
        )
        assert export is not None and export.is_file()
        assert snapshots.read_station_tick() is None, "follower can see the local store"
        assert snapshots.read_station_answer(PROJECT, SESSION)["source"] == "snapshot"
        beat = station.StationBeat(root, clock=lambda: clock[0])
        try:
            yield store, producer, beat, clock, export
        finally:
            beat.stop()


def _advance(beat, clock, seconds: float) -> None:
    clock[0] += timedelta(seconds=seconds)
    beat.beat_once()


def test_an_off_node_follower_reads_the_same_slow_tick_as_a_local_one(reading):
    store, _producer, beat, clock, export = reading
    original = export.read_bytes()
    for _ in range(3):
        _advance(beat, clock, snapshots.STATION_STALE_SECONDS + 1)
        assert store.station_value("heartbeat") == clock[0].isoformat()
    local = snapshots.read_station_answer(
        PROJECT, SESSION, environ={"XDG_RUNTIME_DIR": str(store.root)}
    )
    assert local is not None and local["state"] == snapshots.STATION_SLOW
    assert snapshots.read_station_tick() is None

    rows = ProducerSnapshotNotices(PROJECT, SESSION).poll()
    assert [row["event"] for row in rows] == ["producer-slow"], rows
    assert "remedy" not in rows[0] and "re-arm" not in rows[0]["line"], rows
    remote = snapshots.read_station_answer(PROJECT, SESSION)
    assert remote is not None and remote["source"] == "snapshot"
    assert remote["stale"] is False, remote
    for key in (
        "state",
        "heartbeat_age_seconds",
        "tick_stage",
        "tick_age_seconds",
        "tick_started_at",
    ):
        assert remote[key] == local[key], (key, remote, local)
    assert remote["tick_stage"] == STAGE
    assert remote["tick_age_seconds"] == 3 * (snapshots.STATION_STALE_SECONDS + 1)
    assert remote["payload"] == local["payload"]
    assert json.loads(export.read_text())["sessions"][SESSION]["computed_at"] == (
        START.isoformat()
    )
    assert export.read_bytes() == original, "a beat rewrote the obligation data"


def test_an_off_node_follower_observes_the_tick_finishing(reading):
    _store, producer, beat, clock, _export = reading
    _advance(beat, clock, snapshots.STATION_STALE_SECONDS * 3)
    monotonic = [0.0]
    notices = ProducerSnapshotNotices(PROJECT, SESSION, clock=lambda: monotonic[0])
    assert [row["event"] for row in notices.poll()] == ["producer-slow"]
    monotonic[0] += snapshots.STAT_IDENTITY_INTERVAL_SECONDS + 1
    assert notices.poll() == []

    producer.store.set_station_value(
        snapshots.STATION_TICK_FINISHED_AT_KEY, clock[0].isoformat()
    )
    _advance(beat, clock, 1)
    monotonic[0] += snapshots.STAT_IDENTITY_INTERVAL_SECONDS + 1
    rows = notices.poll()
    assert [row["event"] for row in rows] == ["producer-restored"], rows
    assert "remedy" not in rows[0]
    assert (
        snapshots.read_station_answer(PROJECT, SESSION)["state"]
        == snapshots.STATION_LIVE
    )


@pytest.mark.parametrize("off_node", [True, False])
def test_a_stopped_beat_still_draws_a_lost_row_with_its_remedy(
    reading, monkeypatch: pytest.MonkeyPatch, off_node: bool
):
    store, _producer, beat, clock, _export = reading
    _advance(beat, clock, snapshots.STATION_STALE_SECONDS * 3)
    clock[0] += timedelta(seconds=snapshots.STATION_STALE_SECONDS + 1)
    if not off_node:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(store.root))
    answer = snapshots.read_station_answer(PROJECT, SESSION)
    assert answer is not None
    assert answer["source"] == ("snapshot" if off_node else "station")
    assert answer["state"] == snapshots.STATION_STOPPED, answer
    rows = ProducerSnapshotNotices(PROJECT, SESSION).poll()
    assert [row["event"] for row in rows] == ["producer-lost"], rows
    assert rows[0]["remedy"] == watcher_ensure_line(PROJECT)
    assert rows[0]["remedy"] in rows[0]["line"]


def test_an_export_without_a_matching_health_record_keeps_its_own_beat(reading):
    _store, _producer, beat, clock, export = reading
    _advance(beat, clock, snapshots.STATION_STALE_SECONDS * 3)
    document = json.loads(export.read_text())
    document["station"]["host"] = "another-station-host.invalid"
    export.write_text(json.dumps(document))
    answer = snapshots.read_station_answer(PROJECT, SESSION)
    assert answer is not None and answer["stale"] is True, answer
    rows = ProducerSnapshotNotices(PROJECT, SESSION).poll()
    assert [row["event"] for row in rows] == ["producer-lost"], rows


@pytest.mark.parametrize(
    "damage",
    ["invalid-json", "non-object", "host", "job", "markers", "beat", "older-beat"],
)
def test_unreadable_or_unmatched_health_cannot_supply_a_live_beat(reading, damage):
    _store, _producer, beat, clock, export = reading
    _advance(beat, clock, snapshots.STATION_STALE_SECONDS * 3)
    assert snapshots.read_station_answer(PROJECT, SESSION)["state"] == (
        snapshots.STATION_SLOW
    ), "the positive control did not see the live publication"
    identity = json.loads(export.read_text())["station"]
    path = snapshots.station_reading_path(identity["host"], identity["job"])
    document = json.loads(path.read_text())
    if damage in ("host", "job"):
        document[damage] = "another-station"
    elif damage == "markers":
        document["station"] = []
    elif damage == "beat":
        document["station"]["heartbeat"] = "unreadable"
    elif damage == "older-beat":
        document["station"]["heartbeat"] = (START - timedelta(seconds=1)).isoformat()
    path.write_text(
        "{"
        if damage == "invalid-json"
        else json.dumps([] if damage == "non-object" else document)
    )
    answer = snapshots.read_station_answer(PROJECT, SESSION)
    assert answer is not None and answer["stale"] is True, answer
    assert answer["heartbeat_age_seconds"] == snapshots.STATION_STALE_SECONDS * 3
    rows = ProducerSnapshotNotices(PROJECT, SESSION).poll()
    assert [row["event"] for row in rows] == ["producer-lost"], rows
    assert rows[0]["remedy"] == watcher_ensure_line(PROJECT)
