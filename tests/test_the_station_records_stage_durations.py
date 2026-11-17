"""Completed tick timings remain readable while the next tick runs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon.crew import obligation_snapshot as snapshots
from reckon.crew import station, station_store

START = datetime(2026, 11, 17, tzinfo=UTC)


@pytest.fixture()
def timed_station(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    elapsed = [0.0]
    durations = dict(
        zip(snapshots.STATION_TICK_STAGES, (0.5, 2.0, 0.25, 4.0, 1.0), strict=True)
    )
    monkeypatch.setattr(station, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))

    def spend(stage):
        elapsed[0] += durations[stage]

    @contextmanager
    def committing():
        yield
        spend("per-project-refresh")

    with station_store.StationStore(root) as store:
        producer = station.Station(
            store,
            home=home,
            projects=dict,
            duties=SimpleNamespace(committing=committing),
            clock=lambda: START + timedelta(seconds=elapsed[0]),
        )
        monkeypatch.setattr(store, "tick", lambda **kwargs: spend("store-tick"))

        def classify(**kwargs):
            spend("classify")
            return {}

        monkeypatch.setattr(store, "classify_tick", classify)
        heartbeat = store.heartbeat

        def beat(**kwargs):
            spend("heartbeat")
            heartbeat(**kwargs)

        monkeypatch.setattr(store, "heartbeat", beat)
        monkeypatch.setattr(store, "sweep_retention", lambda **kwargs: spend("publish"))
        monkeypatch.setattr(producer, "_prune_released_followers", lambda moment: [])
        yield producer, elapsed, durations


def test_each_completed_tick_persists_its_stage_durations(timed_station):
    producer, _elapsed, durations = timed_station
    for scale in (1, 2):
        started = producer.clock().isoformat()
        producer.tick()
        markers = dict(
            producer.store.connect().execute("SELECT key, value FROM station")
        )
        assert markers["tick_stage"] == snapshots.STATION_TICK_STAGES[-1]
        assert markers["tick_started_at"] == started
        assert markers["tick_finished_at"] == producer.clock().isoformat()
        assert (
            snapshots.station_heartbeat_age(markers["heartbeat"], now=producer.clock())
            is not None
        )
        record = json.loads(markers["last_tick"])
        assert record["stage_durations_seconds"] == durations
        assert record["duration_seconds"] == sum(durations.values())
        assert record["started_at"] == started
        assert record["finished_at"] == producer.clock().isoformat()
        if scale == 1:
            durations.update({key: value * 2 for key, value in durations.items()})


def test_station_reading_reports_last_total_and_slowest_without_a_session(
    timed_station,
):
    producer, _elapsed, durations = timed_station
    producer.tick()
    with station_store.StationStore(producer.store.root) as reopened:
        assert (
            reopened.station_value("tick_finished_at") == producer.clock().isoformat()
        )
    reading = snapshots.read_station_tick(now=producer.clock())
    assert reading is not None and reading["state"] == snapshots.STATION_LIVE
    assert reading["tick_age_seconds"] is None
    assert reading["last_tick"]["duration_seconds"] == sum(durations.values())
    assert reading["last_tick"]["slowest_stage"] == "per-project-refresh"
    assert reading["last_tick"]["slowest_stage_seconds"] == 4.0


def test_running_tick_preserves_the_last_completed_measurement(timed_station):
    producer, elapsed, _durations = timed_station
    producer.tick()
    completed = snapshots.read_station_tick(now=producer.clock())
    assert completed is not None
    producer._enter("store-tick", started=producer.clock() + timedelta(seconds=1))
    elapsed[0] += 2
    running = snapshots.read_station_tick(now=producer.clock())
    assert running is not None and running["tick_stage"] == "store-tick"
    assert running["tick_age_seconds"] == 1
    assert running["last_tick"] == completed["last_tick"]
