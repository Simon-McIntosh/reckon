"""Runs nobody will come back to.

A live pointer whose classification is terminal and unpromoted, and whose
owning session has had no follower and no dispatch for a while, is forgotten:
every loop that could move it has stopped, and no reader would ever see it
again. A session may keep its runs as long as it needs them, so this only
reports -- the refusal that fences dispatch is a different list,
``overdue_unreconciled_runs``, and reading one for the other would turn a
report into a block.

Two conditions, both read from the readers that already answer them: the
classifier, and the follower-liveness and unattended-age readings the watcher's
sweep is built on. A pointer whose session is unknown counts as forgotten once
it passes the threshold, because absence is not ownership -- nothing names a
follower for it either way.

The threshold is a parameter of the derivation, defaulting to the six hours a
coordinator has had time to return by.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .follower_registration import _follower_liveness, follower_lock_path
from .recovery_classification import classify_pointer
from .runs import _unattended_seconds, list_live

#: How long a session may go without a follower or a dispatch before its
#: terminal, unpromoted runs read as forgotten.
DEFAULT_THRESHOLD_SECONDS = 6.0 * 60.0 * 60.0

#: The classifications a forgotten run may carry: the work has stopped and
#: nobody has landed its outcome. The waiting family (``waiting``, ``paused``)
#: is deliberately absent -- a paused or declared wait names what lifts it, so
#: it is never the bucket a forgotten run sits in -- and so are ``running``,
#: ``queued`` and ``stopped``, which are work in flight or work a session ended
#: on purpose. The fleet verdict's own words are accepted beside the
#: classifier's because a delivered run reads ``complete`` there and
#: ``promotable`` or ``scoring`` here.
TERMINAL_UNPROMOTED_CLASSIFICATIONS = frozenset(
    {
        "complete",
        "completed_unpromoted",
        "promotable",
        "scoring",
        "blocked",
        "failed",
        "abandoned",
    }
)


def _terminal_unpromoted(row: Mapping[str, Any]) -> bool:
    """Whether a classified row's work has stopped with nothing landed."""

    verdict = row.get("fleet_verdict")
    state = str(verdict.get("state") or "") if isinstance(verdict, Mapping) else ""
    return (
        str(row.get("classification") or "") in TERMINAL_UNPROMOTED_CLASSIFICATIONS
        or state in TERMINAL_UNPROMOTED_CLASSIFICATIONS
    )


def forgotten_runs(
    project: str,
    *,
    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per terminal, unpromoted pointer nobody is coming back to.

    Each row names the run, its node, its session, its classification, how long
    its session has gone unattended and the recovery the classifier already
    offers for it. ``threshold_seconds`` is the unattended age a session must
    pass; a pointer carrying no session has no follower and no dispatch to
    measure, so its own terminal age is what is compared instead.
    """

    moment = datetime.now(tz=UTC) if now is None else now
    now_seconds = moment.timestamp()

    pointers = list(list_live(project=project))
    by_session: dict[str, list[dict[str, Any]]] = {}
    for pointer in pointers:
        session = str(pointer.get("session") or "")
        if session:
            by_session.setdefault(session, []).append(pointer)

    rows: list[dict[str, Any]] = []
    for pointer in pointers:
        row = classify_pointer(pointer, now_seconds=now_seconds)
        if not _terminal_unpromoted(row):
            continue
        session = str(row.get("session") or "")
        unattended: float | None = None
        if session:
            if _follower_liveness(follower_lock_path(project, session)).get("live"):
                continue
            unattended = _unattended_seconds(
                project,
                session,
                now_seconds=now_seconds,
                pointers=by_session.get(session, []),
            )
            age = unattended
            if age is None:
                # Nothing dates the session at all, which is the case the
                # watcher's own sweep reads as unattended: no follower left a
                # stamp and no pointer carries one.
                age = row.get("terminal_age_seconds")
            if age is not None and age < threshold_seconds:
                continue
        else:
            # A pointer naming no session measures nothing, so it is forgotten
            # once its own terminal age passes the threshold.
            age = row.get("terminal_age_seconds")
            if age is not None and age < threshold_seconds:
                continue
        rows.append(
            {
                "run_id": row.get("run_id"),
                "node": row.get("node"),
                "session": session or None,
                "classification": row.get("classification"),
                "fleet_state": (
                    row["fleet_verdict"].get("state")
                    if isinstance(row.get("fleet_verdict"), Mapping)
                    else None
                ),
                "age_seconds": int(age) if age is not None else None,
                "terminal_age_seconds": row.get("terminal_age_seconds"),
                "unattended_seconds": (
                    int(unattended) if unattended is not None else None
                ),
                "recover": row.get("recovery"),
                "next_action": row.get("next_action"),
                "detail": row.get("detail"),
                "worktree": row.get("worktree"),
            }
        )
    rows.sort(
        key=lambda item: (
            -(item["age_seconds"] if item["age_seconds"] is not None else -1),
            str(item["run_id"] or ""),
        )
    )
    return rows


def forgotten_runs_view(
    project: str,
    *,
    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The forgotten-runs read view: the rows, their count and the threshold."""

    rows = forgotten_runs(project, threshold_seconds=threshold_seconds, now=now)
    return {
        "runs": rows,
        "count": len(rows),
        "threshold_seconds": int(threshold_seconds),
    }
