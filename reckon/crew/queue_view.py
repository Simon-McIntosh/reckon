"""Where the queue's work stands, ordered by the admission pass's own order.

One read answers "where does my work stand": every queued and awaiting-upstream
request, every pending review and every pending promotion, each with the place
it holds in the order admission launches by, the reason for that place, how long
it has waited and the hold that stops it, beside each session's priority budget
and the points in use and free. Places come from the reader the admission pass
itself orders by, so this document and the next pass cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import runs
from reckon.crew.admission.shared import (
    BLOCKED_UPSTREAM_PHASE,
    HELD_PHASE,
    QUEUED_PHASE,
    WAITING_UPSTREAM_PHASES,
)

__all__ = ["queue_view"]

WAITING_PHASES: tuple[str, ...] = (QUEUED_PHASE, HELD_PHASE, *WAITING_UPSTREAM_PHASES)


def _since(stamp: Any, moment: datetime) -> float | None:
    """The seconds between one recorded instant and now, or ``None``."""
    parsed = parse_utc(stamp)
    if parsed is None:
        return None
    return round(max(0.0, (moment - parsed).total_seconds()), 3)


def _waited_seconds(pointer: Mapping[str, Any], moment: datetime) -> float | None:
    """The wait so far, measured from the request's own arrival stamp."""
    return _since(pointer.get("queued_at"), moment)


def _wait_reason(phase: str, waits: Sequence[str]) -> str:
    """The wait an unplaced request is under, in its own record's terms."""
    named = ", ".join(waits) or "no ref recorded"
    if phase == BLOCKED_UPSTREAM_PHASE:
        return (
            "blocked upstream: a section this request waits on was closed "
            f"unfinished ({named})"
        )
    return f"waiting upstream: {named}"


def _hold(pointer: Mapping[str, Any]) -> dict[str, Any] | None:
    """The hold that stops the request, composed by the admission package."""
    from reckon.crew.admission import verdicts as verdicts_module

    record = verdicts_module.queued_hold(pointer)
    verdict = str(record.get("verdict") or "")
    if not verdict:
        return None
    hold: dict[str, Any] = {
        "verdict": verdict,
        "reason": verdicts_module.hold_reason(record),
    }
    remedy = str(record.get("remedy") or "")
    if remedy:
        hold["remedy"] = remedy
    return hold


def _priority(pointer: Mapping[str, Any]) -> int:
    """The points the request carries, read by the queue ordering's reader."""
    from reckon.crew.queue_order import _priority as ordering_priority

    return ordering_priority(pointer) or 0


def _node_field(pointer: Mapping[str, Any], key: str) -> str:
    """One field of the request's node record, empty when it carries none."""
    node = pointer.get("node")
    if isinstance(node, Mapping):
        return str(node.get(key) or "")
    return ""


def _row(
    pointer: Mapping[str, Any],
    *,
    moment: datetime,
    placement: Mapping[str, Any],
    waits: Sequence[str],
) -> dict[str, Any]:
    """One waiting request as this read publishes it.

    The reason is the admission ordering's own word for a request it places,
    and the request's own recorded wait for one it cannot place, so a reader is
    never shown a reason this module made up.
    """
    phase = str(pointer.get("phase") or "")
    row: dict[str, Any] = {
        "run_id": str(pointer.get("run_id") or ""),
        "node": _node_field(pointer, "id"),
        "session": str(pointer.get("session") or ""),
        "project": str(pointer.get("project") or ""),
        "plan": str(pointer.get("plan") or ""),
        "section": _node_field(pointer, "section"),
        "lane": str(pointer.get("backend") or ""),
        "phase": phase,
        "priority": _priority(pointer),
    }
    position = placement.get("position")
    if position is not None:
        row["position"] = int(position)
    row["waited_seconds"] = _waited_seconds(pointer, moment)
    row["reason"] = str(placement.get("reason") or "") or _wait_reason(phase, waits)
    hold = _hold(pointer)
    if hold is not None:
        row["hold"] = hold
    if waits:
        row["waits"] = list(waits)
    return row


