"""Measure picker decisions against their own promoted run outcomes.

Success is a recorded ``gate == "passed"`` or, when the gate was not run,
an outcome of ``passed``, ``complete``, ``done``, ``success``, or a review
scored at least 80. A failed gate always fails. All other rows have unknown success and
are excluded from success-rate denominators, never counted as failures. A
run's outcome is attributed to a route when its recorded route mode is picker.
Older rows with no route mode use the backend match as an approximate attribution.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from reckon import flight, ledger
from reckon._timestamps import parse_utc
from reckon.crew.run_time_profile import _number, _percentile, run_time_profile

REVIEW_SUCCESS_SCORE = 80
SUCCESS_RULE = (
    "gate passed succeeds; gate failed fails; with no closed gate, outcome "
    f"passed/complete/done/success or review scored at least {REVIEW_SUCCESS_SCORE} "
    "succeeds; a lower review score fails; otherwise unknown"
)
BUCKETS = ("below_0.5", "0.5_to_0.7", "0.7_to_0.85", "0.85_and_above")
BURN_LEVELS = ("below_1", "1_to_2", "2_to_4", "4_and_above", "unknown")

# The route mode a run records when the picker held but the dispatch went ahead
# anyway. A picker-routed dispatch refuses on a hold, so a run carrying a
# holding selection only exists because the operator named a lane or forced
# deterministic routing past the picker's answer; naming it apart lets a reader
# count how often a hold is overruled instead of folding it into the shadow
# picks.
OVERRIDDEN_HOLD_ROUTE_MODE = "overridden-hold"


def overridden_hold(selection: object) -> bool:
    """Whether a recorded picker answer was a hold the dispatch went past."""
    return isinstance(selection, Mapping) and selection.get("action") == "hold"


def _success(row: Mapping[str, Any]) -> bool | None:
    gate = str(row.get("gate") or "").lower()
    if gate == "passed":
        return True
    if gate == "failed":
        return False
    outcome = str(row.get("outcome") or "").strip().lower()
    if outcome in {"passed", "complete", "done", "success"}:
        return True
    if match := re.match(r"review scored (\d+)\b", outcome):
        return int(match.group(1)) >= REVIEW_SUCCESS_SCORE
    return None


def _confidence_bucket(value: float) -> str:
    if value < 0.5:
        return BUCKETS[0]
    if value < 0.7:
        return BUCKETS[1]
    if value < 0.85:
        return BUCKETS[2]
    return BUCKETS[3]


def _burn_level(value: float) -> str:
    if value < 1:
        return BURN_LEVELS[0]
    if value < 2:
        return BURN_LEVELS[1]
    if value < 4:
        return BURN_LEVELS[2]
    return BURN_LEVELS[3]


def _fallback_kind(reason: object) -> str:
    value = str(reason or "").lower()
    if "timeout" in value:
        return "timeout"
    if "jev-error" in value:
        return "jev-error"
    if "input" in value:
        return "input-error"
    return "other"


def _codex_lane(row: Mapping[str, Any]) -> bool:
    """Whether a run ran on the codex lane, joined through the catalogue.

    A row is joined on the lane its backend names, not on a name prefix: the
    lane's declared name, its models, an old alias and a model id all resolve to
    the codex lane, while a backend whose name merely begins with ``codex``
    reaches it only when the catalogue says it does. A backend the catalogue
    cannot resolve is its own lane, so an unrecognised name is never folded into
    codex.
    """

    name = str(row.get("backend") or "")
    lane = str(row.get("lane") or "")
    if not lane:
        try:
            lane, _ = ledger.resolve_name(name)
        except ValueError:
            lane = None
        lane = lane or name
    return lane == "codex"


def _hold_path(docs: Path, project: str) -> Path:
    return docs / "state" / project / "picker-holds.jsonl"


def record_picker_hold(
    *,
    project: str,
    docs: Path,
    node: str,
    plan: str,
    selection: Mapping[str, Any],
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one run-free hold with an exclusive lock and durable flush."""
    path = _hold_path(docs, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "project": project,
        "node": node,
        "plan": plan,
        "held_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "confidence": _number(selection.get("confidence")),
        "reasons": [reason],
    }
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream, fcntl.LOCK_UN)
    return record


def _read_holds(docs: Path, project: str) -> tuple[list[dict[str, Any]], int]:
    path = _hold_path(docs, project)
    if not path.is_file():
        return [], 0
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(item, dict) and parse_utc(item.get("held_at")) is not None:
            records.append(item)
        else:
            malformed += 1
    return records, malformed


