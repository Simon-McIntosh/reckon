"""Order queued dispatches by roadmap priority, ageing, and lane occupancy."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew.node import _TERMINAL_RUN_PHASES
from reckon.roadmap_ranking import leading_signal, section_ranking_key

STARVATION_AGE_SECONDS = 30 * 60
_LOGGER = logging.getLogger(__name__)
_PRIORITY_KEYS: dict[
    tuple[str, str], tuple[tuple[Any, ...], dict[str, tuple[Any, ...]]]
] = {}
_DIRECTORY_FILES: dict[Path, tuple[Any, tuple[Path, ...]]] = {}


def _named_plan_signature(docs_dir: Path, slugs: set[str]) -> tuple[Any, ...]:
    """The named plans' file identities, including absent files."""
    from reckon import file_memo

    signature: list[tuple[str, tuple[int, int, int, int, int] | None]] = []
    for slug in sorted(slugs):
        try:
            stat = (docs_dir / "plans" / f"{slug}.html").stat()
            identity = file_memo.signature_from_stat(stat)
        except OSError:
            identity = None
        signature.append((slug, identity))
    return tuple(signature)


def _roadmap_input_signature(
    docs_dir: Path, slugs: set[str], project: str
) -> tuple[Any, ...]:
    """Plan and sprint identities, including additions and in-place edits.

    Directory stamps detect arrivals and removals; file stamps detect edits
    that leave the containing directory unchanged. Directory contents are
    listed only when that directory's own identity moves.
    """
    from reckon import file_memo
    from reckon._store import state_path

    signature: list[tuple[str, tuple[int, int, int, int, int] | None]] = []
    paths = {
        state_path(project, "index"),
        state_path(project, "index", root=docs_dir.parent),
    }
    for directory in (docs_dir / "plans", docs_dir / "sprints"):
        try:
            identity = file_memo.file_signature(directory)
        except OSError:
            identity = None
        signature.append((str(directory), identity))
        cached = _DIRECTORY_FILES.get(directory)
        if cached is None or cached[0] != identity:
            cached = (identity, tuple(directory.glob("*.html")))
            _DIRECTORY_FILES[directory] = cached
        paths.update(cached[1])
    paths.difference_update(docs_dir / "plans" / f"{slug}.html" for slug in slugs)
    for path in sorted(paths):
        try:
            identity = file_memo.file_signature(path)
        except OSError:
            identity = None
        signature.append((str(path), identity))
    return _named_plan_signature(docs_dir, slugs), tuple(signature)


def _roadmap_priority_keys(
    queued_pointers: Sequence[Mapping[str, Any]],
) -> dict[int, tuple[Any, ...]]:
    """Resolve section signals from queued plans, without project discovery."""
    from reckon.crew.plan_review import _plan_inventory_item
    from reckon.mcp_discovery import _docs_dir_for_project
    from reckon.roadmap import build_roadmap, roadmap_build_scope

    keys: dict[int, tuple[Any, ...]] = {}
    projects = {
        str(row.get("project") or "")
        for row in queued_pointers
        if row.get("project") and row.get("plan") and row.get("section")
    }
    for project in projects:
        try:
            docs_dir = _docs_dir_for_project(project, None)
            cache_key = (project, str(docs_dir.resolve())) if docs_dir else None
            slugs = {
                str(pointer.get("plan") or "")
                for pointer in queued_pointers
                if str(pointer.get("project") or "") == project and pointer.get("plan")
            }
            signature = (
                _roadmap_input_signature(docs_dir, slugs, project) if docs_dir else ()
            )
            cached = _PRIORITY_KEYS.get(cache_key) if cache_key else None
            if cached is not None and cached[0] == signature:
                project_keys = cached[1]
            else:
                inventory = (
                    [
                        item
                        for slug in sorted(slugs)
                        if (item := _plan_inventory_item(project, docs_dir, slug))
                        is not None
                    ]
                    if docs_dir
                    else []
                )
                with roadmap_build_scope(resolve_coordinators=False):
                    report = build_roadmap(
                        project,
                        inventory,
                        [],
                        docs_dir=docs_dir,
                        include_attempts=False,
                    )
                sprint_order = {
                    str(row["ref"]): int(row["order"]) - 1
                    for row in report.get("sprints") or ()
                    if row.get("ref") and row.get("order")
                }
                critical = set(report.get("critical_path", {}).get("sections") or ())
                project_keys = {
                    str(row["ref"]): section_ranking_key(
                        row, critical_sections=critical, sprint_order=sprint_order
                    )
                    for row in report.get("immediate_roadmap") or ()
                    if row.get("ref")
                }
                if cache_key is not None:
                    _PRIORITY_KEYS[cache_key] = (signature, project_keys)
        except Exception as exc:  # noqa: BLE001 - an unreadable roadmap yields no invented key
            _LOGGER.debug("roadmap priority unavailable for %s: %s", project, exc)
            continue
        for pointer in queued_pointers:
            if str(pointer.get("project") or "") != project:
                continue
            ref = f"{pointer.get('plan')}#{pointer.get('section')}"
            key = project_keys.get(ref)
            if key is not None:
                keys[id(pointer)] = key
    return keys


def _override(pointer: Mapping[str, Any]) -> int | None:
    value = pointer.get("priority_override")
    return (
        int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    )


