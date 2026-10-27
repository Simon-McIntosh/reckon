"""A node station owns one ordered transition stream across projects and restarts."""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import station, station_store


def _pointer(home: Path, run_id: str, project: str, session: str) -> Path:
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    path = live / f"{run_id}.json"
    stream = home / "crew" / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n", encoding="utf-8"
    )
    record = {
        "run_id": run_id,
        "project": project,
        "session": session,
        "phase": "queued",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "launcher_host": socket.gethostname(),
        "node": {"id": run_id},
        "reason": "waiting for a lane",
        "log_path": str(stream),
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _phase(path: Path, phase: str) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    record["phase"] = phase
    record["reason"] = f"now {phase}"
    path.write_text(json.dumps(record), encoding="utf-8")


def _wait_for(predicate, *, seconds: float = 12.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("station did not publish the expected observation")


def _start(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            "crew",
            "station",
            "--interval",
            "0.05",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_one_station_keeps_every_session_transition_once_across_restart(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    pointers = {
        "r-one": _pointer(home, "r-one", "alpha", "session-one"),
        "r-two": _pointer(home, "r-two", "alpha", "session-two"),
        "r-three": _pointer(home, "r-three", "beta", "session-three"),
    }
    store = station_store.StationStore(runtime)
    first = _start(env)
    try:
        _wait_for(
            lambda: (
                sum(len(store.recorded_fleet(project)) for project in ("alpha", "beta"))
                == 3
                or first.poll() is not None
            )
        )
        assert first.poll() is None, first.stderr.read()
        assert store.events() == []
        initial_offsets = {row["path"]: row["consumed"] for row in store.stream_rows()}
        assert len(initial_offsets) == 3 and all(
            offset > 0 for offset in initial_offsets.values()
        )

        second = subprocess.run(
            [
                sys.executable,
                "-c",
                "from reckon.cli import main; main()",
                "crew",
                "station",
                "--once",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode != 0
        assert "held by" in second.stderr and str(first.pid) in second.stderr
        assert socket.gethostname() in second.stderr
        assert (runtime / "leases" / "station.json").is_file()
        assert not (runtime / "station.lock").exists()

        _phase(pointers["r-one"], "blocked-upstream")
        _phase(pointers["r-three"], "awaiting-upstream")
        for path in pointers.values():
            stream = Path(json.loads(path.read_text(encoding="utf-8"))["log_path"])
            with stream.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
                )

        # The events row and the consumed offset are written in one tick but
        # are not atomic together, so wait for both facts before asserting
        # either; asserting on the event count alone reads a half-applied tick.
        def both_published() -> bool:
            if len(store.events()) < 2:
                return False
            advanced = {row["path"]: row["consumed"] for row in store.stream_rows()}
            return bool(advanced) and all(
                advanced.get(path, 0) > offset
                for path, offset in initial_offsets.items()
            )

        _wait_for(lambda: both_published() or first.poll() is not None)
        assert first.poll() is None, first.stderr.read()
        assert [(row["run"], row["audience"]) for row in store.events()] == [
            ("r-one", "session-one"),
            ("r-three", "session-three"),
        ]
        assert [
            (json.loads(row["payload"])["run_id"], row["run"]) for row in store.events()
        ] == [("r-one", "r-one"), ("r-three", "r-three")]
        consumed = {row["path"]: row["consumed"] for row in store.stream_rows()}
        assert all(consumed[path] > initial_offsets[path] for path in consumed)
    finally:
        if first.poll() is None:
            first.kill()
        first.communicate(timeout=10)
        store.close()

    restarted = _start(env)
    after = station_store.StationStore(runtime)
    try:
        _wait_for(
            lambda: (
                after.station_value(station_store.HEARTBEAT_KEY) is not None
                or restarted.poll() is not None
            )
        )
        assert restarted.poll() is None, restarted.stderr.read()
        time.sleep(0.2)
        assert len(after.events()) == 2
        assert {row["path"]: row["consumed"] for row in after.stream_rows()} == consumed
        _phase(pointers["r-two"], "blocked-upstream")
        _wait_for(lambda: len(after.events()) >= 3 or restarted.poll() is not None)
        assert restarted.poll() is None, restarted.stderr.read()
        assert len(after.events()) == 3
        assert after.events(after_id=2)[0]["audience"] == "session-two"
    finally:
        if restarted.poll() is None:
            restarted.send_signal(signal.SIGTERM)
        restarted.communicate(timeout=10)
        after.close()


def test_each_tick_classifies_and_moves_the_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    store = station_store.StationStore(tmp_path / "runtime")
    home = tmp_path / "home"
    home.mkdir()
    calls: list[int] = []
    original = store.classify_tick

    def classify(**kwargs):
        calls.append(1)
        return original(**kwargs)

    monkeypatch.setattr(store, "classify_tick", classify)
    active = station.Station(store, home=home, projects=dict)
    first = datetime(2026, 10, 26, tzinfo=UTC)
    active.tick(now=first)
    assert store.station_value(station_store.HEARTBEAT_KEY) == first.isoformat()
    active.tick(now=first + timedelta(seconds=1))
    assert (
        store.station_value(station_store.HEARTBEAT_KEY)
        == (first + timedelta(seconds=1)).isoformat()
    )
    assert len(calls) == 2
    store.close()


def test_adoption_precedes_store_open(tmp_path: Path, monkeypatch) -> None:
    from reckon.crew import dispatch_launch

    called: list[str] = []
    monkeypatch.setattr(
        dispatch_launch,
        "_adopt_launched_workers_from_reexec",
        lambda: called.append("adopt"),
    )

    def open_store():
        assert called == ["adopt"]
        called.append("open")
        return station_store.StationStore(tmp_path / "runtime")

    monkeypatch.setattr(station_store.StationStore, "at_runtime_root", open_store)
    station.run_station(once=True)
    assert called == ["adopt", "open"]


def test_newly_discovered_run_is_baseline_without_an_event(
    home: Path, tmp_path: Path
) -> None:
    store = station_store.StationStore(tmp_path / "runtime")
    active = station.Station(store, home=home / "crew", projects=dict)
    _pointer(home, "r-one", "alpha", "session-one")
    active.tick()
    assert store.events() == []
    second = _pointer(home, "r-two", "alpha", "session-two")
    active.tick()
    assert store.events() == []
    _phase(second, "blocked-upstream")
    active.tick()
    assert [(row["run"], row["audience"]) for row in store.events()] == [
        ("r-two", "session-two")
    ]
    store.close()


def test_a_day_file_without_payload_remains_readable(tmp_path: Path) -> None:
    store = station_store.StationStore(tmp_path / "runtime")
    store.connect()
    day = datetime(2026, 10, 25, tzinfo=UTC).date()
    store.day_directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store._day_path(day)) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, time TEXT, project TEXT, "
            "audience TEXT, run TEXT, kind TEXT, from_state TEXT, to_state TEXT, "
            "detail TEXT, duty TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES (1, ?, 'alpha', 'session-one', 'r-one', "
            "'transition', 'queued', 'blocked', '', 'coordinator')",
            (datetime.combine(day, datetime.min.time(), tzinfo=UTC).isoformat(),),
        )
    assert store.events()[0]["payload"] is None
    store.close()