def summarize(
    rows_by_project: Mapping[str, list[dict[str, Any]]],
    holds_by_project: Mapping[str, list[dict[str, Any]]],
    *,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Derive counts from valid decisions; missing outcomes remain unknown."""
    boundary = parse_utc(since) if since else None
    if since and boundary is None:
        raise ValueError(f"since must be an ISO timestamp: {since!r}")
    actions: Counter[str] = Counter()
    route_modes: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    latency: dict[str, list[float]] = defaultdict(list)
    groups: dict[tuple[str, str, str, str, str | None], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    calibration: dict[str, list[bool | None]] = defaultdict(list)
    burn: dict[str, list[bool]] = defaultdict(list)
    codex_runs: list[dict[str, Any]] = []
    malformed = 0
    for project, rows in rows_by_project.items():
        for row in rows:
            if not isinstance(row, Mapping):
                malformed += 1
                continue
            stamp = parse_utc(row.get("completed_at"))
            if boundary and (stamp is None or stamp < boundary):
                continue
            selection = row.get("picker_selection")
            if not isinstance(selection, Mapping):
                if selection is not None:
                    malformed += 1
                continue
            action = selection.get("action")
            if action not in {"route", "hold", "fallback", "refuse"}:
                malformed += 1
                continue
            actions[action] += 1
            if action == "fallback":
                fallbacks[_fallback_kind(selection.get("fallback_reason"))] += 1
            ms = _number(selection.get("latency_ms"))
            if ms is not None and ms >= 0:
                latency["overall"].append(ms)
                latency[project].append(ms)
            success = _success(row)
            confidence = _number(selection.get("confidence"))
            matching_selection = (
                action == "route"
                and bool(selection.get("backend"))
                and selection.get("backend") == row.get("backend")
            )
            mode = row.get("route_mode")
            route_modes[str(mode) if mode else "unrecorded"] += 1
            routed = matching_selection and mode == "picker"
            approximate = matching_selection and mode is None
            attribution = "approximate" if mode is None else "picker"
            if routed and confidence is not None and 0 <= confidence <= 1:
                calibration[_confidence_bucket(confidence)].append(success)
            if routed or approximate:
                backend = str(row.get("backend") or selection.get("backend") or "")
                role = str(row.get("role") or "")
                spec = str(row.get("spec_level") or "")
                definition = row.get("node_definition")
                risk = row.get("risk") or (
                    definition.get("risk") if isinstance(definition, Mapping) else None
                )
                groups[
                    (
                        project,
                        backend,
                        role,
                        spec,
                        str(risk) if risk else None,
                        attribution,
                    )
                ].append(dict(row))
            offered = selection.get("offered")
            if isinstance(offered, list):
                codex_offers = [
                    c
                    for c in offered
                    if isinstance(c, Mapping) and c.get("family") == "codex"
                ]
                if codex_offers:
                    measured = [
                        value
                        for c in codex_offers
                        if (value := _number(c.get("burn_multiple"))) is not None
                        and value >= 0
                    ]
                    level = _burn_level(min(measured)) if measured else "unknown"
                    burn[level].append(routed and _codex_lane(row))
            if routed and _codex_lane(row):
                chosen = next(
                    (
                        c
                        for c in offered or []
                        if isinstance(c, Mapping)
                        and c.get("backend") == row.get("backend")
                    ),
                    None,
                )
                codex_runs.append(
                    {
                        "project": project,
                        "run_id": row.get("run_id"),
                        "burn_multiple": chosen.get("burn_multiple")
                        if chosen
                        else None,
                        "pace_allowance": chosen.get("pace_allowance")
                        if chosen
                        else None,
                    }
                )
    group_rows: list[dict[str, Any]] = []
    for (project, backend, role, spec, risk, attribution), members in sorted(
        groups.items(), key=lambda item: str(item[0])
    ):
        known = [value for row in members if (value := _success(row)) is not None]
        scores = [
            score
            for row in members
            if isinstance(row.get("review"), Mapping)
            and (score := _number(row["review"].get("total"))) is not None
        ]
        walls = [
            wall
            for row in members
            if (wall := _number(row.get("wall_seconds"))) is not None
        ]
        profile_groups = (profiles or {}).get(project, {}).get("groups", [])
        matched = [
            g
            for g in profile_groups
            if g.get("backend") == backend
            and g.get("role") == role
            and g.get("spec_level") == spec
        ]
        p50 = (
            max(matched, key=lambda g: g.get("runs") or 0).get("wall_seconds_median")
            if matched
            else None
        )
        group_rows.append(
            {
                "project": project,
                "backend": backend,
                "role": role,
                "spec_level": spec,
                "risk": risk,
                "attribution": attribution,
                "count": len(members),
                "known_outcomes": len(known),
                "success_rate": sum(known) / len(known) if known else None,
                "review_score_median": median(scores) if scores else None,
                "repair_or_resume_count": sum(
                    bool(
                        r.get("repairs")
                        or r.get("resume_remedy")
                        or r.get("attempt_kind") in {"repair", "resume", "redispatch"}
                    )
                    for r in members
                ),
                "wall_seconds_median": median(walls) if walls else None,
                "profile_p50_seconds": p50,
                "wall_vs_profile_p50": (median(walls) / p50)
                if walls and _number(p50) and p50 > 0
                else None,
            }
        )
    hold_rows: list[dict[str, Any]] = []
    for project, holds in holds_by_project.items():
        for hold in holds:
            if (
                not isinstance(hold, Mapping)
                or (stamp := parse_utc(hold.get("held_at"))) is None
            ):
                malformed += 1
                continue
            if boundary and stamp < boundary:
                continue
            later = [
                r
                for r in rows_by_project.get(project, [])
                if isinstance(r, Mapping)
                and r.get("node") == hold.get("node")
                and r.get("plan") == hold.get("plan")
                and (run_stamp := parse_utc(r.get("dispatched_at"))) is not None
                and run_stamp > stamp
            ]
            first = (
                min(later, key=lambda r: parse_utc(r["dispatched_at"]))
                if later
                else None
            )
            hold_rows.append(
                {
                    **hold,
                    "later_run_id": first.get("run_id") if first else None,
                    "later_backend": first.get("backend") if first else None,
                    "later_success": _success(first) if first else None,
                }
            )

    def latency_report(values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "p50_ms": median(values) if values else None,
            "p90_ms": _percentile(values, 0.9),
        }

    subscription_backends = sorted(
        {
            backend
            for (_project, backend, _role, _spec, _risk, _attribution) in groups
            if ledger.is_subscription_backend(backend, project=_project)
        }
    )
    return {
        "rules": {
            "success": SUCCESS_RULE,
            "confidence": "[0,.5), [.5,.7), [.7,.85), [.85,1]",
            "burn": "[0,1), [1,2), [2,4), [4,infinity)",
            "burn_offer": "lowest offered codex burn_multiple; unknown when none was recorded",
            "actions": "selection actions on promoted rows; run-free holds appear separately",
            "attribution": "routed outcomes, calibration and chosen metered spend require route mode picker and a matching selected backend; rows without route mode use the matching-backend rule in approximate_outcomes; shadow, explicit and overridden-hold rows are excluded",
            "window": "since is inclusive on run completed_at and hold held_at",
            "latency": "p50 is median; p90 is nearest-rank percentile",
            "repair_or_resume": "attempt kind repair, resume, or redispatch, or a repair/resume remedy",
            "profile": "14-day run-time profile; backend, role and spec; largest effort cohort",
            "missing": "unknown outcomes and absent numeric values are excluded from rate denominators",
        },
        "since": since,
        "projects": sorted(rows_by_project),
        "mechanics": {
            "actions": {
                name: actions[name] for name in ("route", "hold", "fallback", "refuse")
            },
            # Rows carrying a picker answer, counted by the route mode their
            # promotion recorded. An overridden hold is its own key rather than
            # sharing the shadow picks, so a reader can count how often an
            # operator overrules a hold.
            "route_modes": {
                name: route_modes.get(name, 0)
                for name in (
                    OVERRIDDEN_HOLD_ROUTE_MODE,
                    "picker",
                    "shadow",
                    "explicit",
                    "unrecorded",
                )
            },
            "fallback_reasons": dict(sorted(fallbacks.items())),
            "latency_ms": {
                name: latency_report(latency[name])
                for name in ("overall", *sorted(rows_by_project))
            },
        },
        "routed_outcomes": [
            group for group in group_rows if group["attribution"] == "picker"
        ],
        "approximate_outcomes": [
            group for group in group_rows if group["attribution"] == "approximate"
        ],
        "calibration": {
            name: {
                "count": len(calibration[name]),
                "known_outcomes": sum(v is not None for v in calibration[name]),
                "success_rate": (
                    sum(v is True for v in calibration[name])
                    / sum(v is not None for v in calibration[name])
                )
                if any(v is not None for v in calibration[name])
                else None,
            }
            for name in BUCKETS
        },
        "metered_spend": {
            "codex_runs": codex_runs,
            "billing": {
                "subscription_backends": subscription_backends,
                "rule": (
                    "a lane whose catalogue budget group is subscription-billed "
                    "is reported as subscription, not as metered spend; codex_runs "
                    "carries its burn figures, which are ratios, not dollars"
                ),
            },
            "offered_codex_by_burn": {
                name: {
                    "count": len(burn[name]),
                    "chosen_codex": sum(burn[name]),
                    "share": sum(burn[name]) / len(burn[name]) if burn[name] else None,
                }
                for name in BURN_LEVELS
            },
        },
        "holds": {"count": len(hold_rows), "records": hold_rows},
        "malformed_rows": malformed,
    }


def read_outcomes(
    *, project: str | None = None, since: str | None = None
) -> dict[str, Any]:
    """Read every mounted project's ledger and its run-free picker holds."""
    if since and parse_utc(since) is None:
        raise ValueError(f"since must be an ISO timestamp: {since!r}")
    mounts = flight.mounted_project_docs()
    if project is not None:
        if project not in mounts:
            raise ValueError(f"project {project!r} is not mounted")
        mounts = {project: mounts[project]}
    rows: dict[str, list[dict[str, Any]]] = {}
    holds: dict[str, list[dict[str, Any]]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    malformed_holds = 0
    for name, docs in mounts.items():
        rows[name] = ledger.runs(name, root=docs.parent, since=since)
        holds[name], count = _read_holds(docs, name)
        malformed_holds += count
        if rows[name]:
            profiles[name] = run_time_profile(name)
    result = summarize(rows, holds, profiles=profiles, since=since)
    result["malformed_rows"] += malformed_holds
    return result
