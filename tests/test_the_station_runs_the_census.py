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
    station.run_station(
        once=True, store=store, station_instance=active, census_sweep=True
    )
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


def _station_named(home, tick=lambda: None):
    return SimpleNamespace(
        home=home,
        clock=lambda: datetime.now(tz=UTC),
        reflex=SimpleNamespace(start=lambda: None, stop=lambda: None),
        tick=tick,
    )


def test_a_station_that_did_not_opt_in_sends_no_signal(stubs, tmp_path, monkeypatch):
    """A planted orphan survives a station run that never opted into the sweep."""
    orphan = stubs(1)
    store = station_store.StationStore(tmp_path / "runtime")
    monkeypatch.setattr(station, "StationReloader", lambda root: None)

    def refuse(*args, **kwargs):
        raise AssertionError("a station that did not opt in ran the census")

    with monkeypatch.context() as patched:
        patched.setattr(census, "census", refuse)
        patched.setattr(os, "kill", refuse)
        patched.setattr(os, "pidfd_open", refuse)
        patched.setattr(
            signal,
            "pidfd_send_signal",
            refuse,
            raising=False,
        )
        station.run_station(
            once=True, store=store, station_instance=_station_named(tmp_path / "crew")
        )
    assert orphan.poll() is None, "an unopted station signalled a process"


def test_the_census_thread_stops_when_the_station_exits(tmp_path, monkeypatch):
    """run_station's finally stops the census thread it started."""
    events = []

    class RecordingBeat:
        def __init__(self, *, home, beat_seconds=None):
            self.home = home

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(station, "CensusBeat", RecordingBeat)
    monkeypatch.setattr(station, "StationReloader", lambda root: None)
    store = station_store.StationStore(tmp_path / "runtime")
    station.run_station(
        once=True,
        store=store,
        station_instance=_station_named(tmp_path / "crew"),
        census_sweep=True,
    )
    assert events == ["start", "stop"]


def test_a_spared_orphan_is_never_announced_as_stopping(tmp_path, monkeypatch, capsys):
    """The stop line follows the revalidation that confirms the target."""
    report = {
        "orphaned": [
            {
                "pid": 424242,
                "start_time": "12345",
                "owner": {"kind": "stamped", "source": "RECKON_OWNER_PID"},
            }
        ]
    }
    reports = [report, {"orphaned": []}]
    monkeypatch.setattr(census, "census", lambda **kwargs: reports.pop(0))
    beat = station.CensusBeat(home=tmp_path)
    assert beat.beat_once() is True
    records = [
        json.loads(line.removeprefix("station-census: "))
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("station-census: ")
    ]
    assert [row["action"] for row in records] == ["spared"]
    assert records[0]["owner"] == report["orphaned"][0]["owner"]


def test_apply_revalidates_a_whole_sweep_from_one_table_read(monkeypatch):
    """One process-table walk answers every listed pid, not one per pid."""
    calls = []

    def counting(**kwargs):
        calls.append(kwargs)
        return {"orphaned": []}

    monkeypatch.setattr(census, "census", counting)
    report = {
        "state_home": "/nonexistent-crew-home",
        "orphaned": [
            {"pid": pid, "start_time": str(pid), "owner": {"kind": "stamped"}}
            for pid in (1001, 1002, 1003)
        ],
    }
    results = census.apply_census(report)
    assert len(calls) == 1, f"apply walked the table {len(calls)} times"
    assert [row["action"] for row in results] == ["spared", "spared", "spared"]
