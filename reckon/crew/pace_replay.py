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
from datetime import datetime
from pathlib import Path
from typing import Any

from reckon import budget as budget_module
from reckon import ledger
from reckon._timestamps import parse_utc
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
        evidence = _read_row(pace_row)
        allowance_value = _recomputed_allowance(
            evidence,
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


# The kinds a field of a pace row is read as.  A trailing ``?`` admits a null as
# a value of its own -- "no hold fired", "nothing to say" -- while an unmarked
# kind that is absent, null or blank is a row that cannot be measured.
_TEXT = "text"
_NUMBER = "number"
_INSTANT = "instant"
_OBJECT = "object"
_BOOLEAN = "boolean"
_OBSERVED = "observed"

_CLOCK_PERIODS: tuple[str, ...] = ("five_hour", "seven_day")
_FIVE_HOUR_PERIOD = "five_hour"

# The shape a replay reads a pace row in: every path this module converts or
# dereferences, and the fields it does not derive from as well.  Reading the row
# whole is deliberate.  The row is one record of one decision, so a damaged field
# anywhere in it leaves the row unmeasured rather than half-read, and a field a
# later version of this module starts to use cannot arrive unguarded.  The order
# is the order a reader hears about failures in, so the reported reason names the
# field that actually stopped the reading.
_ROW_FIELDS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (
        ("recorded_at",),
        _INSTANT,
        "the pace row recorded_at stamp",
        "the pace row has no recorded instant",
    ),
    (
        ("policy",),
        _OBJECT,
        "the recorded pace policy",
        "the pace row carries no policy",
    ),
    (
        ("policy", "drain_lead_hours"),
        _NUMBER,
        "the recorded drain_lead_hours",
        "the pace row's policy names no drain_lead_hours",
    ),
    (
        ("policy", "pace_multiple"),
        _NUMBER,
        "the recorded pace_multiple",
        "the pace row's policy names no pace_multiple",
    ),
    (
        ("group",),
        _TEXT,
        "the pace row's budget group",
        "the pace row carries no budget group",
    ),
    (("lane",), _TEXT, "the pace row's lane", "the pace row names no lane"),
    (("node",), _TEXT, "the pace row's node", "the pace row names no node"),
    (
        ("score",),
        _NUMBER,
        "the recorded open-endedness score",
        "the pace row carries no score",
    ),
    (
        ("state",),
        _TEXT,
        "the pace row's pace state",
        "the pace row carries no pace state",
    ),
    (
        ("source",),
        _TEXT,
        "the pace row's window source",
        "the pace row carries no window source",
    ),
    (
        ("member",),
        _TEXT,
        "the pace row's wallet member",
        "the pace row carries no wallet member",
    ),
    (
        ("allowance",),
        _OBJECT,
        "the recorded allowance",
        "the pace row records no allowance",
    ),
    (("bar",), _OBJECT, "the recorded bar", "the pace row records no bar"),
    (("hold",), _OBJECT + "?", "the recorded hold", "the pace row carries no hold"),
    (
        ("reason",),
        _TEXT + "?",
        "the pace row's reason",
        "the pace row carries no reason",
    ),
)

# The figures one metered clock is read as: required of an observed clock, and
# admitted as null otherwise, because an unobserved clock reports absence rather
# than a position.  Each carries the words a reader is given for it.  The window
# length rides the clock because the allowance is derived from the period the
# provider reported rather than from a fixed week, so a reader that does not read
# the length cannot reproduce the derivation.
_CLOCK_FIGURES: tuple[tuple[str, str, str], ...] = (
    ("utilisation", _NUMBER, "utilisation"),
    ("observed_at", _INSTANT, "observation stamp"),
    ("resets_at", _INSTANT, "reset stamp"),
    ("window_minutes", _NUMBER, "window length"),
)


@dataclass(frozen=True, slots=True)
class _RowEvidence:
    """The figures a replay derives from, read once through the guarded reader.

    The derivation consumes values the reader has already passed rather than
    reaching back into the row, so the shape that was checked is the shape that
    is used.  Both clocks are carried, because the operative window length and
    the branch the producer took are read from them: an account with a short
    sibling is paced through the week derivation, and a primary-only account by
    the elapsed fraction of the one window it reports.
    """

    recorded_at: datetime
    group: str
    lead_hours: float
    pace_multiple: float
    primary_only: bool
    five_hour: Mapping[str, Any]
    seven_day: Mapping[str, Any]


