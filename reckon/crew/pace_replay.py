"""Replay committed pace rows without consulting a stream or live receipt.

The dispatch row is the durable account of a lane decision.  This module reads
those rows from committed run files, sends the allowance inputs back through
``reckon.crew.pace``, and reports the lane split and hold evidence in a form a
person or another tool can inspect.  It deliberately has no stream or receipt
reader: a replay that needs either source is not a replay of the record.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon.crew import pace as pace_module

__all__ = [
    "load_committed_rows",
    "render_report",
    "replay",
    "replay_committed_rows",
    "replay_project",
]


@dataclass(frozen=True, slots=True)
class _UnmeasuredAllowance:
    reason: str


_UNMEASURED = _UnmeasuredAllowance("the seven-day clock was not observed")
_UNVERIFIABLE = object()


def load_committed_rows(
    root: str | Path,
    project: str,
) -> list[dict[str, Any]]:
    """Load run records carrying pace rows from one project's run directory.

    The run files are the authority for this report.  Reading them directly
    keeps the report independent of the aggregate index and makes its input
    boundary explicit: no live pointers, streams, receipts, or configuration
    layers are opened.
    """
    run_dir = Path(root).expanduser() / "docs" / "state" / project / "runs"
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*.json")):
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, Mapping):
            raise TypeError(f"committed run {path} is not a JSON object")
        if isinstance(record.get("pace"), Mapping):
            rows.append(dict(record))
    return rows


def replay_committed_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    drain_lead_hours: float | None = None,
) -> dict[str, Any]:
    """Replay committed records and return allowance, hold, and lane checks."""
    return replay(rows, drain_lead_hours=drain_lead_hours)


def replay_project(
    root: str | Path,
    project: str,
    *,
    drain_lead_hours: float | None = None,
) -> dict[str, Any]:
    """Replay every pace-bearing committed run for ``project`` under ``root``."""
    return replay(
        load_committed_rows(root, project),
        drain_lead_hours=drain_lead_hours,
    )


def replay(
    rows: Iterable[Mapping[str, Any]],
    *,
    drain_lead_hours: float | None = None,
) -> dict[str, Any]:
    """Recompute each allowance and validate every recorded hold decision.

    ``drain_lead_hours`` is an optional candidate setting.  Supplying one
    intentionally different from the row's recorded policy makes the report
    show which rows would move, allowing a mistuned setting to be detected from
    the committed evidence alone.
    """
    checks: list[dict[str, Any]] = []
    split: dict[str, dict[str, int]] = defaultdict(
        lambda: {"local": 0, "metered": 0, "unknown": 0, "total": 0}
    )
    records = [dict(row) for row in rows]
    for record in records:
        pace_row = _pace_row(record)
        allowance_value = _recomputed_allowance(
            pace_row,
            drain_lead_hours=drain_lead_hours,
        )
        allowance_unmeasured = isinstance(allowance_value, _UnmeasuredAllowance)
        allowance_unmeasured_reason = (
            allowance_value.reason if allowance_unmeasured else None
        )
        expected_allowance = None if allowance_unmeasured else allowance_value
        recorded_allowance = pace_row.get("allowance")
        allowance_match = not allowance_unmeasured and _same_value(
            expected_allowance, recorded_allowance
        )
        hold_value = _hold_decision(pace_row)
        hold_unverifiable = hold_value is _UNVERIFIABLE
        expected_hold = None if hold_unverifiable else hold_value
        recorded_hold = _recorded_hold_decision(pace_row)
        hold_match = not hold_unverifiable and expected_hold == recorded_hold
        run_id = str(record.get("run_id") or pace_row.get("node") or "")
        checks.append(
            {
                "run_id": run_id,
                "allowance": recorded_allowance,
                "recomputed_allowance": expected_allowance,
                "allowance_match": allowance_match,
                "allowance_unmeasured": allowance_unmeasured,
                "allowance_unmeasured_reason": allowance_unmeasured_reason,
                "hold": pace_row.get("hold"),
                "recomputed_hold": expected_hold,
                "hold_match": hold_match,
                "hold_unverifiable": hold_unverifiable,
            }
        )
        work_class = _work_class(record, pace_row)
        lane_kind = _lane_kind(record, pace_row)
        split[work_class][lane_kind] += 1
        split[work_class]["total"] += 1

    allowance_mismatches = [
        check["run_id"]
        for check in checks
        if not check["allowance_match"] and not check["allowance_unmeasured"]
    ]
    allowance_unmeasured = [
        check["run_id"] for check in checks if check["allowance_unmeasured"]
    ]
    hold_mismatches = [
        check["run_id"]
        for check in checks
        if not check["hold_match"] and not check["hold_unverifiable"]
    ]
    hold_unverifiable = [
        check["run_id"] for check in checks if check["hold_unverifiable"]
    ]
    mistuned = _mistuned_summary(checks, drain_lead_hours)
    result: dict[str, Any] = {
        "rows": checks,
        "row_count": len(checks),
        "allowances": {
            "checked": len(checks),
            "matched": len(checks)
            - len(allowance_mismatches)
            - len(allowance_unmeasured),
            "mismatches": allowance_mismatches,
            "unmeasured": len(allowance_unmeasured),
            "all_match": not allowance_mismatches and not allowance_unmeasured,
        },
        "holds": {
            "checked": len(checks),
            "matched": len(checks) - len(hold_mismatches) - len(hold_unverifiable),
            "mismatches": hold_mismatches,
            "unverifiable": len(hold_unverifiable),
            "all_match": not hold_mismatches and not hold_unverifiable,
        },
        "split_by_class": dict(sorted(split.items())),
        "mistuned": mistuned,
    }
    result["ok"] = (
        result["allowances"]["all_match"]
        and result["holds"]["all_match"]
        and (not mistuned["requested"] or mistuned["detected"])
    )
    result["text"] = render_report(result)
    return result


def render_report(report: Mapping[str, Any]) -> str:
    """Render the replay result as stable, line-oriented human-readable text."""
    allowances = report["allowances"]
    holds = report["holds"]
    lines = [
        f"rows: {report['row_count']}",
        (
            "allowances: "
            f"{allowances['matched']}/{allowances['checked']} reproduced "
            f"({'ok' if allowances['all_match'] else 'mismatch'}"
            f"{'; ' + str(allowances['unmeasured']) + ' unmeasured' if allowances['unmeasured'] else ''})"
        ),
        (
            "holds: "
            f"{holds['matched']}/{holds['checked']} reproduced "
            f"({'ok' if holds['all_match'] else 'mismatch'}"
            f"{'; ' + str(holds['unverifiable']) + ' unverifiable' if holds['unverifiable'] else ''})"
        ),
        "split by class:",
    ]
    for work_class, counts in report["split_by_class"].items():
        lines.append(
            f"  {work_class}: local={counts['local']} "
            f"metered={counts['metered']} unknown={counts['unknown']} "
            f"total={counts['total']}"
        )
    for row in report["rows"]:
        reason = row.get("allowance_unmeasured_reason")
        if reason:
            lines.append(f"unmeasured {row['run_id']}: {reason}")
    mistuned = report["mistuned"]
    if mistuned["requested"]:
        verdict = "detected" if mistuned["detected"] else "not detected"
        lines.append(
            f"mistuned drain_lead_hours={mistuned['candidate']}: {verdict} "
            f"({len(mistuned['mismatches'])} rows moved)"
        )
    else:
        lines.append("mistuned drain_lead_hours: not requested")
    return "\n".join(lines)


def _pace_row(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a record's pace mapping, accepting a pace row for small callers."""
    pace_row = record.get("pace")
    if isinstance(pace_row, Mapping):
        return pace_row
    if "allowance" in record and "clocks" in record:
        return record
    raise ValueError("a replay row must carry a pace mapping")


