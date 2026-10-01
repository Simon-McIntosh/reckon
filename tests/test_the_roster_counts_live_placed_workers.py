"""The reservation roster counts the placed workers still able to hold memory.

A placement is not a seat by itself. Once every backend runs as a step inside
one shared allocation, a run that recorded a placement but whose worker has
exited — a finished-but-unpromoted run, a blocked run awaiting resume, a run
waiting on an external condition — is not resident anywhere and must not hold a
seat against the cap. These cases pin the rule that the roster counts a placed
run only while its worker can still hold memory, which is read from the process
table rather than from the run's phase or manifest, and that a placed run which
has not yet named its worker still holds its place inside the dispatch's launch
window.

The liveness probe is a stub throughout: the property under test is which
pointers reckon counts, not what the kernel answers. Scheduler verbs are never
run, and the crew state is a temporary RECKON_HOME.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import placement, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RESERVATION_JOB = "1277272"
PLACED = {"scheduler": "srun", "options": ["--partition=all", "--ntasks=1"]}


class StubProbe:
    """A process-liveness stub answering for a fixed set of live pids."""

    def __init__(self, live: Any = ()) -> None:
        self.live = {int(pid) for pid in live}
        self.asked: list[Any] = []

    def __call__(self, pid: Any) -> bool:
        self.asked.append(pid)
        return int(pid) in self.live


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the shared crew state at a temp home before anything is written."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))


def _placed_backend() -> dict[str, Any]:
    return {"launch": "cli", "command": "codex", "placement": dict(PLACED)}


def _placed_pointer(run_id: str, *, pid: Any) -> dict[str, Any]:
    return {"run_id": run_id, "project": "alpha", "placement": dict(PLACED), "pid": pid}


def _utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def test_a_placed_run_whose_worker_exited_holds_no_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Thirty placed pointers, twenty six exited: the four live ones are the roster.

    The acute case this rule exists for: a fleet carrying finished-but-unpromoted
    placed runs would otherwise reach the cap of twenty five within the hour and
    refuse every session's dispatch, though almost none of those runs holds any
    memory. With the exited workers freed, four of the thirty hold seats and a
    further dispatch is admitted.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": RESERVATION_JOB})
    live = {1000, 1001, 1002, 1003}
    pointers = [
        _placed_pointer(f"r-dead-{i}", pid=pid)
        for i, pid in enumerate(range(2000, 2026))
    ]
    pointers += [
        _placed_pointer(f"r-live-{i}", pid=pid) for i, pid in enumerate(sorted(live))
    ]
    monkeypatch.setattr(runs, "process_alive", StubProbe(live))

    # Four live placed workers, far below the cap, so the next dispatch lands.
    dispatch_module._refuse_over_reservation_roster(_placed_backend(), pointers)

    assert placement.occupying_the_reservation(pointers) == [
        _placed_pointer(f"r-live-{i}", pid=pid) for i, pid in enumerate(sorted(live))
    ]


def test_a_finished_run_is_not_counted_by_its_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The count is taken from the process, not from the run's recorded state."""
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": RESERVATION_JOB})
    pointer = {
        "run_id": "r-done",
        "project": "alpha",
        "placement": dict(PLACED),
        "pid": 4242,
        "phase": "working",
        "manifest": "status: complete",
    }
    monkeypatch.setattr(runs, "process_alive", StubProbe(()))
    assert placement.occupying_the_reservation([pointer]) == []


def test_twenty_five_live_placed_workers_refuse_the_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap still holds once every seat is genuinely resident."""
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": RESERVATION_JOB})
    live = set(range(3000, 3025))
    pointers = [_placed_pointer(f"r-{pid}", pid=pid) for pid in sorted(live)]
    monkeypatch.setattr(runs, "process_alive", StubProbe(live))

    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(_placed_backend(), pointers)

    assert "25" in str(refused.value)
    assert "resident memory per worker" in str(refused.value)


def test_a_placed_run_still_launching_holds_a_seat() -> None:
    """A placed run with no worker pid yet holds its place while it arrives."""
    now = 1_800_000_000.0
    launching = {
        "run_id": "r-launching",
        "project": "alpha",
        "placement": dict(PLACED),
        "pid": None,
        "attempt_started_at": _utc(now - 5),
    }

    assert placement.occupying_the_reservation([launching], now=now) == [launching]


def test_a_placed_run_past_its_launch_window_with_no_worker_holds_no_seat() -> None:
    """Past the window a run that never named its worker is not still arriving."""
    now = 1_800_000_000.0
    stalled = {
        "run_id": "r-never-launched",
        "project": "alpha",
        "placement": dict(PLACED),
        "pid": None,
        "attempt_started_at": _utc(now - 600),
    }

    assert placement.occupying_the_reservation([stalled], now=now) == []


def test_unplaced_pointers_hold_no_seat() -> None:
    """A pointer that records no placement runs outside the reservation."""
    probe = StubProbe(range(4000, 4010))
    pointers = [{"run_id": f"r-{pid}", "pid": pid} for pid in range(4000, 4010)]

    assert placement.occupying_the_reservation(pointers, alive=probe) == []