def _read(
    row: Any,
    path: tuple[str, ...],
    kind: str,
    *,
    label: str,
    missing: str,
) -> Any | _UnmeasuredAllowance:
    """Read one pace-row field, or say why it cannot be read as measured.

    This is the one door every row-sourced value in this module comes through: a
    figure is a float here or the row is unmeasured, a stamp is a datetime here
    or the row is unmeasured, and a mapping is indexed only after it has come
    through.  The reader never coerces a value it cannot read and never returns
    half of one.  A damaged field yields an unmeasured row carrying that field's
    reason, because a week's report that dies on one bad row tells its reader
    nothing, and a report that silently drops the row tells them less.
    """
    value: Any = row
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return _UnmeasuredAllowance(missing)
        value = value[key]
    nullable = kind.endswith("?")
    bare = kind[:-1] if nullable else kind
    if value is None or (isinstance(value, str) and not value.strip()):
        return None if nullable else _UnmeasuredAllowance(missing)
    if bare == _TEXT:
        if not isinstance(value, str):
            return _UnmeasuredAllowance(f"{label} is not text: {value!r}")
        return value.strip()
    if bare == _NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _UnmeasuredAllowance(f"{label} is not a number: {value!r}")
        figure = float(value)
        if not math.isfinite(figure) or figure < 0.0:
            return _UnmeasuredAllowance(f"{label} is not a usable figure: {value!r}")
        return figure
    if bare == _INSTANT:
        if not isinstance(value, str):
            return _UnmeasuredAllowance(f"{label} is not an instant: {value!r}")
        moment = parse_utc(value)
        if moment is None:
            return _UnmeasuredAllowance(f"{label} is not a valid instant: {value!r}")
        return moment
    if bare == _OBJECT:
        if not isinstance(value, Mapping) or not value:
            return _UnmeasuredAllowance(f"{label} is not an object carrying evidence")
        return value
    if bare == _BOOLEAN:
        if not isinstance(value, bool):
            return _UnmeasuredAllowance(f"{label} is not a boolean: {value!r}")
        return value
    raise ValueError(f"{kind!r} is not a kind this reader knows")


def _raw_at(row: Any, path: tuple[str, ...]) -> Any:
    """Return a row value as the record wrote it, for verbatim comparison.

    Only ever called for a path :func:`_read` has already passed, so the value
    exists and carries the kind it was checked as; this returns it uncoerced so
    a stamp the derivation copies keeps the producer's own text.
    """
    value: Any = row
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _read_clock(row: Any, period: str) -> dict[str, Any] | _UnmeasuredAllowance:
    """Read one metered clock, requiring its figures of an observed one only."""
    named = period.replace("_", "-")
    container = _read(
        row,
        ("clocks", period),
        _OBJECT,
        label=f"the {named} clock",
        missing=f"the pace row carries no {named} clock",
    )
    if isinstance(container, _UnmeasuredAllowance):
        return container
    clock: dict[str, Any] = {}
    for name in ("state", "period"):
        value = _read(
            row,
            ("clocks", period, name),
            _TEXT,
            label=f"the {named} clock {name}",
            missing=f"the pace row carries no {named} clock {name}",
        )
        if isinstance(value, _UnmeasuredAllowance):
            return value
        clock[name] = value
    for name, kind, said in _CLOCK_FIGURES:
        required = kind if clock["state"] == _OBSERVED else kind + "?"
        value = _read(
            row,
            ("clocks", period, name),
            required,
            label=f"the {named} clock {name}",
            missing=f"the {named} clock has no {said}",
        )
        if isinstance(value, _UnmeasuredAllowance):
            return value
        clock[name] = value
    # The stamps are recorded as the producer wrote them, because the allowance
    # copied them verbatim and a replay that re-rendered a parsed instant would
    # report a disagreement over formatting rather than over the figure.
    for name in ("observed_at", "resets_at"):
        text = _raw_at(row, ("clocks", period, name))
        clock[f"{name}_text"] = text if isinstance(text, str) else None
    age = _read(
        row,
        ("clocks", period, "age_seconds"),
        _NUMBER + "?",
        label=f"the {named} clock age",
        missing=f"the {named} clock carries no age",
    )
    if isinstance(age, _UnmeasuredAllowance):
        return age
    clock["age_seconds"] = age
    return clock