def _recomputed_allowance(
    row: Mapping[str, Any],
    *,
    drain_lead_hours: float | None,
) -> dict[str, Any] | _UnmeasuredAllowance:
    """Recompute an allowance from the row's clocks through ``pace.py``."""
    clocks = row.get("clocks")
    if not isinstance(clocks, Mapping):
        raise TypeError("a pace row must carry clocks")
    week = clocks.get("seven_day")
    if not isinstance(week, Mapping):
        raise TypeError("a pace row must carry a seven-day clock")
    if week.get("state") != "observed":
        return _UNMEASURED
    if not isinstance(week.get("resets_at"), str) or not week["resets_at"].strip():
        return _UnmeasuredAllowance("the seven-day clock has no reset stamp")
    recorded_at = _instant(row.get("recorded_at"))
    reset_at = _instant(week.get("resets_at"))
    elapsed_hours = max(
        0.0,
        pace_module.WEEK_HOURS - (reset_at - recorded_at).total_seconds() / 3600.0,
    )
    policy = row.get("policy")
    if not isinstance(policy, Mapping):
        raise TypeError("a pace row must carry policy")
    lead = (
        float(drain_lead_hours)
        if drain_lead_hours is not None
        else float(policy["drain_lead_hours"])
    )
    pace = pace_module.PacePolicy(
        drain_lead_hours=lead,
        pace_multiple=float(policy["pace_multiple"]),
    )
    reading = pace_module.GroupReading(
        group=str(row.get("group") or ""),
        utilisation=float(week["utilisation"]),
        elapsed_hours=elapsed_hours,
    )
    return pace_module.allowance_for_group(reading, pace=pace).as_dict()


