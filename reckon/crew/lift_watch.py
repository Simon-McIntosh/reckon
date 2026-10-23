"""The producer's lift and reset sweeps: the rows it folds onto the stream.

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

The reset family is the module's second entry point. A limit reset
(``reckon.crew.budget_reset``) is the same kind of workstation-wide record: it
reaches every session's pane once, and nothing about it is visible on a stream
unless a producer writes it. It becomes due on the clock rather than on a
document moving — an announced ``--from`` instant passes with every file still,
and so does a reading written to a store outside the change gate's fingerprint —
so its sweep runs on the producer's clock-driven block with a memory of the
event ids it has announced, rather than on the change gate the lift fold reads.

**Expiry is never computed here.** Every in-force verdict and every end cause is
read through :mod:`reckon.crew.budget_lift`'s public :func:`~reckon.crew.budget_lift.end_cause`
ladder, the one ``effective_budget`` also reads through, so the producer cannot
disagree with the resolver the pace path reads.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import budget_lift, budget_reset, recovery_watch
from reckon.crew.budget_lift import END_AT, END_CEILING, END_CLEAR, END_RESET

LIFT_GRANTED = "lift-granted"
LIFT_ENDED = "lift-ended"
LIFT_IN_FORCE = "lift-in-force"
LIMIT_RESET = "limit-reset"

#: How far back the reset sweep reads. It bounds the fleet's wake on a fresh
#: (re)arm: a producer that has just taken its seat has announced nothing, so
#: without a horizon it would replay every reset the store still holds. One hour
#: is long enough that a session stopped at a limit and resumed inside its window
#: still learns of the reset, and short enough that a restart is not a
#: re-announcement of old news.
RESET_HORIZON = timedelta(minutes=60)

__all__ = [
    "END_AT",
    "END_CEILING",
    "END_CLEAR",
    "END_RESET",
    "LIFT_ENDED",
    "LIFT_GRANTED",
    "LIFT_IN_FORCE",
    "LIMIT_RESET",
    "RESET_HORIZON",
    "end_cause",
    "sweep",
    "sweep_resets",
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

    # Read through the settling reader: the first pass that observes a lift's end
    # stamps it on the record, so a reading that flips between two figures cannot
    # end the lift on one poll and revive it on the next under the same id.
    document = budget_lift.settled_document(
        readings_for=lambda lift_group: resolver(lift_group, config, moment),
        now=moment,
        bound=bound,
    )
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


def sweep_resets(
    project: str,
    *,
    seen: Collection[str] | None = None,
    now: datetime | None = None,
    horizon: timedelta | float | None = None,
    stream_path: str | Path | None = None,
    append: Callable[[Path, Sequence[Mapping[str, Any]]], None] | None = None,
    home: Path | None = None,
) -> set[str]:
    """Fold the recent limit resets into rows and return the ids announced.

    This is the module's second entry point rather than a branch of
    :func:`sweep`, because the two run on different triggers with different
    memories. A lift is announced when a document moves, so its sweep rides the
    poll's change gate and remembers the lifts in force. A reset becomes due on
    the clock — the instant the lead stated passes, or a store the gate's
    fingerprint does not cover is written — so this sweep rides the producer's
    clock-driven block and remembers the event ids it has announced. An event
    written with a future instant therefore reaches the stream on the first pass
    after that instant, with no file changed in between.

    ``seen`` is that memory: the ids already announced, or ``None`` before the
    first pass. One row is appended per event whose instant has passed and whose
    id is not in ``seen``, and ids already there are not announced again.

    ``horizon`` bounds how far back the events reach and defaults to
    :data:`RESET_HORIZON`; a producer that has just taken its seat has announced
    nothing, so the horizon is what stops a restart from waking the fleet with an
    old reset. An event whose stated instant is still ahead is never returned by
    the store's reader as due, so nothing older than the horizon is announced.
    """
    moment = _aware(now)
    announced: set[str] = set(seen or ())
    events = budget_reset.recent_resets(
        now=moment,
        horizon=RESET_HORIZON if horizon is None else horizon,
        home=home,
    )
    rows: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id or event_id in announced:
            continue
        instant = budget_reset._parse(event.get("detected_at"))
        if instant is None or instant > moment:
            continue
        rows.append(_reset_row(project, event, moment=moment))
        announced.add(event_id)
    if rows:
        _append_rows(project, stream_path, rows, append)
    return announced


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
    the free-text field the formatter already knows. The row's session is the
    session the lift is scoped to, so it reaches only that session's follower.
    """
    if ended is not None:
        previous, current = LIFT_IN_FORCE, LIFT_ENDED
    elif kind == "baseline":
        previous, current = None, LIFT_IN_FORCE
    else:
        previous, current = None, LIFT_GRANTED
    return _transition_row(
        project,
        kind=kind,
        node=str(lift.get("group") or ""),
        session=_scope_session(lift),
        detail=_lift_detail(lift, kind=kind, ended=ended),
        previous=previous,
        current=current,
        # The lift's id as a structured field, so a reader that has to key a row
        # on the lift it names — the session pane's once-per-lift claim — reads
        # the id rather than parsing it back out of the free-text clause, which
        # the renderer drops. The group stays in ``node`` for the pane's own
        # memory.
        key=str(lift.get("id") or ""),
    )