def _read_row(row: Mapping[str, Any]) -> _RowEvidence | _UnmeasuredAllowance:
    """Read a whole pace row through the guarded reader, first failure first.

    Both metered clocks are read whatever the row's shape, because which one
    paced the group is read from them rather than assumed: an account with a
    genuine short sibling is paced through the week derivation, and a
    primary-only account -- whose one reported window sits where a five-hour
    clock would, under the period name ``primary`` -- is paced by that window's
    own elapsed fraction.  A row whose short sibling is present but whose weekly
    clock was not observed cannot be reproduced and is unmeasured.
    """
    clocks: dict[str, Any] = {}
    for period in _CLOCK_PERIODS:
        clock = _read_clock(row, period)
        if isinstance(clock, _UnmeasuredAllowance):
            return clock
        clocks[period] = clock
    primary_only = clocks["five_hour"]["period"] != _FIVE_HOUR_PERIOD
    if not primary_only and clocks["seven_day"]["state"] != _OBSERVED:
        return _UNMEASURED
    values: dict[tuple[str, ...], Any] = {}
    for path, kind, label, missing in _ROW_FIELDS:
        value = _read(row, path, kind, label=label, missing=missing)
        if isinstance(value, _UnmeasuredAllowance):
            return value
        values[path] = value
    if isinstance(values[("hold",)], Mapping):
        # A hold that fired records the decision it made; a row that reports a
        # hold without one is a row whose decision cannot be read back.
        decision = _read(
            row,
            ("hold", "held"),
            _BOOLEAN,
            label="the recorded hold decision",
            missing="the recorded hold carries no decision",
        )
        if isinstance(decision, _UnmeasuredAllowance):
            return decision
    return _RowEvidence(
        recorded_at=values[("recorded_at",)],
        group=values[("group",)],
        lead_hours=values[("policy", "drain_lead_hours")],
        pace_multiple=values[("policy", "pace_multiple")],
        primary_only=primary_only,
        five_hour=clocks["five_hour"],
        seven_day=clocks["seven_day"],
    )


def _operative_clock(evidence: _RowEvidence) -> Mapping[str, Any]:
    """Return the clock whose window the group was paced by.

    The producer derives from the longest provider-reported window, and the row
    carries each clock's own length, so the operative clock is the one with the
    greater length.  A length tie is settled on the weekly clock, matching the
    producer's preference for the longer horizon.
    """
    five = evidence.five_hour
    week = evidence.seven_day
    five_minutes = five["window_minutes"] or 0.0
    week_minutes = week["window_minutes"] or 0.0
    return week if week_minutes >= five_minutes else five


def _recomputed_allowance(
    evidence: _RowEvidence | _UnmeasuredAllowance,
    *,
    drain_lead_hours: float | None,
) -> dict[str, Any] | _UnmeasuredAllowance:
    """Recompute an allowance from the row's own clocks, under its own window.

    The window the provider reported is what the derivation divides: a group
    carrying a genuine short sibling keeps the week derivation over its weekly
    reset, and a primary-only group's allowance is the elapsed fraction of its
    one window leaned by the pace multiple.  Both carry the operative length and
    reset forward, so the reproduced allowance is the recorded one rather than an
    approximation of it.
    """
    if isinstance(evidence, _UnmeasuredAllowance):
        return evidence
    operative = _operative_clock(evidence)
    window_minutes = operative["window_minutes"]
    if not isinstance(window_minutes, float) or window_minutes <= 0:
        return _UnmeasuredAllowance("the operative window length was not read")
    reset = operative["resets_at"]
    if reset is None:
        return _UnmeasuredAllowance("the operative window has no readable reset")
    window_hours = window_minutes / 60.0
    remaining_hours = (reset - evidence.recorded_at).total_seconds() / 3600.0
    elapsed_hours = max(0.0, window_hours - remaining_hours)
    elapsed_fraction = min(1.0, elapsed_hours / window_hours)
    multiple = evidence.pace_multiple
    utilisation = operative["utilisation"]
    floor_reason = budget_module._burn_floor_reason(
        float(utilisation) * 100.0, elapsed_fraction
    )
    if floor_reason is not None:
        allowance = budget_module._unknown_allowance(
            evidence.group, floor_reason, utilisation=float(utilisation)
        )
        allowance.update(
            state=_OBSERVED,
            elapsed_hours=elapsed_hours,
            drain_hours=window_hours,
            remaining_budget=max(0.0, 1.0 - float(utilisation)),
            pace_multiple=float(multiple),
            window_minutes=window_minutes,
            elapsed_fraction=elapsed_fraction,
            observed_at=operative["observed_at_text"],
            resets_at=operative["resets_at_text"],
        )
        return allowance
    burn = None if elapsed_fraction <= 0 else float(utilisation) / elapsed_fraction
    if evidence.primary_only:
        derived = min(1.0, elapsed_fraction * float(multiple))
        return {
            "group": evidence.group,
            "state": _OBSERVED,
            "reason": None,
            "utilisation": float(utilisation),
            "elapsed_hours": elapsed_hours,
            "drain_hours": window_hours,
            "remaining_budget": max(0.0, 1.0 - float(utilisation)),
            "remaining_windows": None,
            "pace_multiple": float(multiple),
            "derived": derived,
            "provider_ceiling": None,
            "effective_limit": derived,
            "limited_by": "allowance",
            "window_minutes": window_minutes,
            "elapsed_fraction": elapsed_fraction,
            "burn_multiple": burn,
            "observed_at": operative["observed_at_text"],
            "resets_at": operative["resets_at_text"],
        }
    week = evidence.seven_day
    week_reset = week["resets_at"]
    week_elapsed = max(
        0.0,
        pace_module.WEEK_HOURS
        - (week_reset - evidence.recorded_at).total_seconds() / 3600.0,
    )
    lead = evidence.lead_hours if drain_lead_hours is None else drain_lead_hours
    pace = pace_module.PacePolicy(
        drain_lead_hours=lead,
        pace_multiple=multiple,
    )
    reading = pace_module.GroupReading(
        group=evidence.group,
        utilisation=week["utilisation"],
        elapsed_hours=week_elapsed,
    )
    allowance = pace_module.allowance_for_group(reading, pace=pace).as_dict()
    allowance.update(
        {
            "state": _OBSERVED,
            "reason": None,
            "window_minutes": window_minutes,
            "elapsed_fraction": elapsed_fraction,
            "burn_multiple": burn,
            "observed_at": operative["observed_at_text"],
            "resets_at": operative["resets_at_text"],
        }
    )
    return allowance


