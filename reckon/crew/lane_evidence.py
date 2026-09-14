"""Return shape-conditioned lane evidence without choosing a lane.

Gate outcomes are deliberately absent: they are saturated across the observed
lanes and cannot distinguish how a lane handles a flawed premise. Rework is
also absent because a later touch does not distinguish a defect from a useful
adjacent finding. Instead, corrections to a brief are counted per recorded
attempt, so resumed attempts remain visible rather than being collapsed into
one node-level value.

The returned rows are evidence, not a routing policy. This module never ranks,
selects, or recommends a lane; an orchestrator makes that decision.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any

from reckon.crew.quota_weight import (
    RelativeQuotaWeight,
    RequestTokenUsage,
    quota_weight,
)

MINIMUM_LANE_SAMPLE = 10

# The window period a lane's quota probe measures, and how close two refusals
# may sit to it and still count as the short window resetting between them.
SHORT_WINDOW_HOURS = 5.0
RESET_CROSSING_TOLERANCE = 0.30


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _committed(row: Mapping[str, Any]) -> bool:
    return any(str(commit).strip() for commit in row.get("commits") or ())


def _lane(row: Mapping[str, Any]) -> str:
    backend = str(row.get("backend") or "").strip()
    if backend:
        return backend
    agent = row.get("agent")
    if isinstance(agent, Mapping):
        backend = str(agent.get("backend") or "").strip()
    return backend or "unknown"


def _model(row: Mapping[str, Any]) -> str:
    model = str(row.get("model") or "").strip()
    if model:
        return model
    agent = row.get("agent")
    if isinstance(agent, Mapping):
        return str(agent.get("model") or "").strip()
    return ""


def _request_usage(row: Mapping[str, Any]) -> Sequence[RequestTokenUsage] | None:
    raw = row.get("quota_requests")
    if raw is None:
        budget = row.get("budget")
        raw = budget.get("quota_requests") if isinstance(budget, Mapping) else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        return None

    requests: list[RequestTokenUsage] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        input_tokens = item.get("input_tokens")
        output_tokens = item.get("output_tokens")
        if (
            not isinstance(input_tokens, int)
            or isinstance(input_tokens, bool)
            or not isinstance(output_tokens, int)
            or isinstance(output_tokens, bool)
            or input_tokens < 0
            or output_tokens < 0
        ):
            return None
        requests.append(RequestTokenUsage(input_tokens, output_tokens))
    return requests


def _metric(
    value: float | None, sample_size: int, *, state: str = "measured"
) -> dict[str, object]:
    return {"state": state, "sample_size": sample_size, "value": value}


def _insufficient(sample_size: int) -> dict[str, object]:
    return _metric(None, sample_size, state="insufficient_evidence")


def _unmeasured(sample_size: int) -> dict[str, object]:
    return _metric(None, sample_size, state="unmeasured")


def _disputes(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    values = [_number(row.get("dispute_count")) for row in rows]
    if any(value is None or value < 0 for value in values):
        return _unmeasured(sum(value is not None for value in values))
    if len(values) < MINIMUM_LANE_SAMPLE:
        return _insufficient(len(values))
    return _metric(
        sum(value for value in values if value is not None) / len(values), len(values)
    )


def _durable_metric(
    rows: Sequence[Mapping[str, Any]], *, field: str
) -> dict[str, object]:
    durable = [row for row in rows if _committed(row)]
    values = [_number(row.get(field)) for row in rows]
    if any(value is None or value < 0 for value in values):
        return _unmeasured(len(durable))
    if len(durable) < MINIMUM_LANE_SAMPLE:
        return _insufficient(len(durable))
    return _metric(
        sum(value for value in values if value is not None) / len(durable),
        len(durable),
    )


def _cost(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    durable_count = sum(_committed(row) for row in rows)
    weights: list[float] = []
    for row in rows:
        requests = _request_usage(row)
        model = _model(row)
        if requests is None or not model:
            return _unmeasured(durable_count)
        result = quota_weight(model, requests)
        if not isinstance(result, RelativeQuotaWeight):
            return _unmeasured(durable_count)
        weights.append(result.weight)
    if durable_count < MINIMUM_LANE_SAMPLE:
        return _insufficient(durable_count)
    return _metric(sum(weights) / durable_count, durable_count)


def _row(lane: str | None, rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    sample_size = len(rows)
    if sample_size < MINIMUM_LANE_SAMPLE:
        metrics = {
            "dispute_count_per_attempt": _insufficient(sample_size),
            "cost_per_durable_node": _insufficient(sample_size),
            "wall_seconds_per_durable_node": _insufficient(sample_size),
        }
        return {
            "lane": lane,
            "sample_size": sample_size,
            "state": "insufficient_evidence",
            "minimum_sample_size": MINIMUM_LANE_SAMPLE,
            **metrics,
        }
    return {
        "lane": lane,
        "sample_size": sample_size,
        "state": "measured",
        "minimum_sample_size": MINIMUM_LANE_SAMPLE,
        "dispute_count_per_attempt": _disputes(rows),
        "cost_per_durable_node": _cost(rows),
        "wall_seconds_per_durable_node": _durable_metric(rows, field="wall_seconds"),
    }


def lane_evidence(
    ledger_rows: Iterable[Mapping[str, Any]], role: str, spec_level: str
) -> dict[str, object]:
    """Return per-lane evidence for exactly one declared node shape.

    Callers obtain ``ledger_rows`` through :func:`reckon.ledger.read_records`.
    This function intentionally accepts those rows rather than opening a ledger
    file itself, keeping the source and revision of the evidence explicit.
    """
    shape = {"role": str(role), "spec_level": str(spec_level)}
    lanes: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        if str(row.get("role") or "") != shape["role"]:
            continue
        if str(row.get("spec_level") or "") != shape["spec_level"]:
            continue
        lanes[_lane(row)].append(row)

    rows = [_row(lane, lanes[lane]) for lane in sorted(lanes)]
    if not rows:
        rows = [_row(None, ())]
    return {"shape": shape, "lanes": rows}


def _moment(value: object) -> datetime | None:
    """Parse an ISO timestamp, or None when it cannot be read."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def infer_binding_window(
    stamps: Sequence[Mapping[str, Any]],
    lane: str,
    *,
    short_window_hours: float = SHORT_WINDOW_HOURS,
    reset_crossing_tolerance: float = RESET_CROSSING_TOLERANCE,
) -> dict[str, object]:
    """Place the quota window whose exhaustion binds a lane, or say it is unknown.

    A lane that reports no quota reading is constrained by whichever window
    keeps refusing it, and only the lane's own refusals can say which. Two
    refusals whose separation matches the short window's own period are a reset
    crossing: the short window came back and the lane still refused, so the
    weekly window is what binds. Two refusals separated by far more show a lane
    that was served after the short window returned and then drained it again,
    so the short window binds. A single refusal cannot distinguish the two and
    is reported as undetermined rather than guessed.

    Callers pass ``stamps`` as returned by ``RunStore.refusal_stamps()``; the
    lane's own records are selected here because the store carries every lane's
    refusals in one list. Each refusal carries the time it happened and the
    return time it itself stated.
    """
    chrono: list[tuple[datetime, Mapping[str, Any]]] = []
    for stamp in stamps:
        if stamp.get("lane") != lane:
            continue
        moment = _moment(stamp.get("refused_at"))
        if moment is None:
            continue
        chrono.append((moment, stamp))
    chrono.sort(key=lambda item: item[0])

    if len(chrono) < 2:
        return {
            "lane": lane,
            "binding_window": "undetermined",
            "refusal_count": len(chrono),
            "reason": (
                "one refusal cannot say whether the short window reset between "
                "refusals; only a second refusal can"
            ),
        }

    lower = short_window_hours * (1.0 - reset_crossing_tolerance)
    upper = short_window_hours * (1.0 + reset_crossing_tolerance)
    pairs: list[dict[str, object]] = []
    crossed_reset = False
    for (first_moment, _), (second_moment, second) in pairwise(chrono):
        gap_hours = (second_moment - first_moment).total_seconds() / 3600.0
        weekly = lower <= gap_hours <= upper
        crossed_reset = crossed_reset or weekly
        pairs.append(
            {
                "refused_at": second.get("refused_at"),
                "returns_at": second.get("returns_at"),
                "gap_hours": gap_hours,
                "reset_crossed": weekly,
            }
        )

    return {
        "lane": lane,
        "binding_window": "weekly" if crossed_reset else "short",
        "refusal_count": len(chrono),
        "pairs": pairs,
        "reason": (
            "the short window reset between refusals and the lane refused anyway"
            if crossed_reset
            else "the lane was served after the short window returned and then refused again"
        ),
    }
