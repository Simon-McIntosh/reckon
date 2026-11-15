"""A request that has not launched is never drawn as a working worker.

The station classifies every live pointer and the follower draws what it is
given. On 14 November five nova requests whose pointers read ``held`` or
``awaiting-upstream``, with no pid and no ``launched_at``, were drawn
``new -> working`` and counted in the working column; at 18:02Z a batch of
sixteen requests, thirteen of them already dropped and archived, drew
``new -> working`` in the same second. A coordinator trusting those rows
believes held or parked work has launched.

These cases hold a follower's own reading of a pointer population: what a row
says, the transition it draws on arrival, which column it appears in, and what
happens when it finally launches, ages or departs.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import promotion, recovery, runs
from reckon.crew.recovery_watch import (
    FLEET_WORKING_STATES,
    _fleet_counts,
    _watch_snapshot,
    fleet_transitions,
)
from reckon.crew.ticker import _display_state

PROJECT = "proj"
SESSION = "coordinator-a"
STALL_SECONDS = 300

# The states a row carries when a worker's absence has been read as a stop. A
# request with no worker has nothing to stall, so no age may move it here.
STOP_STATES = frozenset(
    {
        "blocked",
        "stalled",
        "abandoned",
        "interrupted",
        "launch-failed",
        "failed",
        "needs-help",
        "unreadable",
        "exited-unfinished",
    }
)

# The five nova requests of the reported minute, by the phase their recorded
# pointers carried. None named a pid, a worktree or a launch clock.
NOVA_REQUESTS = (
    ("r-20261114T144505738776", "held"),
    ("r-20261114T144521862249", "held"),
    ("r-20261114T144526369757", "held"),
    ("r-20261114T144531369043", "awaiting-upstream"),
    ("r-20261114T144536248146", "awaiting-upstream"),
)


def _crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    return home


def _pointer(
    home: Path,
    run_id: str,
    phase: str,
    *,
    launched: bool = False,
    age_seconds: int = 0,
) -> dict:
    """Write one live pointer the way a dispatcher leaves it on disk.

    No pid and no ``launched_at`` unless ``launched`` -- the shape of a request
    the queue or the upstream gate holds before any worker exists.
    """
    moment = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    stream = home / f"{run_id}.log"
    stream.write_text('{"type":"assistant"}\n', encoding="utf-8")
    if age_seconds:
        stamp = moment.timestamp()
        os.utime(stream, (stamp, stamp))
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "launch": "cli",
        "node": {"id": run_id, "plan": "plan-a", "section": "dispatch"},
        "created_at": moment.isoformat(),
        "log_path": str(stream),
    }
    if launched:
        pointer["pid"] = os.getpid()
        pointer["launched_at"] = moment.isoformat()
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")
    return pointer


def _snapshots(*, moment: float | None = None, stall_seconds: int = STALL_SECONDS):
    moment = datetime.now(tz=UTC).timestamp() if moment is None else moment
    cache: dict = {}
    return {
        str(pointer["run_id"]): _watch_snapshot(
            pointer, moment=moment, stall_seconds=stall_seconds, cache=cache
        )
        for pointer in runs._list_live_records(project=PROJECT)
    }


def _counts(snapshots) -> dict:
    return _fleet_counts(snapshots, session=SESSION)


def _transitions(known, current):
    events, _running = fleet_transitions(known, current, ledger_run_ids=dict)
    return events


def test_unlaunched_requests_are_drawn_in_their_own_phases(tmp_path, monkeypatch):
    home = _crew_home(tmp_path, monkeypatch)
    _pointer(home, "r-queued", "queued")
    _pointer(home, "r-held", "held")
    _pointer(home, "r-awaiting", "awaiting-upstream")
    _pointer(home, "r-launched", "working", launched=True)

    snapshots = _snapshots()
    pointers = list(runs._list_live_records(project=PROJECT))

    # classify_pointer itself says none of the three is running.
    for run_id in ("r-queued", "r-held", "r-awaiting"):
        verdict = recovery.classify_pointer(
            runs.read_pointer(run_id), pointers=pointers
        )
        assert verdict["classification"] != "running", verdict
        assert snapshots[run_id]["recovery_classification"] != "running"

    drawn = {run_id: snapshot["state"] for run_id, snapshot in snapshots.items()}
    assert drawn["r-queued"] == "queued"
    assert drawn["r-held"] == "held"
    assert drawn["r-awaiting"] == "awaiting-upstream"
    assert drawn["r-launched"] == "working"

    # Only the launched run occupies the working column.
    assert _counts(snapshots)["working"] == 1
    assert {
        run_id
        for run_id, snapshot in snapshots.items()
        if snapshot["state"] in FLEET_WORKING_STATES
    } == {"r-launched"}

    # The transition each run draws on arrival is its own phase, never working.
    words = {
        str(snapshot["run_id"]): to_state
        for snapshot, _previous, to_state, _counts_at_line in _transitions(
            {}, snapshots
        )
    }
    assert words["r-queued"] == "queued"
    assert words["r-held"] == "held"
    assert words["r-awaiting"] == "awaiting-upstream"
    assert words["r-launched"] == "dispatched"
    assert _display_state(words["r-launched"]) == "working"
    for run_id in ("r-queued", "r-held", "r-awaiting"):
        assert _display_state(words[run_id]) != "working", words

    # No arrival line ever counts more than the one launched run as working.
    for _snapshot, _previous, _to_state, at_line in _transitions({}, snapshots):
        assert at_line["working"] <= 1, at_line


def test_a_queued_request_that_launches_is_then_working(tmp_path, monkeypatch):
    home = _crew_home(tmp_path, monkeypatch)
    _pointer(home, "r-q", "queued")
    before = _snapshots()
    assert before["r-q"]["state"] == "queued"
    assert _counts(before)["working"] == 0

    _pointer(home, "r-q", "queued", launched=True)
    after = _snapshots()
    assert after["r-q"]["state"] == "working"
    assert _counts(after)["working"] == 1

    moved = [
        (previous, to_state)
        for _snapshot, previous, to_state, _c in _transitions(before, after)
        if str(_snapshot["run_id"]) == "r-q"
    ]
    assert moved == [("queued", "working")]


def test_an_unlaunched_request_never_ages_into_needing_you(tmp_path, monkeypatch):
    home = _crew_home(tmp_path, monkeypatch)
    long_ago = STALL_SECONDS * 24
    _pointer(home, "r-held", "held", age_seconds=long_ago)
    _pointer(home, "r-awaiting", "awaiting-upstream", age_seconds=long_ago)
    # The positive control: a worker with the same silence and age does stall,
    # so the stall clock is live and the guard is what spares the request.
    _pointer(home, "r-worker", "working", launched=True, age_seconds=long_ago)

    aged = _snapshots(stall_seconds=STALL_SECONDS)

    assert aged["r-worker"]["state"] == "stalled"
    assert aged["r-held"]["state"] == "held"
    assert aged["r-awaiting"]["state"] == "awaiting-upstream"
    for run_id in ("r-held", "r-awaiting"):
        assert aged[run_id]["state"] not in STOP_STATES, aged[run_id]


def test_a_dropped_and_archived_request_is_drawn_dropped(tmp_path, monkeypatch):
    home = _crew_home(tmp_path, monkeypatch)
    _pointer(home, "r-drop", "queued")
    before = _snapshots()
    assert before["r-drop"]["state"] == "queued"

    runs.pointer_path("r-drop").unlink()
    marker = promotion.discard_spool_path("r-drop")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    (home / "archive" / "runs" / "r-drop").mkdir(parents=True)
    assert _snapshots() == {}

    events = _transitions(before, {})
    assert len(events) == 1
    _snapshot, previous, word, at_line = events[0]
    assert previous == "queued"
    assert word != "working"
    assert _display_state(word) == "dropped"
    assert at_line["working"] == 0


def test_the_five_nova_requests_of_the_reported_minute(tmp_path, monkeypatch):
    home = _crew_home(tmp_path, monkeypatch)
    pointers = []
    for run_id, phase in NOVA_REQUESTS:
        pointers.append(_pointer(home, run_id, phase))

    snapshots = _snapshots()
    assert len(snapshots) == len(NOVA_REQUESTS)
    assert _counts(snapshots)["working"] == 0
    for pointer in pointers:
        verdict = recovery.classify_pointer(pointer, pointers=pointers)
        assert verdict["classification"] != "running", verdict
        assert snapshots[pointer["run_id"]]["recovery_classification"] != "running"
        assert snapshots[pointer["run_id"]]["state"] == pointer["phase"]
