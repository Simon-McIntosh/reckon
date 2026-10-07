"""Arming on one host leaves a live producer on another host alone.

The watch producer registers the host that issued its pid beside the pid
itself. ``watch_observer_alive`` checked that parent pid against the local
process table without comparing hosts, so an arming on a different node found
its own table empty for the foreign number and read the producer dead. Arming
then tried to unwatch a producer it cannot signal and timed out.

``_record_producer_dead`` and ``_seat_names_a_foreign_host`` already hold the
rule for the neighbouring questions: a seat naming another host is never judged
dead from here. These tests pin the same rule onto the observer check — a
foreign-host registration reads *unknown*, not dead — while a registration
isolated to this host, or to no host at all, is judged exactly as before.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import time
from pathlib import Path

import pytest

from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# A pid this host cannot have issued and cannot be running, standing in for a
# parent on another kernel exactly as the local table answers for one.
ABSENT_PID = 1 << 29

OTHER_HOST = "98dci4-clu-2058"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    """Move the seat and the stream into a throwaway configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _registration(*, host: str | None, parent_pid: object = ABSENT_PID) -> dict:
    record = {
        "project": "proj",
        "pid": ABSENT_PID,
        "pid_start_time": "4242",
        "parent_pid": parent_pid,
        "parent_start_time": "5252",
        "stall_window": "15m",
    }
    if host is not None:
        record["host"] = host
    return record


def _write_seat(project: Path | str, record: dict) -> None:
    path = runs.watch_lock_path(str(project))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def _write_stream(project: str, *, age_seconds: float = 0.0) -> None:
    path = runs.watch_stream_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event": "transition", "run_id": "r-one"}) + "\n")
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(path, (past, past))


def test_a_foreign_host_registration_reads_unknown(home: Path) -> None:
    """A parent pid from another kernel is not evidence of death here."""
    registration = _registration(host=OTHER_HOST)

    assert runs.watch_observer_alive(registration) is None


def test_arming_does_not_stop_a_foreign_host_producer(home: Path, monkeypatch) -> None:
    """A live producer on another host must survive an arming from here.

    The seat is fresh and names another host, so the producer reads live from
    the transition stream. Arming must not reach for a producer it cannot
    signal: the stop is recorded so a call is visible rather than a timeout.
    """
    project = "proj"
    _write_seat(project, _registration(host=OTHER_HOST))
    _write_stream(project)

    stops: list[tuple[str, float]] = []
    monkeypatch.setattr(
        dispatch_module,
        "_stop_watch_producer_within",
        lambda name, timeout: stops.append((name, timeout)),
    )

    state = dispatch_module._ensure_watch_producer(project)

    assert stops == [], "arming stopped a producer naming another host"
    assert state["watcher_live"] is True


def test_a_local_host_registration_with_a_dead_parent_still_reads_dead(
    home: Path,
) -> None:
    """The host comparison must not turn a genuine local death into unknown."""
    registration = _registration(host=socket.gethostname())

    assert runs.watch_observer_alive(registration) is False


def test_a_hostless_registration_keeps_its_former_behaviour(home: Path) -> None:
    """A record naming no host is still judged against this host's table."""
    assert runs.watch_observer_alive(_registration(host=None)) is False
    without_parent = _registration(host=None)
    del without_parent["parent_pid"]
    assert runs.watch_observer_alive(without_parent) is None
