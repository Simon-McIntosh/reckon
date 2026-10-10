"""One shared producer seat remains authoritative on either host."""

from __future__ import annotations

import importlib
import os
import socket
import time

import pytest

import reckon.crew.dispatch_watch as dispatch_watch_module
from reckon.crew import recovery_review_delivery, recovery_stream, recovery_watch, runs
from reckon.crew.host_lease import LEASE_STALE_SECONDS, HostLease
from reckon.crew.node import CrewError

dispatch = importlib.import_module("reckon.crew.dispatch")
recovery = importlib.import_module("reckon.crew.recovery")


@pytest.fixture()
def shared_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return tmp_path


def test_second_host_defers_to_fresh_producer_even_with_quiet_stream(
    shared_home, monkeypatch
):
    host = ["host-one"]
    monkeypatch.setattr(socket, "gethostname", lambda: host[0])
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *_: None)
    starts = []
    stops = []

    class Started:
        def poll(self):
            return 1

    def start(project):
        starts.append(project)
        return Started()

    monkeypatch.setattr(dispatch_watch_module, "_start_watch_producer", start)
    monkeypatch.setattr(
        dispatch_watch_module, "_stop_watch_producer_within", lambda *args: stops.append(args)
    )
    monkeypatch.setattr(dispatch_watch_module, "WATCHER_LOAD_BOUND_SECONDS", 0.1)

    with runs._project_watch_claim("example", "30s") as (acquired, _record):
        assert acquired
        host[0] = "host-two"
        state = dispatch._ensure_watch_producer("example")
        assert state["watcher_live"] is True
        assert starts == []
        assert stops == []


def test_second_host_takes_stale_producer_lease(shared_home, monkeypatch):
    path = runs.watch_lock_path("example")
    first = HostLease(path.parent, path.stem, "host-one", 101, "job-one")
    assert first.claim()
    past = time.time() - LEASE_STALE_SECONDS - 1
    os.utime(first.path, (past, past))
    monkeypatch.setattr(socket, "gethostname", lambda: "host-two")
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *_: None)

    with runs._project_watch_claim("example", "30s") as (acquired, _record):
        assert acquired
        holder = first.holder()
        assert holder is not None
        assert holder.host == "host-two"
        assert not first.renew()


def test_unwatch_refuses_seat_that_never_releases(shared_home, monkeypatch):
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *_: None)
    (monkeypatch.setattr(recovery_review_delivery, "_signal_process_group", lambda *args, **kwargs: None), monkeypatch.setattr(recovery_stream, "_signal_process_group", lambda *args, **kwargs: None), monkeypatch.setattr(recovery_watch, "_signal_process_group", lambda *args, **kwargs: None))

    with runs._project_watch_claim("example", "30s") as (acquired, _record):
        assert acquired
        start = time.monotonic()
        with pytest.raises(CrewError, match=r"held by .* pid"):
            recovery.unwatch("example")
        assert time.monotonic() - start < recovery.UNWATCH_SEAT_WAIT_SECONDS + 0.5


def test_unwatch_refuses_fresh_remote_holder(shared_home, monkeypatch):
    host = ["host-one"]
    monkeypatch.setattr(socket, "gethostname", lambda: host[0])
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *_: None)

    with runs._project_watch_claim("example", "30s") as (acquired, _record):
        assert acquired
        host[0] = "host-two"
        start = time.monotonic()
        with pytest.raises(CrewError, match=r"host-one pid"):
            recovery.unwatch("example")
        assert time.monotonic() - start < 0.5


def test_idle_producer_renews_host_lease(shared_home, monkeypatch):
    now = [time.time()]
    path = runs.watch_lock_path("example")

    def lease(project):
        assert project == "example"
        return HostLease(
            path.parent,
            path.stem,
            socket.gethostname(),
            os.getpid(),
            "",
            clock=lambda: now[0],
        )

    monkeypatch.setattr(runs, "watch_host_lease", lease)
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *_: None)

    class EndPollingError(Exception):
        pass

    def sleep(seconds):
        now[0] += seconds
        if now[0] - started >= LEASE_STALE_SECONDS + 5:
            assert lease("example").holder() is not None
            raise EndPollingError

    started = now[0]
    with pytest.raises(EndPollingError):
        next(recovery.watch_ticker("example", poll_interval=30, sleeper=sleep))