def _reset_row(
    project: str, event: Mapping[str, Any], *, moment: datetime
) -> dict[str, Any]:
    """One limit-reset row, built as a transition the formatter renders.

    A reset is scoped the way a lift is: every project's producer carries a
    global reset into its own stream, and a session-scoped one reaches only its
    own session's follower, so the row's session is the event's. The row's
    ``node`` is the budget group, which is what the pane's job cell names, and
    the event's id rides the field a lift row carries its lift id in, so the
    pane's shared claim keys on it without a second field. A reset has no left
    side to pair: it is one row, and a later pass holding the id writes nothing.
    """
    return _transition_row(
        project,
        kind="transition",
        node=str(event.get("group") or ""),
        session=str(event.get("session") or ""),
        detail=_reset_detail(event, moment=moment),
        previous=None,
        current=LIMIT_RESET,
        key=str(event.get("id") or ""),
    )


def _transition_row(
    project: str,
    *,
    kind: str,
    node: str,
    session: str,
    detail: str,
    previous: str | None,
    current: str,
    key: str,
) -> dict[str, Any]:
    """The one row builder both sweeps share.

    The row carries the same fields an ordinary run transition carries, so a
    follower renders it without raising, and the clause naming what happened
    rides ``detail``, the free-text field the formatter already knows. ``key`` is
    the structured identity the pane's shared claim reads — a lift's id, or a
    reset event's — carried under the field the claim path already takes.
    """
    snapshot = {
        "run_id": None,
        "node": node,
        "session": session,
        "role": "",
        "backend": "",
        "model": "",
        "effort": "",
        "alias": "",
        "classification": None,
        "process_alive": None,
        "liveness_proven": None,
        "detail": detail,
        "recovery_classification": current,
    }
    row = recovery_watch._watch_transition(
        project,
        kind=kind,
        snapshot=snapshot,
        previous=previous,
        current=current,
        counts={"working": 0, "blocked": 0, "unpromoted": 0},
        spend_runs=[],
    )
    row["lift_id"] = key
    return row


def _reset_detail(event: Mapping[str, Any], *, moment: datetime) -> str:
    """The clause a reset row carries, reading plainly in the pane.

    It names the group and clock the reset belongs to, the instant it is for in
    UTC, and who announced it and why — or, for an event the window readings
    detected, that it was detected rather than announced, which is the one fact
    that separates the two kinds. The closing clause is what the row asks a
    stopped session to do.
    """
    group = str(event.get("group") or "")
    clock = budget_reset._clock_key(event.get("clock"))
    instant = budget_reset._parse(event.get("detected_at")) or moment
    stamp = instant.astimezone(UTC).strftime("%H:%M")
    if str(event.get("kind") or "") == "consumed":
        provenance = "detected from the window readings"
    else:
        provenance = (
            f"announced by {event.get('by') or 'unknown'}: "
            f"{event.get('reason') or 'marked used'}"
        )
    return (
        f"usage limit reset ({group}, {clock}) at {stamp} UTC, {provenance}; "
        "sessions stopped at the usage limit can continue their work"
    )


def _scope_session(lift: Mapping[str, Any]) -> str:
    """The session a session-scoped lift belongs to, or ``""`` for a global one.

    Every project's producer sweeps the workstation's whole lift document into
    its own stream, and a follower skips a row that another session owns. A
    session-scoped lift spends only for that session, so its row names that
    session and other sessions' panes do not draw it. A global lift covers
    every session, so its row stays unattributed and every follower draws it.
    """
    scope = str(lift.get("scope") or "")
    if scope.startswith(budget_lift.SESSION_PREFIX):
        return scope[len(budget_lift.SESSION_PREFIX) :]
    return ""


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