def _sessions(
    names: Sequence[str],
    *,
    pointers: Sequence[Mapping[str, Any]],
    budget: int | None,
) -> list[dict[str, Any]]:
    """Each session's budget beside the points it holds and the points free.

    The figures are the ones a submission is judged against: a session's points
    are counted across every project, because they are the same points wherever
    it placed them, while the budget is the one the named project declares.
    """
    rows: list[dict[str, Any]] = []
    for name in sorted({str(named) for named in names}):
        in_use, holders = runs.session_priority_in_use(name, live=pointers)
        row: dict[str, Any] = {
            "session": name,
            "budget": budget,
            "in_use": in_use,
            "free": None if budget is None else budget - in_use,
            "holders": [
                {"run_id": str(holder.get("run_id") or ""), "priority": int(points)}
                for points, holder in (
                    (holder.get("priority") or 0, holder) for holder in holders
                )
            ],
        }
        rows.append(row)
    return rows


def _promotions(project: str, session: str, *, moment: datetime) -> dict[str, Any]:
    """The promotion queue's waiting rows, filtered and placed by the drain.

    A promotion drains in the order the drain's own report computes, so the
    place beside a row is the place it holds there rather than a second order.
    """
    from reckon.crew.promotion_requests import promotion_queue_report

    report = promotion_queue_report(now=moment)
    rows: list[dict[str, Any]] = []
    for waiting in report["requests"]:
        if project and str(waiting.get("project", "")) != project:
            continue
        if session and str(waiting.get("session") or "") != session:
            continue
        rows.append(
            {
                "run_id": str(waiting.get("run_id") or ""),
                "session": str(waiting.get("session") or ""),
                "project": str(waiting.get("project") or ""),
                "position": int(waiting["position"]),
                "reason": str(waiting.get("reason") or ""),
                "waited_seconds": _since(waiting.get("requested_at"), moment),
            }
        )
    return {
        "waiting": len(rows),
        "requests": rows,
        "fleet": {
            "waiting": int(report["waiting"]),
            "running": int(report["running"]),
            "parked": int(report["parked"]),
            "oldest_wait_seconds": report["oldest_wait_seconds"],
        },
    }


def queue_view(
    project: str,
    *,
    session: str | None = None,
    now: datetime | None = None,
    checkout_path: str | None = None,
) -> dict[str, Any]:
    """Where the queue's work stands, for one project and optionally one session.

    Every waiting request of the project is listed in the order admission
    launches by, with the reason the ordering records for its place, the wait it
    has served, and the hold that stops it. The fleet's waiting places are read
    whole and the project's rows keep the place they hold in that one order,
    because a pass ranks every project's requests against each other; a
    project-scoped read therefore shows the places its own requests hold rather
    than renumbering them behind rows it filtered out.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must have a timezone")
    from reckon.crew.queue_admission import queued_placements, section_waits_for
    from reckon.crew.recovery_review_delivery import _is_review_run

    pointers = runs.list_live()
    placements = queued_placements(pointers, now=moment)
    rows: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    for pointer in pointers:
        phase = str(pointer.get("phase") or "")
        if phase not in WAITING_PHASES:
            continue
        if project and str(pointer.get("project") or "") != project:
            continue
        if session and str(pointer.get("session") or "") != session:
            continue
        row = _row(
            pointer,
            moment=moment,
            placement=placements.get(str(pointer.get("run_id") or "")) or {},
            waits=section_waits_for(pointer),
        )
        rows.append(row)
        if _is_review_run(pointer):
            reviews.append(dict(row))
    placed = [row for row in rows if "position" in row]
    placed.sort(key=lambda row: row["position"])
    unplaced = [row for row in rows if "position" not in row]
    unplaced.sort(key=lambda row: -(row["waited_seconds"] or 0.0))
    requests = placed + unplaced
    reviews.sort(key=lambda row: (row.get("position", len(rows) + 1), row["run_id"]))
    named = {row["session"] for row in rows}
    if session:
        named.add(session)
    from reckon import flight as flight_module

    config = flight_module.resolve(project, checkout_path=checkout_path).config
    budget = flight_module.queue_priority_budget(config)
    promotions = _promotions(project, session or "", moment=moment)
    return {
        "ok": True,
        "view": "queue",
        "project": project,
        "session": session,
        "generated_at": moment.isoformat(),
        "requests": requests,
        "reviews": reviews,
        "sessions": _sessions(named, pointers=pointers, budget=budget),
        "promotions": promotions,
        "counts": {
            "requests": len(requests),
            "reviews": len(reviews),
            "promotions": promotions["waiting"],
        },
    }