def _next_to_launch(
    rows: deque[tuple[datetime, int, Mapping[str, Any]]],
    keys: Mapping[int, tuple[Any, ...]],
    moment: datetime,
) -> tuple[datetime, int, Mapping[str, Any]]:
    """Take a delivered review first, then aged or explicitly ranked work.

    A review of a delivered run is what lets that run land, so it launches ahead
    of the session's queued implementation however long that has waited. The
    oldest row still anchors the session's place in the cross-session order, so
    the review does not make a long-waiting implementation read as fresh.

    Reviewedness is read by the module that owns the definition, imported here
    because the review-delivery module reads the run commands at import time.
    """
    from reckon.crew.recovery_review_delivery import _is_review_run

    reviews = [row for row in rows if _is_review_run(row[2])]
    if reviews:
        return reviews[0]
    aged = [
        row
        for row in rows
        if (moment - row[0]).total_seconds() >= STARVATION_AGE_SECONDS
    ]
    if aged:
        return aged[0]
    overridden = [row for row in rows if _override(row[2]) is not None]
    if overridden:
        return min(overridden, key=lambda row: (-_override(row[2]), row[0], row[1]))
    ranked = [row for row in rows if id(row[2]) in keys]
    if ranked:
        return min(ranked, key=lambda row: (keys[id(row[2])], row[0], row[1]))
    return rows[0]


def admission_order_details(
    queued_pointers: Sequence[Mapping[str, Any]],
    live_pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[tuple[Mapping[str, Any], str]]:
    """Rank requests and name the signal that placed each one."""
    from reckon.crew.recovery_review_delivery import _is_review_run

    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    moment = now.astimezone(UTC)
    keys = _roadmap_priority_keys(queued_pointers)

    joined = []
    for index, pointer in enumerate(queued_pointers):
        queued_at = parse_utc(pointer.get("queued_at"))
        if queued_at is None:
            raise ValueError("queued pointer has no readable queued_at")
        joined.append((queued_at, index, pointer))
    joined.sort(key=lambda row: (row[0], row[1]))

    waiting = defaultdict(deque)
    for row in joined:
        pointer = row[2]
        waiting[(str(pointer.get("project") or ""), str(pointer["session"]))].append(
            row
        )

    occupancy = Counter(
        (str(pointer.get("backend") or ""), str(pointer.get("session") or ""))
        for pointer in live_pointers
        if str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        and str(pointer.get("phase") or "") != "queued"
    )

    def _rank(group: tuple[str, str]) -> tuple[bool, int, datetime, int]:
        oldest = waiting[group][0]
        launch = _next_to_launch(waiting[group], keys, moment)
        return (
            moment.timestamp() - oldest[0].timestamp() <= STARVATION_AGE_SECONDS,
            occupancy[(str(launch[2].get("backend") or ""), group[1])],
            oldest[0],
            oldest[1],
        )

    def _project_rank(group: tuple[str, str]) -> tuple[Any, ...]:
        row = _next_to_launch(waiting[group], keys, moment)
        pointer = row[2]
        age = (moment - waiting[group][0][0]).total_seconds()
        override = _override(pointer)
        key = keys.get(id(pointer))
        return (
            not _is_review_run(pointer),
            age < STARVATION_AGE_SECONDS,
            override is None,
            -(override or 0),
            key is None,
            key or (),
            occupancy[(str(pointer.get("backend") or ""), group[1])],
            row[0],
            row[1],
        )

    ordered: list[tuple[Mapping[str, Any], str]] = []
    while waiting:
        first = min(waiting, key=_rank)
        project = first[0]
        candidates = [group for group in waiting if group[0] == project]
        group = min(candidates, key=_project_rank) if project else first
        row = _next_to_launch(waiting[group], keys, moment)
        waiting[group].remove(row)
        pointer = row[2]
        if _is_review_run(pointer):
            reason = "delivered review"
        elif (moment - row[0]).total_seconds() >= STARVATION_AGE_SECONDS:
            reason = "ageing"
        elif (override := _override(pointer)) is not None:
            reason = f"priority override {override}"
        elif (key := keys.get(id(pointer))) is not None:
            rivals = [
                keys[id(other[2])]
                for rows in waiting.values()
                for other in rows
                if other[2].get("project") == project and id(other[2]) in keys
            ]
            rival = min(rivals) if rivals else None
            reason = (
                leading_signal(key, rival)
                if rival is not None and key < rival
                else "roadmap priority"
            )
        else:
            reason = "queued at (roadmap priority unavailable)"
        ordered.append((pointer, reason))
        occupancy[(str(pointer.get("backend") or ""), group[1])] += 1
        if not waiting[group]:
            del waiting[group]
    return ordered


def admission_order(
    queued_pointers: Sequence[Mapping[str, Any]],
    live_pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[Mapping[str, Any]]:
    """Rank queued pointers, charging each admission to its session's lane use.

    The caller supplies a snapshot from ``runs.list_live``. Queued and terminal
    pointers consume no lane slot; each selected queued pointer consumes one
    in the simulated order. A session keeps its join order, except that its
    reviews launch ahead of the implementations queued before them.
    """
    return [
        pointer
        for pointer, _reason in admission_order_details(
            queued_pointers, live_pointers, now=now
        )
    ]
