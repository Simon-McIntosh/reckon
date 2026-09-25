"""Derive the work one coordinator session still owes.

The recovery classifier owns each live run's state and remedy.  This module
only projects those rows into coordinator duties, adds the two duties whose
evidence lives outside live pointers, and reports the closure figure from the
drain view.  No obligation state is persisted here.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _store, flight, ledger
from reckon.crew import recovery, runs
from reckon.crew.node import INTERRUPTED_RUN_PHASE, parse_duration
from reckon.crew.routing import _registered_worktrees

CLASSIFICATION_DUTY_KINDS = {
    "scoring": "review-missing",
    "promotable": "review-ready",
    "blocked": "blocked",
    INTERRUPTED_RUN_PHASE: "turn-ended-early",
}
RECOVERY_CLASSIFICATION_DUTY_KINDS = {"needs-help": "needs-help"}


def _utc_now() -> datetime:
    """Return the observation instant through a patchable clock boundary."""
    return datetime.now(tz=UTC)


def _seconds_since(value: Any, *, now: datetime) -> int:
    """Return the non-negative age of one timestamp, or zero if unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0, int((now - stamp.astimezone(UTC)).total_seconds()))


def _row_age(row: Mapping[str, Any], *, now: datetime) -> int:
    """Read a classifier row's best available age measurement."""
    stated_age = row.get("age_seconds")
    if isinstance(stated_age, (int, float)) and not isinstance(stated_age, bool):
        return max(0, int(stated_age))
    terminal_age = row.get("terminal_age_seconds")
    if isinstance(terminal_age, (int, float)) and not isinstance(terminal_age, bool):
        return max(0, int(terminal_age))
    exit_record = row.get("exit_record")
    if isinstance(exit_record, Mapping) and exit_record.get("exited_at"):
        return _seconds_since(exit_record["exited_at"], now=now)
    log_age = row.get("log_age_seconds")
    if isinstance(log_age, (int, float)) and not isinstance(log_age, bool):
        return max(0, int(log_age))
    return 0


def _current_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether a live review is working on this run's current revision."""
    review_run_id = recovery._review_in_flight(pointer)
    if not review_run_id:
        return False
    review_pointer = runs.read_pointer(review_run_id)
    if not review_pointer:
        return False
    expected = recovery._review_dispatch_fields(pointer)["write_paths"]
    head_keyed = expected[1:]
    if not head_keyed:
        return True
    node = review_pointer.get("node") or {}
    granted = {str(path) for path in node.get("write_paths") or ()}
    return any(str(path) in granted for path in head_keyed)


def _live_review_runs(project: str, session: str) -> set[str]:
    """Snapshot session runs whose current revision has a review in any session."""
    in_flight: set[str] = set()
    for pointer in runs.list_live(project=project):
        if str(pointer.get("session") or "") != session:
            continue
        run_id = str(pointer.get("run_id") or "")
        if run_id and _current_review_in_flight(pointer):
            in_flight.add(run_id)
    return in_flight


def _classified_rows(project: str) -> list[dict[str, Any]]:
    """Classify current project pointers without observing or launching work."""
    return [
        recovery.classify_pointer(pointer)
        for pointer in runs.list_live(project=project)
    ]


def _live_item(row: Mapping[str, Any], *, kind: str, now: datetime) -> dict[str, Any]:
    """Project one recovery row into the stable obligation shape."""
    return {
        "kind": kind,
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or ""),
        "plan": str(row.get("plan") or ""),
        "age_seconds": _row_age(row, now=now),
        "next_command": str(row.get("next_action") or ""),
    }


def _held_worktrees(
    project: str, session: str, *, now: datetime
) -> list[dict[str, Any]]:
    """Return promoted runs whose retained tree remains in Git's registry."""
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return []
    repository = docs_dir.parent.resolve()
    registered = {
        path
        for path in _registered_worktrees(repository)
        if path != repository and path.parent.name == session
    }
    command = " ".join(
        shlex.quote(part)
        for part in (
            "reckon",
            "crew",
            "gc",
            "--repo",
            str(repository),
            "--project",
            project,
            "--apply",
        )
    )
    records = ledger.runs(project, root=repository)
    matched: dict[Path, Mapping[str, Any]] = {}
    for record in records:
        retention = record.get("worktree_retention")
        if isinstance(retention, Mapping):
            value = str(retention.get("worktree") or "").strip()
            if value:
                retained = Path(value).expanduser().resolve()
                if retained in registered:
                    matched[retained] = record
        node = record.get("node")
        node_id = (
            str(node.get("id") or "") if isinstance(node, Mapping) else str(node or "")
        )
        for worktree in registered:
            if node_id and worktree.name == node_id:
                matched[worktree] = record

    items: list[dict[str, Any]] = []
    for record in matched.values():
        retention = record.get("worktree_retention")
        retained_at = (
            retention.get("retained_at") if isinstance(retention, Mapping) else None
        )
        items.append(
            {
                "kind": "worktree-held",
                "run_id": str(record.get("run_id") or ""),
                "node": str(record.get("node") or ""),
                "plan": str(record.get("plan") or ""),
                "age_seconds": _seconds_since(
                    retained_at or record.get("completed_at"),
                    now=now,
                ),
                "next_command": command,
            }
        )
    return items


def obligations(project: str, session: str) -> dict[str, Any]:
    """Return every current duty owed by one coordinator session.

    Live duty kinds are a projection of :func:`recovery.recover`; the
    promotable grace comes from the same resolved flight configuration used by
    dispatch.  The result is ordered oldest first so the first row is also the
    most urgent age signal.
    """
    now = _utc_now()
    resolved = flight.resolve(project)
    config = resolved.config
    grace = parse_duration(
        str((config.get("fences") or {}).get("unreconciled_run_grace") or "15m")
    )
    reviews_in_flight = _live_review_runs(project, session)
    items: list[dict[str, Any]] = []
    for row in _classified_rows(project):
        if str(row.get("session") or "") != session:
            continue
        classification = str(row.get("classification") or "")
        recovery_classification = str(row.get("recovery_classification") or "")
        kind = ""
        if classification == "scoring":
            if str(row.get("run_id") or "") in reviews_in_flight:
                continue
            kind = CLASSIFICATION_DUTY_KINDS[classification]
        elif classification == "promotable":
            age = _row_age(row, now=now)
            kind = (
                "promotable-stale"
                if age > grace
                else CLASSIFICATION_DUTY_KINDS[classification]
            )
        elif recovery_classification in RECOVERY_CLASSIFICATION_DUTY_KINDS:
            kind = RECOVERY_CLASSIFICATION_DUTY_KINDS[recovery_classification]
        else:
            kind = CLASSIFICATION_DUTY_KINDS.get(classification, "")
        if kind:
            items.append(_live_item(row, kind=kind, now=now))

    items.extend(_held_worktrees(project, session, now=now))
    items.sort(
        key=lambda item: (
            -int(item["age_seconds"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    closure = runs.drain(project, session=session)
    return {
        "project": project,
        "session": session,
        "obligations": items,
        "summary": {
            "count": len(items),
            "oldest_age_seconds": max(
                (int(item["age_seconds"]) for item in items), default=0
            ),
            "unreconciled_runs": int(closure["unreconciled_runs"]),
        },
    }
