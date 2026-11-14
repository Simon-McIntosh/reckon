"""The station prunes a dead session's follower registration.

A released follower registration keeps its file, and nothing called the sweep
that removes it, so a project's registry grew by one file per session that ever
followed it. The station now clears them from its own maintenance stage, at
most once an hour and only for a registration at least a day old; a held lock
still means a follower is delivering, whatever the file's mtime says.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import follower_registration, station, station_store

PROJECT = "alpha-fixture"
LIVE = "session-live"
DEAD = "session-dead"
LATER = "session-later"
FRESH = "session-fresh"
MOMENT = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TWO_DAYS = 2 * 24 * 60 * 60
AN_HOUR = 60 * 60

# A second process holds one registration's advisory lock, because that is the
# fact the sweep reads as "a follower is delivering here". A thread in this
# process cannot stand in for it: the registration was released by this process,
# so its own open would take the lock the sweep is meant to find held.
_HOLD_LOCK = """
import fcntl
import sys

handle = open(sys.argv[1], "a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.readline()
"""


class _Sweeps:
    """The station's review-sweep arm, answered without starting a thread."""

    def __call__(self, project: str, *, session: str | None = None, **kwargs) -> dict:
        return {}


def _station(root: Path, home: Path) -> station.Station:
    store = station_store.StationStore(root)
    beat = station.SweepBeat(sweep=_Sweeps(), start=lambda action: action())
    return station.Station(
        store, home=home, projects=lambda: {PROJECT: ""}, sweeps=beat
    )


def _release(project: str, session: str, *, moment: datetime, age: float) -> Path:
    """Register and release one session, then back-date the registration."""
    with follower_registration.follower_claim(project, session):
        pass
    path = follower_registration.follower_lock_path(project, session)
    assert path.exists(), path
    stamp = (moment - timedelta(seconds=age)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def holder():
    """One process holding a registration's lock, and a way to end it."""
    running: list[subprocess.Popen[str]] = []

    def hold(path: Path) -> None:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLD_LOCK, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline() == "held\n"
        running.append(process)

    yield hold
    for process in running:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=10)


def _pruned_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("station-pruned-follower:")
    ]


def test_only_the_dead_registration_is_removed(
    home: Path,
    tmp_path: Path,
    holder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = _release(PROJECT, LIVE, moment=MOMENT, age=TWO_DAYS)
    holder(live)
    dead = _release(PROJECT, DEAD, moment=MOMENT, age=TWO_DAYS)
    fresh = _release(PROJECT, FRESH, moment=MOMENT, age=AN_HOUR)

    _station(tmp_path / "station", home).tick(now=MOMENT)

    assert dead.exists() is False
    assert live.exists() is True
    assert fresh.exists() is True
    removed = _pruned_lines(capsys)
    assert len(removed) == 1, removed
    assert DEAD in removed[0] and str(dead) in removed[0], removed


def test_a_second_pass_within_the_hour_lists_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    node = _station(tmp_path / "station", home)
    _release(PROJECT, DEAD, moment=MOMENT, age=TWO_DAYS)
    node.tick(now=MOMENT)
    assert _pruned_lines(capsys)

    later = _release(PROJECT, LATER, moment=MOMENT, age=TWO_DAYS)
    node.tick(now=MOMENT + timedelta(minutes=30))
    assert later.exists() is True
    assert _pruned_lines(capsys) == []

    node.tick(now=MOMENT + timedelta(hours=2))
    assert later.exists() is False


def test_a_released_registration_younger_than_a_day_survives(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh = _release(PROJECT, FRESH, moment=MOMENT, age=AN_HOUR)

    _station(tmp_path / "station", home).tick(now=MOMENT)

    assert fresh.exists() is True
    assert _pruned_lines(capsys) == []
