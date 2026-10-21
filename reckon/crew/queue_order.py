"""Order queued dispatches by current lane occupancy and time waiting."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew.node import _TERMINAL_RUN_PHASES
from reckon.crew.recovery_vocabulary import REVIEW_ROLE

STARVATION_AGE_SECONDS = 30 * 60


def pointer_is_review(pointer: Mapping[str, Any]) -> bool:
    """Whether a queued request is a review, which the session cap does not bound.

    The dispatch writes the role on the record root and on its node, so either
    spelling names a review and a request carrying neither does not.
    """
    role = str(pointer.get("role") or "").strip()
    if not role:
        node = pointer.get("node")
        role = str(node.get("role") or "").strip() if isinstance(node, Mapping) else ""
    return role == REVIEW_ROLE


def _next_to_launch(
    rows: deque[tuple[datetime, int, Mapping[str, Any]]],
) -> tuple[datetime, int, Mapping[str, Any]]:
    """The row a session launches next: its oldest review, else its oldest row.

    A review of a delivered run is what lets that run land, so it launches ahead
    of the session's queued implementation however long that has waited. The
    oldest row still anchors the session's place in the cross-session order, so
    the review does not make a long-waiting implementation read as fresh.
    """
    for row in rows:
        if pointer_is_review(row[2]):
            return row
    return rows[0]


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
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    moment = now.astimezone(UTC)

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
        waiting[str(pointer["session"])].append(row)

    occupancy = Counter(
        (str(pointer.get("backend") or ""), str(pointer.get("session") or ""))
        for pointer in live_pointers
        if str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        and str(pointer.get("phase") or "") != "queued"
    )

    def _rank(session: str) -> tuple[bool, int, datetime, int]:
        """Rank a session by its oldest wait and the lane its next launch uses.

        The oldest row sets the starvation clock, so a session whose earliest
        request has waited past the age still leads whatever a review ahead of
        it would otherwise mask; the row about to launch supplies the backend
        whose occupancy is charged.
        """
        oldest = waiting[session][0]
        launch = _next_to_launch(waiting[session])
        return (
            moment.timestamp() - oldest[0].timestamp() <= STARVATION_AGE_SECONDS,
            occupancy[(str(launch[2].get("backend") or ""), session)],
            oldest[0],
            oldest[1],
        )

    ordered = []
    while waiting:
        session = min(waiting, key=_rank)
        row = _next_to_launch(waiting[session])
        waiting[session].remove(row)
        pointer = row[2]
        ordered.append(pointer)
        occupancy[(str(pointer.get("backend") or ""), session)] += 1
        if not waiting[session]:
            del waiting[session]
    return ordered
