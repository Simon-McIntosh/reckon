"""``crew watch`` requests the node station before it takes the producer seat.

The station reads and classifies each fact once for the whole node, so a
producer that starts on a node with no station re-derives the fleet on every
pass — the load the station exists to remove. The producer also holds the seat
for the life of its stream, so the request has to be made before it takes the
seat rather than beside it.

The request is asserted against the real ``_ensure_node_station``: a fleet
allocation whose runtime root holds no station holder, so the request is the
one the function would actually write. The negative control removes the call
from ``crew_watch`` and this case then records no request at all.
"""

from __future__ import annotations

import importlib
import os
import socket
from pathlib import Path

from reckon.crew import recovery, runs
from reckon.crew.host_lease import HostLease


def _module(name: str):
    return importlib.import_module(name)


def _option_default(command, name: str):
    """The default the command itself declares for one of its options."""
    for param in command.params:
        if param.name == name:
            return param.default
    raise AssertionError(f"{command.name} declares no {name!r} option")


class _StubReloader:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _StubPoll:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _fleet_allocation_without_a_station(
    tmp_path: Path, monkeypatch
) -> tuple[Path, list]:
    """A fleet node whose runtime root holds no station, recording each request."""
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    requested: list[bytes] = []
    monkeypatch.setattr(dispatch_watch, "_fleet_spawn_enabled", lambda: True)
    monkeypatch.setattr(dispatch_watch, "_read_fleet_record", lambda: {"node": "local"})
    monkeypatch.setattr(dispatch_watch, "_runs_inside_fleet_allocation", lambda _: True)
    monkeypatch.setattr(dispatch_watch, "_fleet_runtime_dir", lambda _: runtime)
    monkeypatch.setattr(
        dispatch_watch,
        "session_host_runtime_candidate",
        lambda **_kwargs: (runtime, "runtime-directory"),
    )
    monkeypatch.setattr(
        dispatch_watch,
        "_write_fleet_request",
        lambda _fifo, line, _deadline: requested.append(line),
    )
    return runtime, requested


def test_crew_watch_requests_the_node_station_before_the_producer_seat(
    tmp_path: Path, monkeypatch
) -> None:
    follow_module = _module("reckon.crew_follow_commands")
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    _runtime, requested = _fleet_allocation_without_a_station(tmp_path, monkeypatch)

    step_order: list[str] = []

    def record_request(fifo, line, deadline):
        step_order.append("station")
        requested.append(line)

    monkeypatch.setattr(dispatch_watch, "_write_fleet_request", record_request)

    produced: list[str] = []

    def stub_watch_follow(project, **kwargs):
        step_order.append("producer")
        produced.append(project)
        yield {"event": "baseline", "project": project}

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(recovery, "watch_follow", stub_watch_follow)
    monkeypatch.setattr(runs, "watch_lease_renewed_at", lambda project: None)
    monkeypatch.setattr(follow_module, "_FollowerReloader", _StubReloader)
    monkeypatch.setattr(follow_module, "_StampPoll", _StubPoll)
    monkeypatch.setattr(follow_module, "_ticker_grid", lambda *a, **k: object())
    monkeypatch.setattr(follow_module, "_emit_crew_result", lambda *a, **k: None)

    follow_module.crew_watch.callback(
        stop=_option_default(follow_module.crew_watch, "stop"),
        project="sample",
        stall_window="1h",
        exit_on_empty=False,
        once=False,
        ensure_service=False,
        follow=True,
        json_output=True,
        pretty=False,
        width=None,
        theme=None,
        no_color=True,
    )

    # Requested exactly once, and before the producer took the seat.
    assert requested == [b"station\n"]
    assert step_order == ["station", "producer"]
    # And the watch still ran as the producer rather than returning early.
    assert produced == ["sample"]


def test_crew_watch_leaves_a_held_station_alone(tmp_path: Path, monkeypatch) -> None:
    """A station that already holds the node is not requested a second time."""
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    runtime, requested = _fleet_allocation_without_a_station(tmp_path, monkeypatch)
    lease = HostLease(runtime, "station", socket.gethostname(), os.getpid(), "")
    assert lease.claim()
    try:
        dispatch_watch._ensure_node_station()
        assert requested == []
    finally:
        lease.release()
