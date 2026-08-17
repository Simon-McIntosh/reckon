from __future__ import annotations

import pytest

from reckon.calibration import (
    CalibrationFigure,
    agent_configuration_key,
    calibrate_agent_speeds,
    calibrate_plan_estimates,
)


def _run(
    plan: str,
    agent: str,
    hours: float,
    *,
    scope_changed: bool = False,
    completed_at_source: str = "stream_mtime",
) -> dict:
    return {
        "plan": plan,
        "agent_key": agent,
        "worker_seconds": hours * 3600,
        "scope_changed": scope_changed,
        "completed_at_source": completed_at_source,
    }


def test_slow_agent_updates_only_its_speed_factor() -> None:
    estimates = {"alpha": 1.0, "beta": 2.0, "gamma": 4.0}
    runs = [
        _run("alpha", "slow", 2.0),
        _run("beta", "slow", 4.0),
        _run("gamma", "slow", 8.0),
    ]

    result = calibrate_agent_speeds(runs, plan_estimates=estimates)

    assert {key: item.value for key, item in result.plan_estimates.items()} == estimates
    assert result.agent_speed_factors["slow"].value == 0.5
    assert result.agent_speed_factors["slow"].samples == 3


def test_underestimated_plan_updates_only_its_hours_for_baseline_agent() -> None:
    baseline = CalibrationFigure(value=1.0, confidence=1.0, samples=20)
    runs = [_run("underestimated", "baseline", 4.0) for _ in range(4)]

    result = calibrate_plan_estimates(
        runs,
        plan_estimates={"underestimated": 2.0},
        agent_speed_factors={"baseline": baseline},
    )

    assert result.plan_estimates["underestimated"].value == 4.0
    assert result.agent_speed_factors["baseline"] == baseline


def test_scope_changed_run_is_withheld_from_both_loops() -> None:
    runs = [
        _run("work", "worker", 2.0),
        _run("work", "worker", 200.0, scope_changed=True),
    ]

    speed = calibrate_agent_speeds(runs, plan_estimates={"work": 1.0})
    effort = calibrate_plan_estimates(
        runs,
        plan_estimates={"work": 1.0},
        agent_speed_factors={"worker": 1.0},
    )

    assert speed.agent_speed_factors["worker"].value == 0.5
    assert effort.plan_estimates["work"].value == 2.0
    assert speed.excluded["scope_changed"] == 1
    assert effort.excluded["scope_changed"] == 1
    assert speed.included_runs == effort.included_runs == 1


def test_promotion_time_run_is_withheld_from_both_loops() -> None:
    runs = [
        _run("work", "worker", 2.0, completed_at_source="terminal_event"),
        _run("work", "worker", 50.0, completed_at_source="promotion_time"),
    ]

    speed = calibrate_agent_speeds(runs, plan_estimates={"work": 1.0})
    effort = calibrate_plan_estimates(
        runs,
        plan_estimates={"work": 1.0},
        agent_speed_factors={"worker": 1.0},
    )

    assert speed.agent_speed_factors["worker"].value == 0.5
    assert effort.plan_estimates["work"].value == 2.0
    assert speed.excluded["unusable_completion"] == 1
    assert effort.excluded["unusable_completion"] == 1


def test_explicit_completion_time_is_usable_for_calibration() -> None:
    run = _run("work", "worker", 2.0, completed_at_source="provided")

    speed = calibrate_agent_speeds([run], plan_estimates={"work": 1.0})
    effort = calibrate_plan_estimates(
        [run],
        plan_estimates={"work": 1.0},
        agent_speed_factors={"worker": 1.0},
    )

    assert speed.included_runs == effort.included_runs == 1
    assert speed.excluded["unusable_completion"] == 0
    assert effort.excluded["unusable_completion"] == 0


def test_missing_counterpart_is_reported_instead_of_assumed() -> None:
    speed = calibrate_agent_speeds(
        [_run("unknown", "worker", 3.0)], plan_estimates={"known": 1.0}
    )
    effort = calibrate_plan_estimates(
        [_run("work", "unknown", 3.0)],
        plan_estimates={"work": 1.0},
        agent_speed_factors={"known": 1.0},
    )

    assert speed.included_runs == effort.included_runs == 0
    assert speed.excluded["missing_counterpart"] == 1
    assert effort.excluded["missing_counterpart"] == 1


def test_confidence_reports_sample_depth_beside_each_figure() -> None:
    one = calibrate_agent_speeds(
        [_run("work", "worker", 2.0)], plan_estimates={"work": 1.0}
    ).agent_speed_factors["worker"]
    many = calibrate_agent_speeds(
        [_run("work", "worker", 2.0) for _ in range(8)],
        plan_estimates={"work": 1.0},
    ).agent_speed_factors["worker"]

    assert one.value == many.value == 0.5
    assert one.samples == 1
    assert many.samples == 8
    assert 0.0 < one.confidence < many.confidence < 1.0


def test_noisy_sequence_converges_without_oscillation_from_empty_history() -> None:
    plan_runs: list[dict] = []
    speed_runs: list[dict] = []
    plan_values: list[float] = []
    speed_values: list[float] = []
    for actual in (3.3, 2.8, 3.1, 2.9, 3.0, 3.05, 2.95, 3.0):
        plan_runs.append(_run("target", "baseline", actual))
        plan_result = calibrate_plan_estimates(
            plan_runs,
            plan_estimates={"target": 1.0},
            agent_speed_factors={"baseline": 1.0},
        )
        plan_values.append(plan_result.plan_estimates["target"].value)

    for actual in (6.4, 5.7, 6.2, 5.9, 6.0, 6.1, 5.95, 6.0):
        speed_runs.append(_run("target", "slower", actual))
        speed_result = calibrate_agent_speeds(
            speed_runs,
            plan_estimates={"target": plan_values[-1]},
        )
        speed_values.append(speed_result.agent_speed_factors["slower"].value)

    assert plan_values[-1] == pytest.approx(3.0, abs=0.05)
    assert speed_values[-1] == pytest.approx(0.5, abs=0.05)
    assert abs(plan_values[-1] - 3.0) <= abs(plan_values[0] - 3.0)
    assert abs(speed_values[-1] - 0.5) <= abs(speed_values[0] - 0.5)
    assert plan_result.plan_estimates["target"].confidence > 0.5
    assert speed_result.agent_speed_factors["slower"].confidence > 0.5


def test_agent_configuration_key_is_stable_across_mapping_order() -> None:
    left = {"agent": {"backend": "local", "effort": "deep", "model": "worker"}}
    right = {"agent": {"model": "worker", "backend": "local", "effort": "deep"}}

    assert agent_configuration_key(left) == agent_configuration_key(right)


def test_invalid_duration_is_excluded_without_changing_figures() -> None:
    result = calibrate_agent_speeds(
        [
            {
                "plan": "work",
                "agent_key": "worker",
                "worker_seconds": None,
                "completed_at_source": "stream_mtime",
            }
        ],
        plan_estimates={"work": 2.0},
        agent_speed_factors={"worker": 1.0},
    )

    assert result.plan_estimates["work"].value == 2.0
    assert result.agent_speed_factors["worker"].value == 1.0
    assert result.excluded["invalid_measurement"] == 1
