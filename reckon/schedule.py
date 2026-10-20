"""The dependency-derived schedule, computed once for every reader.

Positions every plan of a project on a wall-clock axis in hours relative to a
caller-supplied reference instant. A recorded plan ends when it landed. Every
other plan is anchored at the reference instant, or at the latest end of its
prerequisites when that is later, and its progress splits the bar around that
anchor: the done share sits behind it for ``p`` wall-clock hours and the
remaining share runs ahead of it for ``(1 - p)``. A plan's state is decided
here — recorded, in flight, held or predicted — from its status and the live
pointers that serve it, and each bar carries its plan's typed inventory key and
the lane the packing placed it in. Bars past the retention window are excluded;
remaining bars pack into best-fit lanes; the axis range and the chain's far end
are reported with the bars. The reference instant is a parameter and never the
wall clock, so any reader and the rendered surface compute the same numbers
from the same input.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from reckon._timestamps import parse_utc
from reckon.resources import nav_key

RECORDED_STATUSES = frozenset({"shipped", "done", "superseded", "historical"})

# A live pointer at work on its plan right now. The vocabulary is the fleet
# pane's and the sprint liveness derivation's: "dispatched" is a run still in
# its starting phase, "working" one that has advanced past it.
LIVE_RUN_PHASES = frozenset({"working", "starting", "dispatched"})

# A pointer sitting on the plan but not at work on it and not done with it: a
# worker waiting on a person, or a completion nobody has promoted yet.
HELD_RUN_PHASES = frozenset(
    {
        "blocked",
        "unpromoted",
        "completed_unpromoted",
        "complete",
        "promotable",
        "scoring",
    }
)

HELD_STATUS = "blocked"

STATE_RECORDED = "recorded"
STATE_IN_FLIGHT = "in-flight"
STATE_HELD = "held"
STATE_PREDICTED = "predicted"

_MS_PER_HOUR = 3_600_000.0


def _as_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _stamp_millis(stamp: Any) -> float | None:
    text = str(stamp)
    if text != text.strip() or text.endswith("z"):
        return None
    parsed = parse_utc(text)
    if parsed is None:
        return None
    return parsed.timestamp() * 1000.0


def _wall_hours(plan: dict[str, Any]) -> float:
    declared = _as_number(plan.get("wall_clock_hours"))
    if declared is not None and declared > 0:
        return declared
    effort = _as_number(plan.get("effort_hours"))
    return effort if effort is not None and effort > 0 else 0.0


def _progress(plan: dict[str, Any]) -> float:
    """The plan's effort-weighted progress, clamped to a share of its own bar."""

    value = _as_number(plan.get("impl"))
    if value is None:
        return 0.0
    return min(1.0, max(0.0, value))


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


def _run_phase(run: dict[str, Any]) -> str:
    return str(run.get("phase") or "").lower()


def _bar_state(plan: dict[str, Any], plan_runs: list[dict[str, Any]]) -> str:
    """Classify a bar from its plan's status and the pointers serving it.

    A terminal status is recorded however the pointers read. A pointer whose
    phase is live puts the bar in flight. Otherwise a blocked effective status
    — persisted or derived from a dependency — or a plan whose every pointer is
    held reads as held. Everything else is predicted. A pointer in any other
    phase — complete, stalled, waiting, abandoned — sets no state at all.
    """

    status = str(plan.get("status") or "pending").lower()
    if status in RECORDED_STATUSES:
        return STATE_RECORDED
    phases = [_run_phase(run) for run in plan_runs]
    if any(phase in LIVE_RUN_PHASES for phase in phases):
        return STATE_IN_FLIGHT
    effective = str(plan.get("effective_status") or status).lower()
    if effective == HELD_STATUS:
        return STATE_HELD
    if phases and all(phase in HELD_RUN_PHASES for phase in phases):
        return STATE_HELD
    return STATE_PREDICTED


def _typed_key(plan: dict[str, Any]) -> str:
    """The plan's inventory key, carrying its type so a shared slug cannot
    resolve to another document's row."""

    return nav_key("plan", str(plan["slug"]), archived=bool(plan.get("archived")))


def _pack_lanes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-fit pack bars into lanes, stamping each bar with its lane index.

    A bar joins the free lane that ended latest; a bar that fits none opens a
    new lane. The lane index is written back onto the bar so a reader can
    render the packing the server chose without re-deriving it.
    """

    lanes: list[dict[str, Any]] = []
    for item in items:
        free = [
            (index, lane)
            for index, lane in enumerate(lanes)
            if lane["last_end"] <= item["start"] + 0.01
        ]
        if free:
            index, lane = max(free, key=lambda pair: pair[1]["last_end"])
        else:
            index = len(lanes)
            lane = {"last_end": float("-inf"), "items": []}
            lanes.append(lane)
        item["lane"] = index
        lane["items"].append(item)
        lane["last_end"] = item["end"]
    return lanes


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
    runs_by_slug: dict[str, list[dict[str, Any]]] = {}
    for run in matching_runs:
        plan_slug = str(run.get("plan") or "")
        if plan_slug:
            runs_by_slug.setdefault(plan_slug, []).append(run)

    bars: dict[str, dict[str, Any]] = {}

    def resolve(slug: str, seen: frozenset[str] = frozenset()) -> dict[str, Any] | None:
        if slug in bars:
            return bars[slug]
        plan = by_slug.get(slug)
        if plan is None or slug in seen:
            return None
        wall_hours = _wall_hours(plan)
        status = str(plan.get("status") or "pending").lower()

        if status in RECORDED_STATUSES:
            # A recorded bar ends where it landed, as it always has.
            landed = plan.get("edited") or plan.get("modified") or plan.get("last")
            end = _hours_from_now(landed, now_millis)
            end = 0.0 if end is None else end
            split = end
            start = end - wall_hours
        else:
            dependency_ends: list[float] = []
            for dep in (
                _dependency_slug(ref, project) for ref in plan.get("depends_on") or []
            ):
                if not dep or dep not in by_slug:
                    continue
                dependency_bar = resolve(dep, seen | {slug})
                if dependency_bar is not None:
                    dependency_ends.append(dependency_bar["end"])
            anchor = max([0.0, *dependency_ends])
            progress = _progress(plan)
            start = anchor - progress * wall_hours
            split = anchor
            end = anchor + (1.0 - progress) * wall_hours

        bar = {
            "slug": slug,
            "plan": plan,
            "state": _bar_state(plan, runs_by_slug.get(slug, [])),
            "nav_key": _typed_key(plan),
            "wall_hours": wall_hours,
            "start": start,
            "split": split,
            "end": end,
        }
        bars[slug] = bar
        return bar

    for plan in project_plans:
        resolve(plan["slug"])

    items = [bars[plan["slug"]] for plan in project_plans if plan["slug"] in bars]
    items = [item for item in items if item["start"] > -retention_hours]
    items.sort(key=lambda item: (item["start"], item["end"], item["slug"]))
    lanes = _pack_lanes(items)

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
