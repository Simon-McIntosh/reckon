"""A node station owns one ordered transition stream across projects and restarts."""

from __future__ import annotations

import json
import os
import signal
import socket
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
    record = {
        "run_id": run_id,
        "project": project,
        "session": session,
        "phase": "queued",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "launcher_host": socket.gethostname(),
        "node": {"id": run_id},
        "reason": "waiting for a lane",
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
        [sys.executable, "-c", "from reckon.cli import main; main()", "crew", "station", "--interval", "0.05"],
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
        _wait_for(lambda: len(store.events()) >= 3 or first.poll() is not None)
        assert first.poll() is None, first.stderr.read()
        initial = store.events()
        assert len(initial) == 3, initial
        assert {(row["run"], row["audience"]) for row in initial} == {
            ("r-one", "session-one"),
            ("r-two", "session-two"),
            ("r-three", "session-three"),
        }
        assert all(row["kind"] == "transition" for row in initial)
        assert all(json.loads(row["payload"])["run_id"] == row["run"] for row in initial)

        second = subprocess.run(
            [sys.executable, "-c", "from reckon.cli import main; main()", "crew", "station", "--once"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode != 0
        assert "held by" in second.stderr and str(first.pid) in second.stderr

        _phase(pointers["r-one"], "blocked-upstream")
        _phase(pointers["r-three"], "awaiting-upstream")
        _wait_for(lambda: len(store.events()) == 5 or first.poll() is not None)
        assert first.poll() is None, first.stderr.read()
        assert [(row["run"], row["audience"]) for row in store.events(after_id=3)] == [
            ("r-one", "session-one"),
            ("r-three", "session-three"),
        ]
        consumed = {row["path"]: row["consumed"] for row in store.stream_rows()}
    finally:
        if first.poll() is None:
            first.kill()
        first.communicate(timeout=10)
        store.close()

    restarted = _start(env)
    after = station_store.StationStore(runtime)
    try:
        _wait_for(lambda: after.station_value(station_store.HEARTBEAT_KEY) is not None or restarted.poll() is not None)
        assert restarted.poll() is None, restarted.stderr.read()
        time.sleep(0.2)
        assert len(after.events()) == 5
        assert {row["path"]: row["consumed"] for row in after.stream_rows()} == consumed
        _phase(pointers["r-two"], "blocked-upstream")
        _wait_for(lambda: len(after.events()) == 6 or restarted.poll() is not None)
        assert restarted.poll() is None, restarted.stderr.read()
        assert after.events(after_id=5)[0]["audience"] == "session-two"
    finally:
        if restarted.poll() is None:
            restarted.send_signal(signal.SIGTERM)
        restarted.communicate(timeout=10)
        after.close()


def test_each_tick_classifies_and_moves_the_heartbeat(tmp_path: Path, monkeypatch) -> None:
    store = station_store.StationStore(tmp_path / "runtime")
    home = tmp_path / "home"
    home.mkdir()
    calls: list[int] = []
    original = store.classify_tick

    def classify(**kwargs):
        calls.append(1)
        return original(**kwargs)

    monkeypatch.setattr(store, "classify_tick", classify)
    active = station.Station(store, home=home, projects=lambda: {})
    first = datetime(2026, 10, 26, tzinfo=UTC)
    active.tick(now=first)
    assert store.station_value(station_store.HEARTBEAT_KEY) == first.isoformat()
    active.tick(now=first + timedelta(seconds=1))
    assert store.station_value(station_store.HEARTBEAT_KEY) == (first + timedelta(seconds=1)).isoformat()
    assert len(calls) == 2
    store.close()


def test_adoption_precedes_store_open(tmp_path: Path, monkeypatch) -> None:
    from reckon.crew import dispatch_launch

    called: list[str] = []
    monkeypatch.setattr(dispatch_launch, "_adopt_launched_workers_from_reexec", lambda: called.append("adopt"))

    def open_store():
        assert called == ["adopt"]
        called.append("open")
        return station_store.StationStore(tmp_path / "runtime")

    monkeypatch.setattr(station_store.StationStore, "at_runtime_root", open_store)
    station.run_station(once=True)
    assert called == ["adopt", "open"]
