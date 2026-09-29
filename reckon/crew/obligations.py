"""Derive the work one coordinator session still owes.

The recovery classifier owns each live run's state and remedy.  This module
only projects those rows into coordinator duties, adds the duties whose
evidence lives outside live pointers, and reports the closure figure from the
drain view.  No obligation state is persisted here.

One of those duties is derived rather than projected: a stored review whose
dimension sits below the floor flight configuration declares for it is a
finding that must be answered before the session can close cleanly over it.
The finding is never folded into the review's total — it is reported beside
it, for the run the review is about, until a disposition in the closed set is
recorded against the dimension on the stored record itself. The disposition
vocabulary, and the read-back that decides whether a finding still stands,
live with the review store (:mod:`reckon.crew.review`); this module only
reports what is still owed.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _store, flight, ledger
from reckon._timestamps import parse_utc
from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.node import INTERRUPTED_RUN_PHASE, parse_duration
from reckon.crew.routing import _registered_worktrees

CLASSIFICATION_DUTY_KINDS = {
    "scoring": "review-missing",
    "promotable": "review-ready",
    "blocked": "blocked",
    INTERRUPTED_RUN_PHASE: "turn-ended-early",
}
RECOVERY_CLASSIFICATION_DUTY_KINDS = {"needs-help": "needs-help"}

# A duty kind whose evidence is a stored review rather than a live run's state:
# the row is owed while the dimension carries no disposition, and it names the
# run the review is about rather than the reviewing run.
SUB_FLOOR_DUTY_KIND = "review-dimension-sub-floor"


def _utc_now() -> datetime:
    """Return the observation instant through a patchable clock boundary."""
    return datetime.now(tz=UTC)


def _seconds_since(value: Any, *, now: datetime) -> int:
    """Return the non-negative age of one timestamp, or zero if unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0
    stamp = parse_utc(text)
    if stamp is None:
        return 0
    return max(0, int((now - stamp).total_seconds()))


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


def _reflex_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether the reflex's own recorded review dispatch still holds a live run.

    The reflex records the review it launched on the run it acted for, and that
    record is its claim: while the review run it names holds a live pointer the
    reflex will not compose a second review, so a printed dispatch is one the
    coordinator can only have refused as a scope conflict. The claim is dated
    from the launch window the record carries rather than being re-judged
    against the run's manifest: a run that re-completed after that launch has
    not taken the claim away, because the live review the reflex owns is still
    the thing under way and the reflex is what re-fires once it ends.
    """
    recorded = pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return False
    review_run_id = str(recorded.get("run_id") or "")
    if not review_run_id:
        return False
    return runs.pointer_path(review_run_id).exists()


def _current_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether a live review is working on this run's current revision.

    The reflex's own claim is honoured whatever revision the run now carries.
    A hand-launched review carries no such record, so it is recognised by the
    head-keyed record path its reviewer was granted, which keeps a review of an
    older revision from reading as a review of the current one.
    """
    if _reflex_review_in_flight(pointer):
        return True
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


def _acknowledgements_in_force(
    project: str, *, now: datetime
) -> dict[str, dict[str, Any]]:
    """Return each live run's recorded deferral that has not yet expired.

    A deferral whose ``until`` has passed is not returned, so the run it named
    re-enters the list on the next read rather than lingering in the
    acknowledged block. A run whose deferral cannot be parsed as an instant is
    treated as undeferred: an unreadable deadline is no excuse to withhold an
    obligation.
    """
    in_force: dict[str, dict[str, Any]] = {}
    for pointer in runs.list_live(project=project):
        run_id = str(pointer.get("run_id") or "")
        record = runs.run_acknowledgement(pointer)
        if not run_id or not record:
            continue
        until = parse_utc(record.get("until"))
        if until is not None and until > now:
            in_force[run_id] = record
    return in_force


