from __future__ import annotations

from collections.abc import Callable

import pytest

from reckon.calibration import (
    MAX_DECLARED_BUDGET_MULTIPLE,
    CalibrationResult,
    calibrate_agent_speeds,
    calibrate_plan_estimates,
)


def _run(
    hours: float,
    *,
    time_budget: str,
    attempt: int | None = None,
    duration_source: str | None = None,
) -> dict:
    run = {
        "plan": "measured-work",
        "agent_key": "measured-worker",
        "worker_seconds": hours * 3600,
        "time_budget": time_budget,
        "completed_at_source": "stream_mtime",
    }
    if attempt is not None:
        run["attempt"] = attempt
    if duration_source is not None:
        run["worker_seconds_source"] = duration_source
    return run


def _agent_loop(runs: list[dict]) -> CalibrationResult:
    return calibrate_agent_speeds(runs, plan_estimates={"measured-work": 2.0})


def _plan_loop(runs: list[dict]) -> CalibrationResult:
    return calibrate_plan_estimates(
        runs,
        plan_estimates={"measured-work": 2.0},
        agent_speed_factors={"measured-worker": 1.0},
    )


@pytest.mark.parametrize("loop", [_agent_loop, _plan_loop])
def test_unjudgeable_and_budget_exclusions_are_reported(
    loop: Callable[[list[dict]], CalibrationResult],
) -> None:
    runs = [
        _run(1.0, time_budget="25m"),
        _run(39609 / 3600, time_budget="25m"),
    ]

    result = loop(runs)

    assert MAX_DECLARED_BUDGET_MULTIPLE == 4.0
    assert result.included_runs == 1
    assert result.unjudgeable_runs == 2
    assert result.excluded["duration_over_budget"] == 1
    assert result.excluded_worker_hours["duration_over_budget"] == 11.0025
    assert sum(result.excluded.values()) == 1
    assert sum(result.excluded_worker_hours.values()) == 11.0025
    assert result.included_runs + sum(result.excluded.values()) == len(runs)


@pytest.mark.parametrize("loop", [_agent_loop, _plan_loop])
def test_single_attempt_overrun_inside_tolerance_remains_a_sample(
    loop: Callable[[list[dict]], CalibrationResult],
) -> None:
    result = loop(
        [
            _run(
                3.5,
                time_budget="1h",
                attempt=1,
                duration_source="wall_fallback",
            )
        ]
    )

    assert result.included_runs == 1
    assert result.excluded["duration_over_budget"] == 0
    assert result.unjudgeable_runs == 0
