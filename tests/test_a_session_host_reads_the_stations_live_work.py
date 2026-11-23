"""A session host takes its pane's live work from the station's reading.

Every Claude Code session runs its own host, which keeps that session's monitor
pane fed. Each host decided on its own, every five seconds, whether a settled
pane owed work, by classifying every live pointer in the project -- spawning git
and reading review and budget records from shared storage. The station already
judges every project's live work once on its tick and records the answer as the
live-work reading, so on 23 November one host read 45 to 54 MB/s repeating a
judgement the station had already made.

The cases below drive ``SessionHost.tick`` directly. The station reading and the
pointer classification are the two seams, so each case states the reading it
wants and reads whether the classification ran. The reading is stated by a case
rather than read from a real store, so no case touches a live station.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import runs, session_host, station_subscriber

PROJECT = "proj"
SESSION = "sess-owner"
WORKING_RUN = "r-owns-a-working-run"

# The wait status a child that settled leaves behind.
SETTLED_STATUS = session_host.SETTLED_EXIT_CODE << 8


@pytest.fixture(autouse=True)
def isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew-home read at a temporary tree, and clear the answer cache."""
    home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(session_host, "_LIVE_WORK_ANSWERS", {}, raising=False)
    return home


class _Classifications(list):
    """The sessions the host's own pointer classification was asked about."""


@pytest.fixture()
def classification(monkeypatch: pytest.MonkeyPatch) -> _Classifications:
    """Record every pointer classification the host runs.

    A case whose station reading answers must leave this empty: that is the
    observable claim that the host classified no pointer.
    """
    classified = _Classifications()
    real = station_subscriber.population_has_live_work

    def counting(pointers, session):
        classified.append(str(session))
        return real(pointers, session)

    monkeypatch.setattr(station_subscriber, "population_has_live_work", counting)
    return classified


class StubHost(session_host.SessionHost):
    """A host whose children are pids this case hands out and ends on demand."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.spawned: list[int] = []
        self.exits: list[int] = []
        self._next_pid = 9_000

    def _spawn(self, child: session_host._Child) -> int | None:
        self._next_pid += 1
        self.spawned.append(self._next_pid)
        return self._next_pid

    def _poll_exit(self, child: session_host._Child) -> int | None:
        if child.pid in self.exits:
            self.exits.remove(child.pid)
            return SETTLED_STATUS
        return None

    def settle(self, pid: int) -> None:
        self.exits.append(pid)

    def child(self) -> session_host._Child:
        return self._children[(PROJECT, SESSION)]


def _host(tmp_path: Path) -> StubHost:
    """A host fenced to a temporary record and log directory, with the default view."""
    return StubHost(
        owner={"pid": os.getpid(), "start_time": "a-state-time-for-the-stub"},
        follower_argv=["a-stub-follower"],
        environ={
            session_host.STATE_DIR_ENV: str(tmp_path / "state"),
            session_host.LOG_DIR_ENV: str(tmp_path / "log"),
            session_host.POLL_ENV: "0.05",
            session_host.BACKOFF_ENV: "0.0",
        },
    )


def _request(host: StubHost) -> None:
    host.handle(
        json.dumps({"project": PROJECT, "session": SESSION, "owner_checked": True})
    )


def _settled(host: StubHost) -> None:
    """Run the requested child to a settle."""
    _request(host)
    assert len(host.spawned) == 1, "the request must start one child"
    host.settle(host.child().pid)
    host.tick()


def test_a_fresh_reading_naming_the_session_rearms_without_classifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classification: _Classifications
) -> None:
    monkeypatch.setattr(
        session_host, "_station_live_work_verdict", lambda p, s: True, raising=False
    )
    host = _host(tmp_path)
    _settled(host)

    assert len(host.spawned) == 2, "a fresh reading naming the session re-arms the pair"
    child = host.child()
    assert child.settled is False
    assert child.live_work_source == session_host.LIVE_WORK_SOURCE_STATION
    assert child.rearmed_for is None, (
        "the station's reading names the session, not the run behind it"
    )
    assert classification == [], "the reading answered, so no pointer is classified"
    recorded = json.loads(host.record_path.read_text(encoding="utf-8"))
    assert recorded["children"][0]["live_work_source"] == "station"


def test_a_fresh_reading_not_naming_the_session_leaves_it_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classification: _Classifications
) -> None:
    monkeypatch.setattr(
        session_host, "_station_live_work_verdict", lambda p, s: False, raising=False
    )
    host = _host(tmp_path)
    _settled(host)

    assert len(host.spawned) == 1, "a fresh reading not naming it spawns nothing"
    child = host.child()
    assert child.settled is True
    assert child.live_work_source is None
    assert classification == [], "the reading answered, so no pointer is classified"


def test_a_stale_reading_falls_back_and_the_census_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classification: _Classifications
) -> None:
    monkeypatch.setattr(
        session_host, "_station_live_work_verdict", lambda p, s: None, raising=False
    )
    runs._write_json(
        runs.pointer_path(WORKING_RUN),
        {
            "run_id": WORKING_RUN,
            "project": PROJECT,
            "session": SESSION,
            "phase": "working",
        },
    )
    host = _host(tmp_path)
    _settled(host)

    assert len(classification) == 1, (
        "no fresh reading, so the host classifies as before"
    )
    assert len(host.spawned) == 2, "the classification named this session's run"
    child = host.child()
    assert child.rearmed_for == WORKING_RUN
    assert child.live_work_source == session_host.LIVE_WORK_SOURCE_CLASSIFIED
    recorded = json.loads(host.record_path.read_text(encoding="utf-8"))
    assert recorded["children"][0]["live_work_source"] == "classified"
