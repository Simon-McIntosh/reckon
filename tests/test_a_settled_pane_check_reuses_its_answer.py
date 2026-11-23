"""A settled pair's live-work check reuses its answer while its pointers stand still.

A session host asks every tick whether a settled pair owes a pane, and the
answer classifies every live pointer in the project, which runs git and reads
review and budget records from shared storage. One host asking every five
seconds read 27 to 52 MB/s on 23 November. The answer is kept while the
session's own pointers and the ownerless ones are unchanged, is asked again at
once when one of them changes, and is asked again after a bounded time anyway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import runs, session_host, station_subscriber

PROJECT = "reuse-the-answer"
SESSION = "s-reuse-the-answer"
PEER = "s-another-session"


class _Asked(list):
    """The sessions the live-work rule was asked about, and the clock it read."""

    clock: list[float]


@pytest.fixture()
def counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Asked:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(session_host, "_LIVE_WORK_ANSWERS", {}, raising=False)
    asked = _Asked()
    real = station_subscriber.population_has_live_work

    def counting(pointers, session):
        asked.append(str(session))
        return real(pointers, session)

    monkeypatch.setattr(station_subscriber, "population_has_live_work", counting)
    clock = [1000.0]
    monkeypatch.setattr(session_host.time, "monotonic", lambda: clock[0])
    asked.clock = clock
    return asked


def _pointer(run_id: str, session: str, phase: str = "running") -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "role": "implement",
            "node": {"id": f"node-{run_id}", "plan": "a-plan"},
            "phase": phase,
            "created_at": runs._utc_now(),
        },
    )


def test_an_unchanged_population_is_classified_once_per_window(counted) -> None:
    _pointer("r-own-running", SESSION)
    first = session_host.live_work_run(PROJECT, SESSION)
    for _tick in range(5):
        counted.clock[0] += 5.0
        assert session_host.live_work_run(PROJECT, SESSION) == first
    assert len(counted) == 1, counted

    counted.clock[0] += getattr(session_host, "LIVE_WORK_RECHECK_SECONDS", 30.0)
    session_host.live_work_run(PROJECT, SESSION)
    assert len(counted) == 2, "the answer must be asked again after the window"


def test_a_change_to_the_sessions_own_pointer_is_seen_at_once(counted) -> None:
    assert session_host.live_work_run(PROJECT, SESSION) is None
    _pointer("r-own-new", SESSION)
    counted.clock[0] += 1.0
    assert session_host.live_work_run(PROJECT, SESSION) == "r-own-new"
    assert len(counted) == 2


def test_another_sessions_pointer_does_not_force_a_fresh_answer(counted) -> None:
    _pointer("r-own-running", SESSION)
    session_host.live_work_run(PROJECT, SESSION)
    _pointer("r-peer-running", PEER)
    counted.clock[0] += 1.0
    session_host.live_work_run(PROJECT, SESSION)
    assert len(counted) == 1, counted
