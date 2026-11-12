"""A PID probe in a worker namespace cannot evict the host's live station."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from reckon.crew import dispatch_watch, host_lease, station, station_store
from reckon.crew.host_lease import HostLease, local_host


def _host_lease(root: Path, monkeypatch: pytest.MonkeyPatch) -> HostLease:
    """Create the holder in a PID namespace the dispatching worker cannot see."""
    readlink = os.readlink

    def namespace(path, *args, **kwargs):
        if str(path) == "/proc/self/ns/pid":
            return "pid:[host-fixture]"
        return readlink(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(os, "readlink", namespace)
        return HostLease(root, "station", local_host(), 2**30, "fixture-job")


def test_dispatch_pid_probe_cannot_evict_a_station_during_a_slow_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "mounts.json"))
    (tmp_path / "mounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(host_lease, "LEASE_STALE_SECONDS", 0.5)
    monkeypatch.setattr(dispatch_watch, "_fleet_spawn_enabled", lambda: True)
    monkeypatch.setattr(dispatch_watch, "_read_fleet_record", dict)
    monkeypatch.setattr(dispatch_watch, "_runs_inside_fleet_allocation", lambda _: True)
    monkeypatch.setattr(dispatch_watch, "_fleet_runtime_dir", lambda _: runtime)
    requests = []
    monkeypatch.setattr(
        dispatch_watch,
        "_write_fleet_request",
        lambda *args: requests.append(args),
    )
    monkeypatch.setattr(
        station, "StationReloader", lambda _: SimpleNamespace(poll=lambda: None)
    )
    store = station_store.StationStore(runtime)
    instance = station.Station(store, projects=dict)
    monkeypatch.setattr(instance.reflex, "start", lambda: None)
    entered, release = threading.Event(), threading.Event()
    original = store.tick

    def held_tick(**kwargs):
        entered.set()
        assert release.wait(timeout=5), "the fixture never released the slow tick"
        return original(**kwargs)

    monkeypatch.setattr(store, "tick", held_tick)
    lease = _host_lease(runtime, monkeypatch)
    errors = []

    def run():
        try:
            station.run_station(
                store=store,
                station_instance=instance,
                lease=lease,
                once=True,
                renew_seconds=0.01,
                beat_seconds=0.02,
            )
        except Exception as error:  # noqa: BLE001 - asserted by the caller
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=5), "the station never entered its tick"
        assert lease.holder() == lease.owner
        inode = lease.path.stat().st_ino
        # This is the live failure signature: a fresh, renewing lease names a
        # host PID that is absent from the worker's process table.
        with pytest.raises(ProcessLookupError):
            os.kill(lease.owner.pid, 0)
        dispatch_watch._ensure_node_station()
        deadline = time.monotonic() + 1.6
        while time.monotonic() < deadline:
            assert lease.holder() == lease.owner, "the worker evicted the sole station"
            assert lease.path.stat().st_ino == inode
            assert not store.guard.lost()
            time.sleep(0.01)
        assert requests == []
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("namespace", ["pid:[elsewhere]", ""])
def test_a_dead_holder_release_requires_a_known_matching_pid_namespace(
    tmp_path: Path, namespace: str
) -> None:
    lease = HostLease(tmp_path, "station", local_host(), os.getpid(), "fixture-job")
    assert lease.claim()
    record = json.loads(lease.path.read_text(encoding="utf-8"))
    if namespace:
        record["pid_namespace"] = namespace
    else:
        record.pop("pid_namespace", None)
    lease.path.write_text(json.dumps(record), encoding="utf-8")
    reader = HostLease(tmp_path, "station", local_host(), os.getpid() + 1, "")
    holder = reader.holder()
    assert holder is not None
    assert not reader.release_holder(holder)
    assert lease.path.exists()


def test_a_matching_pid_namespace_can_release_a_dead_holder(tmp_path: Path) -> None:
    lease = HostLease(tmp_path, "station", local_host(), 2**30, "fixture-job")
    assert lease.claim()
    reader = HostLease(tmp_path, "station", local_host(), os.getpid(), "")
    holder = reader.holder()
    assert holder is not None
    assert reader.release_holder(holder)
    assert not lease.path.exists()


def test_a_matching_pid_number_in_another_namespace_cannot_reattach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _host_lease(tmp_path, monkeypatch)
    assert lease.claim()
    reader = HostLease(
        tmp_path, "station", lease.owner.host, lease.owner.pid, lease.owner.job
    )
    assert not reader.claim()
    assert lease.renew()


def test_a_real_successor_still_closes_the_store_write_guard(tmp_path: Path) -> None:
    lease = HostLease(tmp_path, "station", local_host(), os.getpid(), "fixture-job")
    assert lease.claim()
    replacement = lease.path.with_name("successor.json")
    record = json.loads(lease.path.read_text(encoding="utf-8"))
    successor_token = uuid4().hex
    record["token"] = successor_token
    replacement.write_text(json.dumps(record), encoding="utf-8")
    replacement.replace(lease.path)
    guard = station_store.StoreWriteGuard()
    beat = station.LeaseBeat(lease, guard=guard)
    assert not beat.renew_once()
    assert beat.lost()
    with pytest.raises(station_store.StationLeaseLostError, match="lost its lease"):
        guard.check()
    assert (
        json.loads(lease.path.read_text(encoding="utf-8"))["token"] == successor_token
    )
    assert not lease.release()
