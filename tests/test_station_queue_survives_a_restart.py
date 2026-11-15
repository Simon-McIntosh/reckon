"""A restarted station publishes the whole queued population on its first tick."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
from contextlib import nullcontext

import pytest

from reckon.crew import recovery_watch, runs, station, station_store


class QuietDuties:
    def committing(self):
        return nullcontext()

    def request(self, *args, **kwargs):
        pass


class QuietSweeps:
    def request(self, *args, **kwargs):
        pass


def _node(store):
    return station.Station(
        store,
        home=runs.crew_home(),
        projects=dict,
        duties=QuietDuties(),
        sweeps=QuietSweeps(),
    )


def _partial_tick(root, channel, stage):
    store = station_store.StationStore(root)
    original = getattr(store, stage)
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 14:
            channel.send("fourteen")
            signal.pause()
        return result

    setattr(store, stage, interrupted)
    _node(store).tick()


@pytest.mark.parametrize("stage", ["ingest_pointer", "_write_classification"])
def test_first_published_tick_after_a_kill_counts_every_queued_request(
    tmp_path, monkeypatch, stage
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    request_ids = {f"r-count-{index:02d}" for index in range(31)}
    for run_id in request_ids:
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": "fixture",
                "session": "coordinator",
                "phase": "queued",
                "queued_at": runs._utc_now(),
                "created_at": runs._utc_now(),
                "node": {"id": run_id},
            },
        )
    root = tmp_path / "station"
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(
        target=_partial_tick, args=(root, child, stage)
    )
    process.start()
    try:
        assert parent.poll(30), "station did not reach the partial tick"
        assert parent.recv() == "fourteen"
        print(f"SIGKILL station_pid={process.pid} stage={stage} processed=14")
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)
        parent.close()
        child.close()

    store = station_store.StationStore(root)
    published = []
    original = store.record_tick

    def record(project, events, fleet, **kwargs):
        published.append(recovery_watch._fleet_counts(fleet))
        return original(project, events, fleet, **kwargs)

    monkeypatch.setattr(store, "record_tick", record)
    try:
        _node(store).tick()
        queued = {
            row["run_id"] for row in store.pointer_rows() if row["phase"] == "queued"
        }
        assert queued == request_ids
        assert published[0]["waiting"] == len(queued) == 31
        durable = json.loads(store.station_value("fleet:fixture"))
        assert set(durable) == request_ids
        print(
            f"restart stage={stage} first_published_queue={published[0]['waiting']} store_queue={len(queued)}"
        )
    finally:
        store.close()
