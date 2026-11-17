"""Selection spends one wall-clock budget and records each launch's duration.

Concurrent launches overlap in wall time. The selection-budget probe charges
its clock at candidate validation, while the duration probe advances only the
single launch it measures, keeping those two quantities independent.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon import flight
from reckon.crew import queue_admission, runs
from tests import test_each_admission_pass_records_its_verdict as fixtures

# Two twenty-second candidate validations fit the thirty-second selection
# budget; a pass falling through to the sixty-second default considers more.
BUDGET = 30.0

# The default the flight config supplies when the key says nothing.
DEFAULT_BUDGET = 60.0
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


def _publish_flight_budget(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Resolve flight as it does here, with only the admission budget changed.

    Replacing the whole resolution would also change every other key the pass
    reads, so only the one key the budget comes from is replaced.
    """
    resolved = flight.resolve()
    config = dict(resolved.config)
    config["admission"] = {
        **(config.get("admission") or {}),
        "pass_budget_seconds": seconds,
    }
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=config)
    )


def _run(
    resolve: Any,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    charge_selection: bool = True,
    budget: float | None = BUDGET,
    launcher: Any = None,
) -> dict[str, Any]:
    original = queue_admission._launch_reread_refusal

    def validate(pointer):
        refusal = original(pointer)
        if charge_selection:
            clock.now += LAUNCH_SECONDS
        return refusal

    with monkeypatch.context() as change:
        change.setattr(queue_admission, "_launch_reread_refusal", validate)
        return queue_admission.admit_once(
            config_for=resolve,
            launcher=launcher if launcher is not None else fixtures._launcher([]),
            clock=clock,
            budget_seconds=budget,
        )


def test_a_pass_stops_when_its_launch_budget_is_spent(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    _, resolve = fixtures._config_with_lane(lane)
    run_ids = _queue(admit_repo, 4)
    clock = _Clock()

    first = _run(resolve, clock, monkeypatch)

    assert first["launched"] == run_ids[:2]

    assert first["launch_budget_seconds"] == BUDGET
    assert first["launch_budget_exhausted"] is True

    # What the deadline left keeps its place, untouched: no attempt and no hold
    # was recorded for it, so the next pass begins where this one ended.
    deferred = run_ids[2:]
    for run_id in deferred:
        pointer = runs.read_pointer(run_id)
        assert pointer["phase"] == "queued"
        assert not pointer.get("last_admission")
    assert {row["run_id"] for row in first["verdicts"]}.isdisjoint(deferred)

    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    second = _run(resolve, clock, monkeypatch)

    assert second["launched"] == deferred
    assert second["launch_budget_exhausted"] is False


def test_a_launch_row_carries_its_duration_and_slowest_step(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    _, resolve = fixtures._config_with_lane(lane)
    _queue(admit_repo, 1)
    clock = _Clock()

    result = _run(
        resolve,
        clock,
        monkeypatch,
        charge_selection=False,
        launcher=_timed_launcher(clock),
    )

    launched = [row for row in result["verdicts"] if row["verdict"] == "launched"]
    assert len(launched) == 1
    for row in launched:
        assert row["launch_seconds"] == LAUNCH_SECONDS
        assert row["slowest_step"] == "dispatch"
        assert row["slowest_step_seconds"] == LAUNCH_SECONDS


def test_a_pass_with_no_budget_reads_the_flight_value(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    _, resolve = fixtures._config_with_lane(lane)
    run_ids = _queue(admit_repo, 4)
    _publish_flight_budget(monkeypatch, BUDGET)

    result = _run(resolve, _Clock(), monkeypatch, budget=None)

    assert result["launch_budget_seconds"] == BUDGET
    assert result["launched"] == run_ids[:2]


def test_a_pass_leaves_the_shared_config_untouched(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(10.0))
    before = copy.deepcopy(fixtures.CONFIG)
    _, resolve = fixtures._config_with_lane(lane)
    _queue(admit_repo, 2)

    _run(resolve, _Clock(), monkeypatch)

    assert before == fixtures.CONFIG


def test_the_budget_defaults_to_sixty_seconds_from_flight() -> None:
    assert flight.admission_pass_budget_seconds(None) == DEFAULT_BUDGET
    assert flight.admission_pass_budget_seconds({}) == DEFAULT_BUDGET
    assert (
        flight.admission_pass_budget_seconds({"admission": {"pass_budget_seconds": 5}})
        == 5.0
    )
    for rejected in (0, -1, "60", True, float("inf")):
        assert (
            flight.admission_pass_budget_seconds(
                {"admission": {"pass_budget_seconds": rejected}}
            )
            == DEFAULT_BUDGET
        )
