"""The producer's lift sweep: a stream row when a lift starts, one when it ends.

A budget lift (``reckon.crew.budget_lift``) is a durable, workstation-wide record
that raises one budget group's pace multiple for a bounded time and ends by
itself. Every session reads it, but nothing about a lift is visible on the watch
stream a follower reads, so a coordinator watching the fleet cannot see a peer
lift a group or a lift expire on its own.

This module closes that gap. On each producer poll the sweep folds the lift
document beside the fleet fold and appends the rows the follower already knows
how to render: one row when a lift comes into force, one when it leaves, naming
what ended it, and — for a producer that has just (re)armed — a baseline row for
every lift already in force rather than announcing it as new, exactly as pointer
rows read as baseline inventory on a re-arm.

**Expiry is never computed here.** Every in-force verdict and every end cause is
read through :mod:`reckon.crew.budget_lift`'s public :func:`~reckon.crew.budget_lift.end_cause`
ladder, the one ``effective_budget`` also reads through, so the producer cannot
disagree with the resolver the pace path reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew import budget_lift, recovery_watch
from reckon.crew.budget_lift import END_AT, END_CEILING, END_CLEAR, END_RESET

LIFT_GRANTED = "lift-granted"
LIFT_ENDED = "lift-ended"
LIFT_IN_FORCE = "lift-in-force"

__all__ = [
    "END_AT",
    "END_CEILING",
    "END_CLEAR",
    "END_RESET",
    "LIFT_ENDED",
    "LIFT_GRANTED",
    "LIFT_IN_FORCE",
    "end_cause",
    "sweep",
]


def end_cause(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: budget_lift.LiftCeilings,
) -> str | None:
    """What ended a lift that is no longer in force, or ``None`` while it is.

    The one ladder lives in :func:`reckon.crew.budget_lift.end_cause`, the
    resolver ``effective_budget`` reads through, so this is a thin alias rather
    than a second copy of the conditions.
    """
    return budget_lift.end_cause(lift, readings=readings, now=now, bound=bound)


def sweep(
    project: str,
    *,
    seen: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
    readings_for: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
    stream_path: str | Path | None = None,
    append: Callable[[Path, Sequence[Mapping[str, Any]]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fold the lift document into watch-stream rows and return the next memory.

    ``seen`` is the producer's memory of the lifts it has already announced, in
    the shape this function returns: ``None`` on the first sweep after a
    (re)arm, so every lift already in force is written as a baseline, and a
    mapping of lift id to its recorded lift afterwards. A lift in ``seen`` that
    is no longer in force writes one ended row naming its cause; a lift in force
    that is not yet in ``seen`` writes one granted row.

    Nothing is appended when no lift moved, so an unchanged document costs no
    stream write. Every in-force verdict and every end cause is read through
    ``budget_lift``'s helpers, which consume the group's window readings;
    ``readings_for`` supplies them, defaulting to the group's freshest published
    reading.
    """
    moment = _aware(now)
    if config is None:
        config = _resolve_config(project)
    bound = budget_lift.ceilings(config)
    resolver = readings_for if readings_for is not None else _default_readings

    document = budget_lift.read_document()
    by_id: dict[str, dict[str, Any]] = {}
    in_force: dict[str, dict[str, Any]] = {}
    for lift in document.get("lifts", []):
        if not isinstance(lift, Mapping):
            continue
        lift_id = str(lift.get("id") or "")
        if not lift_id:
            continue
        by_id[lift_id] = dict(lift)
        readings = resolver(str(lift.get("group") or ""), config, moment)
        if budget_lift._in_force(lift, readings=readings, now=moment, bound=bound):
            in_force[lift_id] = dict(lift)

    rows: list[dict[str, Any]] = []
    if seen is None:
        rows.extend(
            _lift_row(project, lift, kind="baseline") for lift in in_force.values()
        )
    else:
        for lift_id, lift in in_force.items():
            if lift_id not in seen:
                rows.append(_lift_row(project, lift, kind="transition"))
        for lift_id, remembered in seen.items():
            if lift_id in in_force:
                continue
            # The end cause is read from the document's own record: a clear
            # stamps the document, and the producer's remembered copy predates
            # that stamp, so reading memory would name no cause.
            lift = by_id.get(lift_id, remembered)
            readings = resolver(str(lift.get("group") or ""), config, moment)
            cause = end_cause(lift, readings=readings, now=moment, bound=bound)
            rows.append(_lift_row(project, lift, kind="transition", ended=cause))

    if rows:
        _append_rows(project, stream_path, rows, append)
    return dict(in_force)


def _default_readings(
    group: str, config: Mapping[str, Any] | None, now: datetime
) -> list[dict[str, Any]]:
    """The group's freshest published window reading, in grant's shape."""
    return budget_lift.published_readings(config, group=group, now=now)


def _resolve_config(project: str) -> Mapping[str, Any]:
    from reckon import flight

    return flight.resolve(project=project).config


def _append_rows(
    project: str,
    stream_path: str | Path | None,
    rows: Sequence[Mapping[str, Any]],
    append: Callable[[Path, Sequence[Mapping[str, Any]]], None] | None,
) -> None:
    from reckon.crew import runs

    target = (
        Path(stream_path)
        if stream_path is not None
        else runs.watch_stream_path(project)
    )
    writer = append if append is not None else runs._append_watch_lines
    writer(target, rows)


def _aware(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _lift_row(
    project: str,
    lift: Mapping[str, Any],
    *,
    kind: str,
    ended: str | None = None,
) -> dict[str, Any]:
    """One lift row, built as a transition the established formatter renders.

    The row carries the same fields an ordinary run transition carries, so a
    follower on current main renders it without raising; the lift's group, form,
    multiple, scope and id are named in the row's ``detail`` clause, which is
    the free-text field the formatter already knows.
    """
    group = str(lift.get("group") or "")
    if ended is not None:
        previous, current = LIFT_IN_FORCE, LIFT_ENDED
    elif kind == "baseline":
        previous, current = None, LIFT_IN_FORCE
    else:
        previous, current = None, LIFT_GRANTED
    snapshot = {
        "run_id": None,
        "node": group,
        "session": "",
        "role": "",
        "backend": "",
        "model": "",
        "effort": "",
        "alias": "",
        "classification": None,
        "process_alive": None,
        "liveness_proven": None,
        "detail": _lift_detail(lift, kind=kind, ended=ended),
        "recovery_classification": current,
    }
    return recovery_watch._watch_transition(
        project,
        kind=kind,
        snapshot=snapshot,
        previous=previous,
        current=current,
        counts={"working": 0, "blocked": 0, "unpromoted": 0},
        spend_runs=[],
    )


def _lift_detail(lift: Mapping[str, Any], *, kind: str, ended: str | None) -> str:
    """The clause naming a lift's group, form, multiple, scope and id.

    For a lift that has ended the clause opens with its cause, so the cause is
    the first thing a reader keeps when the clause is cut to the pane: the
    label the state cell already carries leads the text and is dropped by the
    renderer, and the words that survive are the cause and then the naming
    clause.
    """
    named = (
        f"group {lift.get('group')!r}, form {lift.get('form')}, "
        f"multiple {lift.get('pace_multiple')}, scope {lift.get('scope')}, "
        f"id {lift.get('id')!r}"
    )
    if ended is not None:
        return f"lift ended: cause {ended}, {named}"
    if kind == "baseline":
        return f"lift in force: {named}"
    return f"lift granted: {named}"