def _hold_decision(row: Mapping[str, Any]) -> dict[str, Any] | object:
    """Reconstruct the hold verdict from the evidence carried by the row."""
    hold = _read(row, ("hold",), _OBJECT + "?", label="the recorded hold", missing="")
    if not isinstance(hold, Mapping):
        return {"held": False, "backend": None}
    threshold = _read(
        row,
        ("hold", "effective_ceiling_pct"),
        _NUMBER + "?",
        label="the recorded hold ceiling",
        missing="the recorded hold names no ceiling",
    )
    utilisation = _read(
        row,
        ("hold", "state", "utilisation_pct"),
        _NUMBER + "?",
        label="the recorded hold utilisation",
        missing="the recorded hold names no utilisation",
    )
    backend = _read(
        row,
        ("hold", "backend"),
        _TEXT + "?",
        label="the recorded hold backend",
        missing="the recorded hold names no backend",
    )
    if (
        not isinstance(threshold, float)
        or not isinstance(utilisation, float)
        or isinstance(backend, _UnmeasuredAllowance)
    ):
        return _UNVERIFIABLE
    return {"held": utilisation >= threshold, "backend": backend}


def _recorded_hold_decision(row: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only the decision fields, leaving explanatory evidence intact."""
    hold = _read(row, ("hold",), _OBJECT + "?", label="the recorded hold", missing="")
    if not isinstance(hold, Mapping):
        return {"held": False, "backend": None}
    held = _read(
        row,
        ("hold", "held"),
        _BOOLEAN + "?",
        label="the recorded hold decision",
        missing="the recorded hold carries no decision",
    )
    backend = _read(
        row,
        ("hold", "backend"),
        _TEXT + "?",
        label="the recorded hold backend",
        missing="the recorded hold names no backend",
    )
    return {
        "held": held is True,
        "backend": None if isinstance(backend, _UnmeasuredAllowance) else backend,
    }


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
        "candidate": candidate,
        "detected": bool(mismatches),
        "mismatches": mismatches,
    }


def _work_class(record: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Find the stable work class carried by a run record."""
    for source in (record, row):
        for key in ("work_class", "class", "role"):
            value = _read(
                source,
                (key,),
                _TEXT + "?",
                label=f"the recorded {key}",
                missing=f"the record names no {key}",
            )
            if isinstance(value, str) and value:
                return value
    definition = _read(
        record,
        ("node_definition",),
        _OBJECT + "?",
        label="the record's node definition",
        missing="the record names no node definition",
    )
    if isinstance(definition, Mapping):
        for key in ("role", "work_class"):
            value = _read(
                definition,
                (key,),
                _TEXT + "?",
                label=f"the node definition's {key}",
                missing=f"the node definition names no {key}",
            )
            if isinstance(value, str) and value:
                return value
    return "unknown"


def _lane_kind(record: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Classify a row as local or metered using the recorded lane identity."""
    for source in (record, row):
        kind = _read(
            source,
            ("lane_kind",),
            _TEXT + "?",
            label="the recorded lane kind",
            missing="the record names no lane kind",
        )
        if isinstance(kind, str) and kind.lower() in {"local", "metered"}:
            return kind.lower()
    for source in (record, row):
        local = _read(
            source,
            ("local",),
            _BOOLEAN + "?",
            label="the recorded local flag",
            missing="the record carries no local flag",
        )
        if local is True:
            return "local"
    for source, key in ((record, "backend"), (row, "lane")):
        lane = _read(
            source,
            (key,),
            _TEXT + "?",
            label=f"the recorded {key}",
            missing=f"the record names no {key}",
        )
        if isinstance(lane, str) and lane:
            return "local" if ledger.is_unmetered_backend(lane) else "metered"
    return "unknown"


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
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)
    return left == right
