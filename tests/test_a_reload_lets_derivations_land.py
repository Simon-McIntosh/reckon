"""A station reload lets the derivations in flight land before it replaces the image.

The station re-executes itself whenever committed code moves, and ``os.execve``
ends every thread, so a project's duty derivation caught by a reload was lost.
A derivation slower than the gap between two landings on the primary branch was
lost on every reload: on 14 November reckon's duties sat at 11:56Z for seven
hours while every other project's moved, because its derivation, the slowest in
the fleet, never outlived the next landing.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import checkout_trust
from reckon.crew import follower_reload, station, station_obligations, station_store

OLD = "0" * 40
NEW = "1" * 40


@pytest.fixture
def reloader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A reloader whose committed code has moved, with the exec recorded."""
    runtime_root = tmp_path / "runtime"
    image = runtime_root / "reload-trees" / "key" / NEW
    image.mkdir(parents=True)
    identity = [NEW]
    monkeypatch.setattr(
        checkout_trust, "source_code_identity", lambda *a, **k: identity[0]
    )
    monkeypatch.setattr(
        checkout_trust,
        "materialize_committed_tree",
        lambda root, revision, *, active_root=None: (
            image,
            [],
            [],
            os.open(os.devnull, os.O_RDONLY),
        ),
    )
    monkeypatch.setattr(checkout_trust, "reckon_imports", lambda root: (["reckon"], ""))
    monkeypatch.setattr(follower_reload, "image_reexec_environment", lambda *a, **k: {})
    execs: list[str] = []

    def execve(_path: str, _argv: list[str], _env: dict[str, str]) -> None:
        execs.append("exec")
        raise OSError("execve captured, not executed")

    monkeypatch.setattr(os, "execve", execve)
    active = station.StationReloader(runtime_root)
    active.identity = OLD
    return SimpleNamespace(reloader=active, execs=execs, identity=identity)


@pytest.fixture
def duties(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A duty beat whose derivations block until the case releases them."""
    release = threading.Event()
    started: list[str] = []

    def publish(store, *, project, **_kwargs):
        started.append(project)
        assert release.wait(10), "the derivation was never released"

    monkeypatch.setattr(station_obligations, "publish", publish)
    monkeypatch.setattr(
        station_store,
        "StationStore",
        lambda *a, **k: SimpleNamespace(close=lambda: None),
    )
    beat = station.DutyBeat(tmp_path / "runtime")
    yield SimpleNamespace(beat=beat, release=release, started=started)
    release.set()
    beat.drain(timeout=10)


def _request(beat: station.DutyBeat, project: str) -> bool:
    return beat.request(project, home="home", changed=True, moment=datetime.now(UTC))


def test_a_due_reload_waits_for_a_derivation_in_flight(reloader, duties):
    assert _request(duties.beat, "slow")
    reloader.reloader.poll(duties.beat)
    assert reloader.execs == []
    duties.release.set()
    duties.beat.drain(timeout=10)
    with pytest.raises(OSError, match="execve captured"):
        reloader.reloader.poll(duties.beat)
    assert reloader.execs == ["exec"]


def test_no_derivation_starts_while_a_reload_waits(reloader, duties):
    assert _request(duties.beat, "slow")
    reloader.reloader.poll(duties.beat)
    assert _request(duties.beat, "other") is False
    assert duties.beat.in_flight() == {"slow"}


def test_a_reload_that_did_not_happen_starts_derivations_again(reloader, duties):
    assert _request(duties.beat, "slow")
    reloader.reloader.poll(duties.beat)
    duties.release.set()
    duties.beat.drain(timeout=10)
    reloader.identity[0] = OLD
    reloader.reloader.poll(duties.beat)
    assert _request(duties.beat, "other")


def test_a_failed_exec_starts_derivations_again(reloader, duties):
    with pytest.raises(OSError, match="execve captured"):
        reloader.reloader.poll(duties.beat)
    assert _request(duties.beat, "other")


def test_the_hold_is_bounded(reloader, duties, monkeypatch):
    monkeypatch.setattr(station, "RELOAD_HOLD_SECONDS", 0.0)
    assert _request(duties.beat, "stuck")
    with pytest.raises(OSError, match="execve captured"):
        reloader.reloader.poll(duties.beat)
    assert reloader.execs == ["exec"]


def test_the_station_loop_hands_its_duties_to_the_reload(tmp_path, monkeypatch):
    handed: list[object] = []

    class StopLoopError(Exception):
        pass

    class Reloader:
        def __init__(self, root: Path) -> None:
            pass

        def poll(self, duties: object = None) -> None:
            handed.append(duties)
            raise StopLoopError

    beat = SimpleNamespace(drain=lambda timeout=None: None)
    active = SimpleNamespace(
        home=tmp_path / "crew",
        clock=lambda: datetime.now(tz=UTC),
        reflex=SimpleNamespace(start=lambda: None, stop=lambda: None),
        duties=beat,
        tick=lambda: None,
    )
    monkeypatch.setattr(station, "StationReloader", Reloader)
    with pytest.raises(StopLoopError):
        station.run_station(
            store=station_store.StationStore(tmp_path / "runtime"),
            station_instance=active,
            interval=0.0,
        )
    assert handed == [beat]
