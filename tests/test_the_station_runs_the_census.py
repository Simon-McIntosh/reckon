"""The station stops proven local orphans on a bounded census cadence."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from reckon.crew import census, station, station_store


@pytest.fixture
def stubs(monkeypatch):
    children = []
    read_processes = census._processes

    def spawn(owner, *, host=None):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RECKON_")
        }
        environment.update(
            RECKON_OWNER_PID=str(owner),
            RECKON_OWNER_HOST=host or socket.gethostname(),
            RECKON_SESSION_ID="census-stub-session",
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; print('ready',flush=True); time.sleep(60)",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == "ready"
        return child

    def local_processes():
        selected = {child.pid for child in children}
        return {pid: row for pid, row in read_processes().items() if pid in selected}

    monkeypatch.setattr(census, "_processes", local_processes)
    yield spawn
    for child in children:
        if child.poll() is None:
            print(f"Stopping test stub pid {child.pid}")
            child.terminate()
            child.wait(timeout=5)
        child.stdout.close()


def test_station_beat_stops_only_the_ownerless_stub(
    stubs, tmp_path, monkeypatch, capsys
):
    orphan = stubs(1)
    owned = stubs(os.getpid())
    foreign = stubs(1, host="another-host.invalid")
    home = tmp_path / "crew"
    report = census.census(home=home)
    assert [row["pid"] for row in report["orphaned"]] == [orphan.pid]
    assert {row["pid"] for row in report["spared"]} == {owned.pid, foreign.pid}
    store = station_store.StationStore(tmp_path / "runtime")
    entered = threading.Event()

    def tick():
        entered.set()
        deadline = time.monotonic() + 3
        while orphan.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert orphan.poll() == -signal.SIGTERM, (
            "ownerless stub survived the station beat"
        )

    active = SimpleNamespace(
        home=home,
        clock=lambda: datetime.now(tz=UTC),
        reflex=SimpleNamespace(start=lambda: None, stop=lambda: None),
        tick=tick,
    )
    monkeypatch.setattr(station, "StationReloader", lambda root: None)
    station.run_station(once=True, store=store, station_instance=active)
    assert entered.is_set()
    assert owned.poll() is None
    assert foreign.poll() is None
    records = [
        json.loads(line.removeprefix("station-census: "))
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("station-census: ")
    ]
    assert [(row["pid"], row["action"]) for row in records] == [
        (orphan.pid, "stopping"),
        (orphan.pid, "stopped"),
    ]
    assert all(row["owner"]["pid"] == "1" for row in records)
    assert all(row["owner"]["session"] == "census-stub-session" for row in records)


def test_census_runs_no_more_often_than_its_interval(tmp_path, monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(station.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        census, "census", lambda **kwargs: calls.append(kwargs) or {"orphaned": []}
    )
    monkeypatch.setattr(census, "apply_census", lambda report, **kwargs: [])
    beat = station.CensusBeat(home=tmp_path)
    assert beat.beat_once() is True
    clock[0] += station.CENSUS_BEAT_SECONDS - 0.01
    assert beat.beat_once() is False
    clock[0] += 0.01
    assert beat.beat_once() is True
    assert calls == [{"home": tmp_path}, {"home": tmp_path}]


def test_a_failed_census_is_reported_and_retried_next_beat(
    tmp_path, monkeypatch, capsys
):
    attempted = threading.Event()
    attempts = []

    def broken(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError("fixture census unavailable")
        attempted.set()
        return {"orphaned": []}

    monkeypatch.setattr(census, "census", broken)
    beat = station.CensusBeat(home=tmp_path, beat_seconds=0.02)
    try:
        beat.start()
        assert attempted.wait(3)
    finally:
        beat.stop()
    assert "station-census-failure" in capsys.readouterr().err
    assert beat._thread is None
