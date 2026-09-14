"""The dependency-derived schedule, computed once for every reader.

Positions every plan of a project on a wall-clock axis in hours relative to a
caller-supplied reference instant: a recorded plan ends when it landed and
starts wall-clock hours before that; an active plan starts at the earliest
live run dispatched for it (else the reference instant) and runs for its
wall-clock hours; a pending or draft plan starts at the later of its
prerequisite ends and the reference instant. Bars past the retention window
are excluded; remaining bars pack into best-fit lanes; the axis range and the
chain's far end are reported with the bars. The reference instant is a
parameter and never the wall clock, so any reader and the rendered surface
compute the same numbers from the same input.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

RECORDED_STATUSES = frozenset({"shipped", "done", "superseded", "historical"})
ACTIVE_STATUSES = frozenset({"active", "in-progress"})

_MS_PER_HOUR = 3_600_000.0


def _as_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _stamp_millis(stamp: Any) -> float | None:
    text = str(stamp)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() * 1000.0


def _wall_hours(plan: dict[str, Any]) -> float:
    declared = _as_number(plan.get("wall_clock_hours"))
    if declared is not None and declared > 0:
        return declared
    effort = _as_number(plan.get("effort_hours"))
    return effort if effort is not None and effort > 0 else 0.0


def _dependency_slug(ref: Any, project: str | None) -> str | None:
    value = ref.get("slug") if isinstance(ref, dict) else ref
    if not value:
        return None
    without_section = str(value).split("#", 1)[0]
    separator = without_section.find(":")
    if separator < 0:
        return without_section
    if without_section[:separator] == (project or ""):
        return without_section[separator + 1 :]
    return None


def _hours_from_now(stamp: Any, now_millis: float) -> float | None:
    if not stamp:
        return None
    stamp_millis = _stamp_millis(stamp)
    if stamp_millis is None:
        return None
    return (stamp_millis - now_millis) / _MS_PER_HOUR


def _run_stamp(run: dict[str, Any]) -> Any:
    return (
        run.get("dispatched_at")
        or run.get("attempt_started_at")
        or run.get("started_at")
        or run.get("created_at")
    )


def _run_start_hours(run: dict[str, Any], now_millis: float) -> float | None:
    stamp = _run_stamp(run)
    if stamp:
        stamped_hours = _hours_from_now(stamp, now_millis)
        if stamped_hours is not None:
            return stamped_hours
    elapsed = _as_number(run.get("elapsed_seconds"))
    if elapsed is not None:
        return -max(0.0, elapsed) / 3600.0
    return None


def derive_schedule(
    plans: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    project: str | None,
    reference: datetime,
    *,
    retention_hours: float = 60.0,
) -> dict[str, Any]:
    """Return bars, best-fit lanes, axis bounds and ticks for one project,
    all in hours relative to ``reference`` (interpreted as UTC when naive)."""

    reference = reference.replace(tzinfo=UTC) if reference.tzinfo is None else reference
    now_millis = reference.timestamp() * 1000.0

    project_plans = [
        plan
        for plan in (plans or [])
        if (plan.get("type") or "plan") == "plan"
        and (not plan.get("project") or not project or plan.get("project") == project)
    ]
    by_slug = {plan["slug"]: plan for plan in project_plans}
    matching_runs = [
        run
        for run in (runs or [])
        if not project or not run.get("project") or run.get("project") == project
    ]

    starts: dict[str, float] = {}
    ends: dict[str, float] = {}

    def resolve_end(slug: str, seen: frozenset[str] = frozenset()) -> float:
        if slug in ends:
            return ends[slug]
        plan = by_slug.get(slug)
        if plan is None or slug in seen:
            return 0.0
        wall_hours = _wall_hours(plan)
        status = str(plan.get("status") or "pending").lower()
        if status in RECORDED_STATUSES:
            landed = plan.get("edited") or plan.get("modified") or plan.get("last")
            end = _hours_from_now(landed, now_millis)
            end = 0.0 if end is None else end
            start = end - wall_hours
        elif status in ACTIVE_STATUSES:
            dispatch_hours = [
                hours
                for run in matching_runs
                if run.get("plan") == slug
                for hours in [_run_start_hours(run, now_millis)]
                if hours is not None
            ]
            start = min(dispatch_hours) if dispatch_hours else 0.0
            end = start + wall_hours
        else:
            dependency_ends = [
                resolve_end(dep, seen | {slug})
                for dep in (
                    _dependency_slug(ref, project)
                    for ref in plan.get("depends_on") or []
                )
                if dep and dep in by_slug
            ]
            start = max([0.0, *dependency_ends])
            end = start + wall_hours
        starts[slug] = start
        ends[slug] = end
        return end

    for plan in project_plans:
        resolve_end(plan["slug"])

    items = [
        {
            "slug": plan["slug"],
            "plan": plan,
            "start": starts.get(plan["slug"], 0.0),
            "end": ends.get(plan["slug"], _wall_hours(plan)),
            "wall_hours": _wall_hours(plan),
        }
        for plan in project_plans
    ]
    items = [item for item in items if item["start"] > -retention_hours]
    items.sort(key=lambda item: (item["start"], item["end"], item["slug"]))

    lanes: list[dict[str, Any]] = []
    for item in items:
        free = [lane for lane in lanes if lane["last_end"] <= item["start"] + 0.01]
        lane = max(free, key=lambda candidate: candidate["last_end"]) if free else None
        if lane is None:
            lane = {"last_end": float("-inf"), "items": []}
            lanes.append(lane)
        lane["items"].append(item)
        lane["last_end"] = item["end"]

    earliest_start = min(item["start"] for item in items) if items else 0.0
    latest_end = max(item["end"] for item in items) if items else 0.0
    low = max(-48.0, min(-24.0, earliest_start))
    high = max(24.0, latest_end)
    tick_step = 24.0 if high - low > 96 else 12.0
    ticks: list[dict[str, Any]] = []
    hour = math.ceil(low / tick_step) * tick_step
    while hour <= high + 1e-9:
        label = "now" if hour == 0 else f"+{hour:g}h" if hour > 0 else f"{hour:g}h"
        ticks.append({"hour": hour, "label": label})
        hour += tick_step

    return {
        "items": items,
        "lanes": lanes,
        "low": low,
        "high": high,
        "earliest_start": earliest_start,
        "latest_end": latest_end,
        "ticks": ticks,
    }
