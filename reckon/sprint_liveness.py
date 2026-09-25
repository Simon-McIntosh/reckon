"""Derive each sprint's liveness from the project's live crew pointers.

A sprint is live while a crew works it. The signal is a live pointer serving a
plan whose ``plan-sprint`` meta is that sprint, classified working or dispatched
by the same vocabulary the fleet pane uses, so a stored scheduling marker never
has to agree with what is actually running.

Nothing here writes. Liveness is computed at read time and is never written to a
sprint resource: the pointers are the authority, and the sprint's stored status
is not consulted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._plan_html import parse_meta
from reckon.crew.node import DEFAULT_WATCH_STALL_WINDOW, parse_duration
from reckon.crew.recovery import (
    _run_stream_mtime,
    _utc_seconds,
    _watch_snapshot,
    classify_pointer,
)
from reckon.crew.runs import list_live

# The fleet reducer's words for a pointer that is carrying its sprint right now:
# "dispatched" is a run still in its starting phase and "working" one that has
# advanced past it. Every other state — complete, blocked, stalled, waiting,
# abandoned — names a crew that is not at work on the sprint.
LIVE_STATES = frozenset({"working", "dispatched"})

# A pointer that is on the sprint but not working it and not done with it either:
# a blocked run waiting on a person, or a completion nobody has promoted yet.
# These are held, the middle reading between live and finished.
HELD_CLASSIFICATIONS = frozenset(
    {"blocked", "completed_unpromoted", "promotable", "scoring"}
)


def _record_plan(record: Mapping[str, Any]) -> str:
    """The plan a pointer serves, from its node block or the record's own key."""
    node = record.get("node")
    if isinstance(node, Mapping) and node.get("plan"):
        return str(node["plan"])
    return str(record.get("plan") or "")


def _plan_sprint(docs_dir: str | Path, plan: str) -> str | None:
    """The sprint a plan declares, or None when the plan has no readable sprint.

    A plan with no ``plan-sprint`` meta, or a slug with no file behind it, is
    not assigned to a sprint: a pointer on it tells a reader nothing about sprint
    liveness and is skipped rather than folded into a null bucket.
    """
    if not plan:
        return None
    path = Path(docs_dir) / "plans" / f"{plan}.html"
    if not path.is_file():
        return None
    sprint = parse_meta(path).get("sprint")
    return str(sprint) if sprint else None


def _iso_utc(seconds: float) -> str:
    """Render an epoch reading the way every other stored instant is rendered."""
    return (
        datetime.fromtimestamp(seconds, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _blank_row() -> dict[str, Any]:
    return {
        "live": False,
        "live_runs": [],
        "live_sessions": [],
        "last_activity_at": None,
        "held_runs": 0,
    }


def sprint_liveness(
    project: str,
    docs_dir: str | Path,
    live_records: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Liveness per sprint, derived from one project's live crew pointers.

    Returns one row per sprint a pointer serves, keyed by sprint id. A row
    carries ``live`` (true when at least one pointer is working, starting or
    dispatched by the fleet classification), ``live_runs`` and ``live_sessions``
    (the run and dispatching-session ids behind that verdict),
    ``last_activity_at`` (the newest stream write among the sprint's runs, ISO
    UTC) and ``held_runs`` (the blocked or unpromoted pointers on the sprint).

    ``live_records`` is the pointer set to read; when omitted the project's live
    pointers are read through :func:`list_live`. A sprint with no pointer on it
    cannot be named from pointers alone and does not appear.
    """
    if live_records is None:
        live_records = list_live(project=project)
    moment = _utc_seconds()
    stall_seconds = parse_duration(DEFAULT_WATCH_STALL_WINDOW)

    rows: dict[str, dict[str, Any]] = {}
    for record in live_records:
        sprint = _plan_sprint(docs_dir, _record_plan(record))
        if sprint is None:
            continue
        row = rows.setdefault(sprint, _blank_row())

        newest = _run_stream_mtime(record)
        if newest is not None:
            stamp = _iso_utc(newest)
            if row["last_activity_at"] is None or stamp > row["last_activity_at"]:
                row["last_activity_at"] = stamp

        state = str(
            _watch_snapshot(record, moment=moment, stall_seconds=stall_seconds).get(
                "state"
            )
            or ""
        )
        if state in LIVE_STATES:
            row["live"] = True
            run_id = str(record.get("run_id") or "")
            if run_id and run_id not in row["live_runs"]:
                row["live_runs"].append(run_id)
            session = str(record.get("session") or "")
            if session and session not in row["live_sessions"]:
                row["live_sessions"].append(session)
            continue

        classification = str(
            classify_pointer(record, now_seconds=moment).get("classification") or ""
        )
        if classification in HELD_CLASSIFICATIONS:
            row["held_runs"] += 1

    for row in rows.values():
        row["live_runs"].sort()
        row["live_sessions"].sort()
    return rows
