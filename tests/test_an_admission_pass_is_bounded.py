"""An admission pass spends one launch budget, and says what each launch cost.

A pass that runs until its queue is empty holds every freed seat until it ends,
so a request reaches a seat only once the whole pass is done, and a stall inside
it hides which launch and which step was slow. The pass therefore launches
until the configured budget is spent, ends, and leaves the rest of the queue
untouched for the next pass; every launch it does make carries its own duration
and its slowest step on the verdict row.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest

from reckon import flight
from reckon.crew import queue_admission, runs
from tests import test_each_admission_pass_records_its_verdict as fixtures

BUDGET = 60.0
LAUNCH_SECONDS = 20.0


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared admission fixture: isolated crew home, launchable checkout."""
    return fixtures.admit_repo.__wrapped__(tmp_path, monkeypatch)


class _Clock:
    """A monotonic clock the stubbed launches themselves advance."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _queue(repository: Path, count: int) -> list[str]:
    """Store ``count`` distinct queued requests with disjoint write paths."""
    base = fixtures._queue(repository)
    run_ids: list[str] = []
    for index in range(count):
        pointer = copy.deepcopy(base)
        pointer["run_id"] = f"r-bounded-{index}"
        pointer["node"]["id"] = f"bounded-node-{index}"
        pointer["node"]["goal"] = f"admit bounded node {index}"
        pointer["node"]["write_paths"] = [f"bounded-node-{index}.txt"]
        runs.queue_dispatch(pointer)
        run_ids.append(pointer["run_id"])
    return run_ids


def _timed_launcher(clock: _Clock) -> Any:
    """A launch the stub spends 20 s of the pass's clock on."""

    def launch(plan, *, log_path, stderr_path, prompt_path):
        clock.now += LAUNCH_SECONDS
        return os.getpid()

    return launch


def _resolve(lane_document: Path) -> Any:
    config = fixtures.CONFIG
    config["backends"]["clive"]["lane_document"] = str(lane_document)

    def resolve(_pointer):
        return config

    return resolve


def _pass(resolve: Any, clock: _Clock) -> dict[str, Any]:
    return queue_admission.admit_once(
        config_for=resolve,
        launcher=_timed_launcher(clock),
        clock=clock,
        budget_seconds=BUDGET,
    )


def test_a_pass_stops_when_its_launch_budget_is_spent(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    resolve = _resolve(lane)
    run_ids = _queue(admit_repo, 5)
    clock = _Clock()

    first = _pass(resolve, clock)

    assert first["launched"] == run_ids[:3]
    assert first["launch_budget_seconds"] == BUDGET
    assert first["launch_budget_exhausted"] is True

    # What the deadline left keeps its place, untouched: no attempt and no hold
    # was recorded for it, so the next pass begins where this one ended.
    deferred = run_ids[3:]
    for run_id in deferred:
        pointer = runs.read_pointer(run_id)
        assert pointer["phase"] == "queued"
        assert not pointer.get("last_admission")
    assert {row["run_id"] for row in first["verdicts"]}.isdisjoint(deferred)

    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    second = _pass(resolve, clock)

    assert second["launched"] == deferred
    assert second["launch_budget_exhausted"] is False


def test_a_launch_row_carries_its_duration_and_slowest_step(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    resolve = _resolve(lane)
    _queue(admit_repo, 4)
    clock = _Clock()

    result = _pass(resolve, clock)

    launched = [row for row in result["verdicts"] if row["verdict"] == "launched"]
    assert len(launched) == 3
    for row in launched:
        assert row["launch_seconds"] == LAUNCH_SECONDS
        assert row["slowest_step"] == "dispatch"
        assert row["slowest_step_seconds"] == LAUNCH_SECONDS


def test_the_budget_defaults_to_sixty_seconds_from_flight() -> None:
    assert flight.admission_pass_budget_seconds(None) == 60.0
    assert flight.admission_pass_budget_seconds({}) == 60.0
    assert (
        flight.admission_pass_budget_seconds({"admission": {"pass_budget_seconds": 5}})
        == 5.0
    )
    for rejected in (0, -1, "60", True, float("inf")):
        assert (
            flight.admission_pass_budget_seconds(
                {"admission": {"pass_budget_seconds": rejected}}
            )
            == 60.0
        )