def _hold_decision(row: Mapping[str, Any]) -> dict[str, Any] | object:
    """Reconstruct the hold verdict from the evidence carried by the row."""
    hold = row.get("hold")
    if hold is None:
        return {"held": False, "backend": None}
    if not isinstance(hold, Mapping):
        raise TypeError("a pace row hold must be an object or null")
    state = hold.get("state")
    threshold = hold.get("effective_ceiling_pct")
    utilisation = state.get("utilisation_pct") if isinstance(state, Mapping) else None
    threshold_value = _number(threshold)
    utilisation_value = _number(utilisation)
    if threshold_value is None or utilisation_value is None:
        return _UNVERIFIABLE
    held = utilisation_value >= threshold_value
    return {"held": held, "backend": hold.get("backend")}


def _recorded_hold_decision(row: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only the decision fields, leaving explanatory evidence intact."""
    hold = row.get("hold")
    if hold is None:
        return {"held": False, "backend": None}
    if not isinstance(hold, Mapping):
        return {"held": False, "backend": None}
    return {"held": bool(hold.get("held")), "backend": hold.get("backend")}


def _mistuned_summary(
    checks: Sequence[Mapping[str, Any]],
    candidate: float | None,
) -> dict[str, Any]:
    """Say whether a candidate lead moves any allowance in the recorded week."""
    mismatches = [
        str(check["run_id"])
        for check in checks
        if candidate is not None
        and not check["allowance_unmeasured"]
        and not _same_value(check["allowance"], check["recomputed_allowance"])
    ]
    return {
        "requested": candidate is not None,
        "candidate": None if candidate is None else float(candidate),
        "detected": bool(mismatches),
        "mismatches": mismatches,
    }


def _work_class(record: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Find the stable work class carried by a run record."""
    for source in (record, row):
        for key in ("work_class", "class", "role"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    definition = record.get("node_definition")
    if isinstance(definition, Mapping):
        value = definition.get("role") or definition.get("work_class")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _lane_kind(record: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Classify a row as local or metered using the recorded lane identity."""
    for source in (record, row):
        value = source.get("lane_kind")
        if isinstance(value, str) and value.strip().lower() in {"local", "metered"}:
            return value.strip().lower()
    if record.get("local") is True or row.get("local") is True:
        return "local"
    backend = str(record.get("backend") or row.get("lane") or "").strip()
    if not backend:
        return "unknown"
    return "local" if ledger.is_unmetered_backend(backend) else "metered"


def _same_value(left: Any, right: Any) -> bool:
    """Compare JSON-shaped values while allowing harmless floating-point drift."""
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _same_value(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_value(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-12)
    return left == right


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _instant(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"a pace row needs an ISO instant, not {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