def _partition_acknowledged(
    items: list[dict[str, Any]],
    acknowledgements: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split duties into those still owed and those deliberately deferred."""
    owed: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for item in items:
        record = acknowledgements.get(str(item.get("run_id") or ""))
        if record is None:
            owed.append(item)
            continue
        deferred.append(
            {
                **item,
                "reason": str(record.get("reason") or ""),
                "until": str(record.get("until") or ""),
            }
        )
    return owed, deferred


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


def _live_worktrees(project: str) -> set[Path]:
    """Registered-tree paths a live run currently occupies.

    A retained path reused by a live run is not held by the promoted run whose
    ledger record still names it. The collector sees the path live-referenced
    and leaves it alone, so hinting a collection for it would name work that
    cannot be done and a run that no longer owns the tree.
    """
    trees: set[Path] = set()
    for pointer in runs.list_live(project=project):
        value = str(pointer.get("worktree") or "").strip()
        if value:
            trees.add(Path(value).expanduser().resolve())
    return trees


def _held_worktrees(
    project: str, session: str, *, now: datetime
) -> list[dict[str, Any]]:
    """Return promoted runs whose retained tree remains in Git's registry.

    A registered path a live run now occupies is skipped: the promoted run's
    ledger record still names it under the same node id, but the tree belongs
    to the live run and the collector will not take it, so naming the promoted
    run would send a coordinator at the wrong run for a tree it cannot free.
    """
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
    occupied = _live_worktrees(project)
    matched: dict[Path, Mapping[str, Any]] = {}
    for record in records:
        retention = record.get("worktree_retention")
        if isinstance(retention, Mapping):
            value = str(retention.get("worktree") or "").strip()
            if value:
                retained = Path(value).expanduser().resolve()
                if retained in registered and retained not in occupied:
                    matched[retained] = record
        node = record.get("node")
        node_id = (
            str(node.get("id") or "") if isinstance(node, Mapping) else str(node or "")
        )
        for worktree in registered:
            if node_id and worktree.name == node_id and worktree not in occupied:
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


def _reviewed_tree(pointer: Mapping[str, Any]) -> Path | None:
    """The tree a stored review of this run must describe, when the pointer names one.

    A pointer carrying no readable tree field yields nothing rather than the
    current directory: the empty string resolves to ``.``, which would have the
    reader select a review against whatever repository the caller happens to
    stand in, and report a finding against a revision belonging to something
    else. No tree means the selection falls back to the newest stored record
    rather than a revision comparison the caller cannot honestly make.
    """
    for key in ("worktree", "repo"):
        value = str(pointer.get(key) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_dir():
            return path
    return None


def _sub_floor_items(
    project: str,
    session: str,
    floors: Mapping[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return one duty per undisposed review dimension below its declared floor.

    The review is selected through the same rule the classifier and the
    promotion gate use (:func:`reckon.crew.recovery.select_review_for_head`),
    so a record describing a superseded revision cannot stand in for the
    current one. Each row names the run the review is about — the run whose
    work carries the low dimension — and carries the dimension, the score it
    was given and the floor it fell below, so a reader can see the finding
    without opening the record.

    A review with no floor declared for the dimension, and a dimension already
    answered by a disposition in the closed set, produce nothing. The total is
    not consulted: a high total over four strong dimensions does not retire a
    fifth one that sits below its floor, which is the case this duty exists
    for.
    """
    if not floors:
        return []
    items: list[dict[str, Any]] = []
    for pointer in runs.list_live(project=project):
        if str(pointer.get("session") or "") != session:
            continue
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        tree = _reviewed_tree(pointer)
        record, _described = recovery.select_review_for_head(
            project,
            run_id,
            recovery._reviewed_run_head(pointer) if tree is not None else "",
            tree=tree,
        )
        if not record:
            continue
        node = pointer.get("node") or {}
        items.extend(
            {
                "kind": SUB_FLOOR_DUTY_KIND,
                "run_id": run_id,
                "node": str(node.get("id") or run_id),
                "plan": str(node.get("plan") or ""),
                "age_seconds": _seconds_since(record.get("timestamp"), now=now),
                "dimension": finding["dimension"],
                "score": finding["score"],
                "floor": finding["floor"],
                "next_command": (
                    f"dispose {finding['dimension']} scored "
                    f"{finding['score']} against a floor of {finding['floor']} "
                    f"on run {run_id}: folded with the dispatched node's id, or "
                    "exempted with the recorded reason"
                ),
            }
            for finding in review_module.sub_floor_dimensions(record, floors)
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

    items.extend(
        _sub_floor_items(
            project,
            session,
            review_module.declared_dimension_floors(config),
            now=now,
        )
    )
    items.extend(_held_worktrees(project, session, now=now))
    acknowledgements = _acknowledgements_in_force(project, now=now)
    items, acknowledged = _partition_acknowledged(items, acknowledgements)
    items.sort(
        key=lambda item: (
            -int(item["age_seconds"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    acknowledged.sort(
        key=lambda item: (
            str(item["until"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    closure = runs.drain(project, session=session)
    return {
        "project": project,
        "session": session,
        "obligations": items,
        "acknowledged": acknowledged,
        "summary": {
            "count": len(items),
            "oldest_age_seconds": max(
                (int(item["age_seconds"]) for item in items), default=0
            ),
            "unreconciled_runs": int(closure["unreconciled_runs"]),
        },
    }
