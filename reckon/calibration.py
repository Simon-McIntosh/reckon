"""Independent feedback loops for plan size and worker duration factors.

The two loops intentionally share observations but never mutate each other's
figures.  A caller can therefore update worker factors against stable plan
estimates, or update plan estimates against stable worker factors, without
letting one source of error leak into the other quantity.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Any

from reckon import ledger


# A time fence is an allowance rather than an exact duration prediction. Four
# times that allowance preserves substantial overruns as useful calibration
# evidence while withholding durations likely dominated by time outside work.
MAX_DECLARED_BUDGET_MULTIPLE = 4.0
_DECLARED_DURATION = re.compile(r"^(\d+)([smh])$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


@dataclass(frozen=True)
class CalibrationFigure:
    """One calibrated value with the evidence supporting it."""

    value: float
    confidence: float
    samples: int


@dataclass(frozen=True)
class CalibrationResult:
    """Figures produced by one loop and explicit sample accounting."""

    plan_estimates: dict[str, CalibrationFigure]
    agent_speed_factors: dict[str, CalibrationFigure]
    included_runs: int
    excluded: dict[str, int]
    excluded_worker_hours: dict[str, float]
    unjudgeable_runs: int


def agent_configuration_key(run: Mapping[str, Any]) -> str:
    """Return a stable identity for the worker configuration in a run."""

    explicit = str(run.get("agent_key") or "").strip()
    if explicit:
        return explicit
    agent = run.get("agent")
    if isinstance(agent, Mapping) and agent:
        return json.dumps(dict(agent), sort_keys=True, separators=(",", ":"))
    return str(run.get("member") or run.get("backend") or "").strip()


def _figure(value: Any) -> CalibrationFigure:
    if isinstance(value, CalibrationFigure):
        return value
    if isinstance(value, Mapping):
        raw = value.get("value", value.get("hours", value.get("factor")))
        return CalibrationFigure(
            value=float(raw),
            confidence=float(value.get("confidence", 0.0) or 0.0),
            samples=int(value.get("samples", 0) or 0),
        )
    return CalibrationFigure(value=float(value), confidence=0.0, samples=0)


def _figures(values: Mapping[str, Any]) -> dict[str, CalibrationFigure]:
    figures = {str(key): _figure(value) for key, value in values.items()}
    for key, figure in figures.items():
        if not math.isfinite(figure.value) or figure.value <= 0:
            raise ValueError(f"calibration value for {key!r} must be positive")
    return figures


def _observation(
    run: Mapping[str, Any],
) -> tuple[str, str, float] | str:
    exclusion = ledger.measurement_exclusion_reason(run)
    if exclusion:
        return exclusion
    try:
        attempt = int(run.get("attempt") or 1)
    except (TypeError, ValueError):
        return "invalid_measurement"
    duration_source = str(run.get("worker_seconds_source") or "").strip()
    if attempt > 1 and duration_source in {"", "wall_fallback"}:
        return "untrustworthy_duration"
    plan = str(run.get("plan") or "").strip()
    agent = agent_configuration_key(run)
    try:
        worker_seconds = float(run.get("worker_seconds"))
    except (TypeError, ValueError):
        return "invalid_measurement"
    if (
        not plan
        or not agent
        or not math.isfinite(worker_seconds)
        or worker_seconds <= 0
    ):
        return "invalid_measurement"
    time_budget = str(run.get("time_budget") or "").strip()
    if time_budget:
        match = _DECLARED_DURATION.fullmatch(time_budget)
        budget_seconds = (
            int(match.group(1)) * _DURATION_UNIT_SECONDS[match.group(2)]
            if match
            else None
        )
        if (
            budget_seconds is not None
            and worker_seconds > budget_seconds * MAX_DECLARED_BUDGET_MULTIPLE
        ):
            return "duration_over_budget"
    return plan, agent, worker_seconds / 3600.0


def _duration_provenance_is_unjudgeable(run: Mapping[str, Any]) -> bool:
    """Return whether a record predates both duration provenance signals."""

    attempt = run.get("attempt")
    duration_source = str(run.get("worker_seconds_source") or "").strip()
    return (attempt is None or str(attempt).strip() == "") and not duration_source


def _confidence(samples: Sequence[float]) -> float:
    """Combine sample depth and multiplicative agreement on a zero-to-one scale."""

    logs = [math.log(value) for value in samples]
    depth = len(samples) / (len(samples) + 2.0)
    agreement = math.exp(-pstdev(logs)) if len(logs) > 1 else 1.0
    return round(depth * agreement, 3)


def _aggregate(samples: Sequence[float]) -> CalibrationFigure:
    value = math.exp(fmean(math.log(sample) for sample in samples))
    return CalibrationFigure(
        value=round(value, 6),
        confidence=_confidence(samples),
        samples=len(samples),
    )


def _eligible_runs(
    runs: Sequence[Mapping[str, Any]],
) -> tuple[
    list[tuple[str, str, float]],
    dict[str, int],
    dict[str, float],
    int,
]:
    included: list[tuple[str, str, float]] = []
    excluded = {
        "scope_changed": 0,
        "stalled": 0,
        "unusable_completion": 0,
        "untrustworthy_duration": 0,
        "duration_over_budget": 0,
        "invalid_measurement": 0,
        "missing_counterpart": 0,
    }
    excluded_worker_hours = {reason: 0.0 for reason in excluded}
    unjudgeable_runs = 0
    for run in runs:
        if _duration_provenance_is_unjudgeable(run):
            unjudgeable_runs += 1
        observation = _observation(run)
        if isinstance(observation, str):
            excluded[observation] += 1
            try:
                worker_seconds = float(run.get("worker_seconds"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(worker_seconds) and worker_seconds > 0:
                excluded_worker_hours[observation] += worker_seconds / 3600.0
        else:
            included.append(observation)
    return included, excluded, excluded_worker_hours, unjudgeable_runs


def _reported_excluded_hours(values: Mapping[str, float]) -> dict[str, float]:
    """Return stable worker-hour totals beside exclusion counts."""
    return {reason: round(hours, 6) for reason, hours in values.items()}


def calibrate_agent_speeds(
    runs: Sequence[Mapping[str, Any]],
    *,
    plan_estimates: Mapping[str, Any],
    agent_speed_factors: Mapping[str, Any] | None = None,
) -> CalibrationResult:
    """Update speed factors while keeping every plan estimate unchanged.

    Speed is neutral estimated hours per actual worker-hour.  A factor above
    one is faster; a factor below one is slower.  This direction is shared by
    the capability cache and every consumer of calibrated speed.
    """

    estimates = _figures(plan_estimates)
    factors = _figures(agent_speed_factors or {})
    observations, excluded, excluded_worker_hours, unjudgeable_runs = _eligible_runs(
        runs
    )
    samples: dict[str, list[float]] = defaultdict(list)
    included = 0
    for plan, agent, actual_hours in observations:
        estimate = estimates.get(plan)
        if estimate is None:
            excluded["missing_counterpart"] += 1
            excluded_worker_hours["missing_counterpart"] += actual_hours
            continue
        samples[agent].append(estimate.value / actual_hours)
        included += 1
    for agent, values in samples.items():
        factors[agent] = _aggregate(values)
    return CalibrationResult(
        estimates,
        factors,
        included,
        excluded,
        _reported_excluded_hours(excluded_worker_hours),
        unjudgeable_runs,
    )


def calibrate_plan_estimates(
    runs: Sequence[Mapping[str, Any]],
    *,
    plan_estimates: Mapping[str, Any],
    agent_speed_factors: Mapping[str, Any],
) -> CalibrationResult:
    """Update plan hours while keeping every worker factor unchanged."""

    estimates = _figures(plan_estimates)
    factors = _figures(agent_speed_factors)
    observations, excluded, excluded_worker_hours, unjudgeable_runs = _eligible_runs(
        runs
    )
    samples: dict[str, list[float]] = defaultdict(list)
    included = 0
    for plan, agent, actual_hours in observations:
        factor = factors.get(agent)
        if factor is None:
            excluded["missing_counterpart"] += 1
            excluded_worker_hours["missing_counterpart"] += actual_hours
            continue
        samples[plan].append(actual_hours * factor.value)
        included += 1
    for plan, values in samples.items():
        estimates[plan] = _aggregate(values)
    return CalibrationResult(
        estimates,
        factors,
        included,
        excluded,
        _reported_excluded_hours(excluded_worker_hours),
        unjudgeable_runs,
    )
