"""Order queued dispatches by current lane occupancy and time waiting."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew.node import _TERMINAL_RUN_PHASES

STARVATION_AGE_SECONDS = 30 * 60


def admission_order(
    queued_pointers: Sequence[Mapping[str, Any]],
    live_pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[Mapping[str, Any]]:
    """Rank queued pointers, charging each admission to its session's lane use.

    The caller supplies a snapshot from ``runs.list_live``. Queued and terminal
    pointers consume no lane slot; each selected queued pointer consumes one
    in the simulated order. The same session keeps its join order.
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
    ordered = []
    while waiting:
        session = min(
            waiting,
            key=lambda key: (
                moment.timestamp() - waiting[key][0][0].timestamp()
                <= STARVATION_AGE_SECONDS,
                occupancy[(str(waiting[key][0][2].get("backend") or ""), key)],
                waiting[key][0][0],
                waiting[key][0][1],
            ),
        )
        _, _, pointer = waiting[session].popleft()
        ordered.append(pointer)
        occupancy[(str(pointer.get("backend") or ""), session)] += 1
        if not waiting[session]:
            del waiting[session]
    return ordered
