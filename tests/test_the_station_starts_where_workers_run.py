"""Station startup and image replacement stay on the worker node."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from reckon import checkout_trust
from reckon.crew import dispatch_watch, fleet_supervisor, station
from reckon.crew.host_lease import HostLease


def test_dispatch_requests_a_missing_node_station_once(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(dispatch_watch, "_read_fleet_record", lambda: {"node": "local"})
    monkeypatch.setattr(dispatch_watch, "_runs_inside_fleet_allocation", lambda _: True)
    monkeypatch.setattr(dispatch_watch, "_fleet_runtime_dir", lambda _: runtime)
    requested: list[bytes] = []
    monkeypatch.setattr(
        dispatch_watch,
        "_write_fleet_request",
        lambda _fifo, line, _deadline: requested.append(line),
    )

    dispatch_watch._ensure_node_station()
    assert requested == [b"station\n"]
    lease = HostLease(runtime, "station", socket.gethostname(), os.getpid(), "")
    assert lease.claim()
    try:
        dispatch_watch._ensure_node_station()
        assert requested == [b"station\n"]
    finally:
        lease.release()


def test_dispatch_watch_ensures_station_before_producer(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        dispatch_watch, "_ensure_node_station", lambda: calls.append("station")
    )
    monkeypatch.setattr(
        dispatch_watch, "watch_stream_path", lambda _project: tmp_path / "watch.jsonl"
    )
    monkeypatch.setattr(
        dispatch_watch,
        "watch_state",
        lambda _project, **_kwargs: {"watcher_live": True, "watcher": {}},
    )
    monkeypatch.setattr(dispatch_watch, "watch_observer_alive", lambda _watcher: True)
    dispatch_watch._ensure_watch_producer("sample")
    assert calls == ["station"]


def test_fleet_supervisor_starts_only_one_station(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls: list[list[str]] = []

    class Process:
        pid = 12345

        def poll(self):
            return None

    def spawn(argv, **_kwargs):
        calls.append(argv)
        return Process()

    monkeypatch.setattr(fleet_supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        fleet_supervisor, "service_log_directory", lambda _env: tmp_path / "logs"
    )
    process = fleet_supervisor.StationProcess(
        runtime, {"XDG_RUNTIME_DIR": str(runtime), "SLURM_JOB_ID": "job"}
    )
    process.ensure()
    process.ensure()
    assert len(calls) == 1
    assert calls[0][-2:] == ["crew", "station"]


def test_station_reloads_from_a_node_local_image(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = tmp_path / "source"
    (source / "reckon").mkdir(parents=True)
    monkeypatch.setenv(station.SOURCE_ROOT_ENV, str(source))
    old, new = "a" * 40, "b" * 40
    stamps = iter((old, new))
    monkeypatch.setattr(
        checkout_trust, "source_code_identity", lambda *_args, **_kwargs: next(stamps)
    )
    materialized: list[Path] = []
    pin_path = tmp_path / "pin"

    def materialize(_source, identity, *, active_root):
        assert identity == new
        assert active_root == reloader.import_root
        image = checkout_trust._committed_tree_cache() / identity
        image.mkdir(parents=True)
        materialized.append(image)
        return image, [], [], os.open(pin_path, os.O_CREAT | os.O_RDONLY, 0o600)

    monkeypatch.setattr(checkout_trust, "materialize_committed_tree", materialize)
    monkeypatch.setattr(checkout_trust, "reckon_imports", lambda _image: (True, ""))
    executed: list[tuple[list[str], dict[str, str]]] = []

    class ReexecutedError(Exception):
        pass

    def reexecute(_python, argv, environment):
        executed.append((argv, environment))
        raise ReexecutedError

    monkeypatch.setattr(station.os, "execve", reexecute)
    reloader = station.StationReloader(runtime)
    with pytest.raises(ReexecutedError):
        reloader.poll()
    image = materialized[0]
    assert image.is_relative_to(runtime)
    assert str(image) in executed[0][0][2]
    assert executed[0][1][station.SOURCE_ROOT_ENV] == str(source)
    os.close(int(executed[0][1][station.IMAGE_PIN_ENV]))
