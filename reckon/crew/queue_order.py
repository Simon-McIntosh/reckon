"""Order queued dispatches by roadmap priority, ageing, and lane occupancy."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
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


def _plan_priority_state(docs_dir: Path, slug: str, project: str) -> str | None:
    """The named plan's structured ranking inputs, without editorial stamps."""
    from reckon import _plan_html
    from reckon.crew.plan_review import _plan_inventory_item
    from reckon.roadmap import _decision_rows

    item = _plan_inventory_item(project, docs_dir, slug)
    if item is None:
        return None
    priority = {
        field: item.get(field)
        for field in (
            "type",
            "status",
            "archived",
            "sprint",
            "roi",
            "effort",
            "effort_hours",
            "wall_clock_hours",
            "effort_calibrated",
            "impl",
            "depends_on",
            "after",
            "blocks",
            "blockers",
            "section_declarations",
            "section_depends_on",
            "decision_sections",
        )
    }
    priority["followups"] = sorted(
        (str(row.get("id") or ""), str(row.get("status") or "open"))
        for row in item.get("followups") or []
    )
    priority["decisions"] = sorted(
        (row["id"], row["status"], sorted(row.get("sections") or []))
        for row in _decision_rows(item, item.get("decision_sections"))
    )
    # Gate text can declare section waits, so the ref reader's text inputs stay.
    priority["gates"] = [
        {
            field: gate.get(field)
            for field in (
                "id",
                "section",
                "gated_sections",
                "status",
                "verdict",
                "passed",
                "transition",
                "gating_plan",
                "decision",
                "measure",
                "required_evidence",
                "evidence",
            )
        }
        for gate in item.get("gates") or []
    ]
    state, text = _plan_html.read_state_and_text_file(
        docs_dir / "plans" / f"{slug}.html"
    )
    priority["sections"] = [
        {field: record.get(field) for field in ("id", "status", "effort_hours")}
        for record in state.get("sections") or []
    ]
    priority["section_ids"] = [
        identity for identity, _heading in _plan_html.authored_section_headings(text)
    ]
    return json.dumps(priority, sort_keys=True, separators=(",", ":"))


def _roadmap_input_signature(
    docs_dir: Path, slugs: set[str], project: str
) -> tuple[Any, ...]:
    """Queued plans' priority state and the sprint inputs used beside them.

    File identities avoid reparsing unchanged plans, but only their structured
    priority state enters the roadmap key. An unrelated plan or an editorial
    change therefore leaves the queued work's priorities reusable.
    """
    from reckon import file_memo
    from reckon._store import state_path

    plans = tuple(
        (
            slug,
            file_memo.memoized_key(
                "queue_plan_priority",
                (project, str(docs_dir), slug, identity),
                lambda slug=slug: _plan_priority_state(docs_dir, slug, project),
            )
            if identity is not None
            else None,
        )
        for slug, identity in _named_plan_signature(docs_dir, slugs)
    )
    signature: list[tuple[str, tuple[int, int, int, int, int] | None]] = []
    paths = {
        state_path(project, "index"),
        state_path(project, "index", root=docs_dir.parent),
    }
    directory = docs_dir / "sprints"
    try:
        identity = file_memo.file_signature(directory)
    except OSError:
        identity = None
    files = file_memo.memoized_key(
        "queue_priority_directory",
        (str(directory), identity),
        lambda: tuple(directory.glob("*.html")),
    )
    paths.update(files)
    for path in sorted(paths):
        try:
            identity = file_memo.file_signature(path)
        except OSError:
            identity = None
        signature.append((str(path), identity))
    return plans, tuple(signature)


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
                with roadmap_build_scope(
                    resolve_coordinators=False,
                    classify_runs=False,
                    audit_landings=False,
                ):
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


def _starved(queued_at: datetime | None, now: datetime) -> bool:
    return (
        queued_at is not None
        and (now - queued_at).total_seconds() >= STARVATION_AGE_SECONDS
    )


def _precedence(
    pointer: Mapping[str, Any],
    *,
    queued_at: datetime | None,
    now: datetime,
    review: bool,
) -> tuple[tuple[int, int], str]:
    """The urgency classes shared by admission and promotion."""
    if review:
        return (0, 0), "delivered review"
    if _starved(queued_at, now):
        return (1, 0), "ageing"
    if (priority := _override(pointer)) is not None:
        return (2, -priority), f"priority override {priority}"
    return (3, 0), "session fair share; oldest request"


def fair_queue_order_details(
    requests: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    joined_at: Callable[[Mapping[str, Any]], datetime | None],
    is_review: Callable[[Mapping[str, Any]], bool],
    served: Mapping[str, int],
    consumes_turn: Callable[[Mapping[str, Any]], bool],
) -> list[tuple[Mapping[str, Any], str]]:
    """Serve urgency classes fairly, charging only requests that take a turn.

    Historical service and in-flight reservations are supplied by the queue's
    owner. Each urgency class starts from that snapshot, so taking an urgent
    request does not consume a lower class's session turn.
    """
    classes = defaultdict(list)
    for request in requests:
        stamp = joined_at(request)
        rank, reason = _precedence(
            request, queued_at=stamp, now=now, review=is_review(request)
        )
        classes[rank].append((request, stamp, reason))

    ordered = []
    for rank in sorted(classes):
        remaining = classes[rank]
        turns = Counter(served)
        while remaining:
            row = min(
                remaining,
                key=lambda row: (
                    turns[str(row[0].get("session") or "")],
                    row[1] or datetime.max.replace(tzinfo=UTC),
                    str(row[0].get("run_id") or ""),
                ),
            )
            request, _stamp, reason = row
            ordered.append((request, reason))
            remaining.remove(row)
            if consumes_turn(request):
                turns[str(request.get("session") or "")] += 1
    return ordered


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

    def rank(row: tuple[datetime, int, Mapping[str, Any]]) -> tuple[Any, ...]:
        pointer = row[2]
        urgency, _reason = _precedence(
            pointer, queued_at=row[0], now=moment, review=_is_review_run(pointer)
        )
        roadmap = (
            (id(pointer) not in keys, keys.get(id(pointer), ()))
            if urgency == (3, 0)
            else ()
        )
        return urgency, roadmap, row[0], row[1]

    return min(rows, key=rank)


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
            not _starved(oldest[0], moment),
            occupancy[(str(launch[2].get("backend") or ""), group[1])],
            oldest[0],
            oldest[1],
        )

    def _project_rank(group: tuple[str, str]) -> tuple[Any, ...]:
        row = _next_to_launch(waiting[group], keys, moment)
        pointer = row[2]
        oldest = waiting[group][0][0]
        urgency, _reason = _precedence(
            pointer, queued_at=oldest, now=moment, review=_is_review_run(pointer)
        )
        override = _override(pointer)
        key = keys.get(id(pointer))
        return (
            urgency,
            not _starved(oldest, moment),
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
        urgency, reason = _precedence(
            pointer, queued_at=row[0], now=moment, review=_is_review_run(pointer)
        )
        if urgency == (3, 0) and (key := keys.get(id(pointer))) is not None:
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
        elif urgency == (3, 0):
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
