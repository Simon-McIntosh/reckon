from __future__ import annotations

import pytest

from reckon.calibration import calibrate_agent_speeds, calibrate_plan_estimates


def _run(hours: float, *, attempt: int, duration_source: str | None) -> dict:
    run = {
        "plan": "measured-work",
        "agent_key": "measured-worker",
        "worker_seconds": hours * 3600,
        "attempt": attempt,
        "completed_at_source": "stream_mtime",
    }
    if duration_source is not None:
        run["worker_seconds_source"] = duration_source
    return run


@pytest.mark.parametrize("loop", ["agent", "plan"])
def test_resumed_fallback_durations_are_excluded_and_reported(loop: str) -> None:
    runs = [
        _run(8.0, attempt=2, duration_source="wall_fallback"),
        _run(3.0, attempt=3, duration_source=None),
        _run(2.0, attempt=2, duration_source="stream_events"),
        _run(4.0, attempt=1, duration_source="wall_fallback"),
    ]

    if loop == "agent":
        result = calibrate_agent_speeds(
            runs,
            plan_estimates={"measured-work": 2.0},
        )
        samples = result.agent_speed_factors["measured-worker"].samples
    else:
        result = calibrate_plan_estimates(
            runs,
            plan_estimates={"measured-work": 2.0},
            agent_speed_factors={"measured-worker": 1.0},
        )
        samples = result.plan_estimates["measured-work"].samples

    assert result.included_runs == samples == 2
    assert result.excluded["untrustworthy_duration"] == 2
    assert result.excluded_worker_hours["untrustworthy_duration"] == 11.0
    assert result.included_runs + sum(result.excluded.values()) == len(runs)
