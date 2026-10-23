"""A workstation-wide lift on one budget group's pace, and its own expiry.

A lead sometimes chooses to spend a metered window faster than the configured
pace allows — a window that will be reset with a limit reset they already hold,
or a burst that has to land today. The pace hold would refuse every non-bookend
dispatch, and the only routes that existed were invisible to every peer
session: naming a backend by hand bypasses the hold for one node and leaves no
record, and overriding ``budget.pace_multiple`` on a dispatch lasts one run, is
invisible to other coordinators, and was refused by the session's own safety
classifier as a bypass flag.

A lift replaces both with one durable record every session reads. It carries
who granted it, why, and — decisively — when it stops, so no process has to
remember to lower it.

**Expiry is a pure function of the record, the reading and the clock.** A lift
is in force only while it is uncleared, before its stated end, before the hard
ceiling, and — for a reset-anchored lift — while the group's current reading
still reports the reset the lift was granted against. Nothing runs at the
moment it ends, so a restarted server, a dead producer or an ended session
cannot leave a lift on.

**An end once observed is final.** A reset-anchored lift's end is read from the
group's reading at the moment of use, and a reading that flips between two
figures — two members reporting different ``resets_at``, or a utilisation either
side of the figure recorded at grant — would end the lift on one read and revive
it on the next, under the same id. So the first read that observes an end stamps
it on the record as ``ended_at`` and ``ended_cause``, and :func:`end_cause` reads
that stamp ahead of any reading: no later reading returns the id to in force. The
stamp is the record's own account of the end; it is written by whichever of the
pace path and the producer's sweep first resolves the reading.

**A reset is detected three ways, and any one ends a reset-anchored lift.** The
provider may roll the window on schedule, so the newest reading's ``resets_at``
moving past the stamp recorded at grant by at least half the clock's window ends
the lift — a reset moves a window's end by about a whole window, so a stamp that
only drifts by a minute or so is the same window and does not. A limit
reset may instead be applied early and leave ``resets_at`` where it was, so a
lift records the clock's utilisation at grant and treats a current reading below
that figure as a reset, which catches the common case without history; and it
reads the readings taken since it was granted, treating a fall between two
consecutive readings as a reset, which catches the early one a stale stamp would
hide.

**Resolution happens one step before the pace policy.** A pace policy sees only
a configuration and has no group, reading or clock, so it cannot itself tell
whether a lift is in force. :func:`effective_budget` takes the group, its
readings, the wall clock and the dispatching session, and returns the budget
block with the lifted multiple, the drain-by hold or the uncapped release — or
``config["budget"]`` unchanged when no lift covers the group.

The record lives in one control document per workstation,
``<config-home>/budget-lifts.json``, written atomically under a monotonically
increasing ``version``, in the same shape as the router's gate document.

**A grant supersedes the lift already standing on its group and scope.** The
standing record is cleared as :func:`clear` clears one, ``cleared_by`` naming
the granting user and ``replaced_by`` naming the new lift, and the new lift's
``replaces`` names the record it cleared. The two links are separate fields so
``cleared_by`` always holds a name, and a lift on another scope is untouched.

**An end inside the window is stamped with the group's utilisation there.**
When a lift stops before the window it was anchored to resets — cleared, or
lapsed at its stated end or its hard ceiling — the group's utilisation at that
moment is recorded on the record as ``u_end`` beside the instant as ``t_end``.
A clear stamps it where it clears; a lapse is stamped once by the next budget
read that resolves the group, since nothing runs at the moment a lift ends. A
lift already cleared without a stamp — by a successor, or by a build that
predates the stamp — is filled in the same way from its ``cleared_at``, so a
record written before this stamp existed still hands the pace path its point.
Nothing is granted: the record is the whole of it, and
:func:`re_anchor_origin` hands the pace path the point from which the group's
remaining budget is to be spread to the reset.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reckon import _store as store
from reckon._timestamps import parse_utc
from reckon.crew import pace as pace_module

LIFTS_LEAF = "budget-lifts.json"

SEVEN_DAY = "seven_day"
FIVE_HOUR = "five_hour"
CLOCKS = (SEVEN_DAY, FIVE_HOUR)

CLOCK_HOURS = {SEVEN_DAY: 168.0, FIVE_HOUR: 5.0}

MULTIPLE = "multiple"
DRAIN_BY = "drain_by"
UNCAPPED = "uncapped"
CREDITS = "credits"
FORMS = (MULTIPLE, DRAIN_BY, UNCAPPED, CREDITS)

GLOBAL = "global"
SESSION_PREFIX = "session:"

# The four conditions that end a lift, named the way the watch stream reports
# them. One ladder computes them, in :func:`end_cause`, so the pace path and the
# watch stream never disagree about why a lift stopped.
END_CLEAR = "clear"
END_CEILING = "ceiling"
END_AT = "at"
END_RESET = "reset"

# The causes a recorded end may name, and the cause read from a record whose
# stamp carries none: the latch exists for the reading-dependent end, so an
# unreadable cause is reported as the reset it was stamped for.
END_CAUSES = (END_CLEAR, END_CEILING, END_AT, END_RESET)

DEFAULT_MAX_MULTIPLE = 3.0
DEFAULT_MAX_HOURS = 168.0
# The share of a credit cap a fresh dispatch leaves unspent for the workers
# already drawing on it when the lift fills.
DEFAULT_CREDIT_RESERVE_FRACTION = 0.1

RUN_ID_ENV = "RECKON_RUN_ID"

__all__ = [
    "CLOCKS",
    "CREDITS",
    "DEFAULT_MAX_HOURS",
    "DEFAULT_MAX_MULTIPLE",
    "DRAIN_BY",
    "END_AT",
    "END_CEILING",
    "END_CLEAR",
    "END_RESET",
    "FIVE_HOUR",
    "FORMS",
    "GLOBAL",
    "LIFTS_LEAF",
    "MULTIPLE",
    "SEVEN_DAY",
    "UNCAPPED",
    "LiftRefusedError",
    "ceilings",
    "clear",
    "drain_line",
    "effective_budget",
    "end_cause",
    "grant",
    "lifts_path",
    "list_lifts",
    "published_readings",
    "re_anchor_origin",
    "record_observed_end",
    "settled_document",
    "under_pace_hold",
]


class LiftRefusedError(ValueError):
    """A grant was refused before it was recorded."""


@dataclass(frozen=True, slots=True)
class LiftCeilings:
    """The declared ceilings on a lift, as resolved flight config carries them."""

    max_multiple: float
    max_hours: float


def lifts_path() -> Path:
    """Return the lift document's path, directly under the shared config home."""
    return store._config_home() / LIFTS_LEAF


def ceilings(config: Mapping[str, Any] | None) -> LiftCeilings:
    """Read the lift ceilings out of resolved flight config.

    Both are declared flight keys under ``budget.lift``; absence falls back to
    the shipped default, so a configuration that sets neither still bounds a
    lift. A non-finite value is read as the default rather than inverted.
    """
    block = (config or {}).get("budget") or {}
    lift = block.get("lift") or {}
    max_multiple = _finite(lift.get("max_multiple"), DEFAULT_MAX_MULTIPLE)
    max_hours = _finite(lift.get("max_hours"), DEFAULT_MAX_HOURS)
    return LiftCeilings(
        max_multiple=max(0.0, max_multiple),
        max_hours=max(0.0, max_hours),
    )


def configured_pace_multiple(config: Mapping[str, Any] | None) -> float:
    """Return the configured pace multiple, the floor a lift must exceed.

    Read through the pace policy so the figure a configuration with no
    ``pace_multiple`` resolves to is the pace module's own default rather than a
    second copy of it kept here, which could drift from the one the allowance
    itself is derived against.
    """
    return float(pace_module.policy(config).pace_multiple)


def published_readings(
    config: Mapping[str, Any] | None,
    *,
    group: str,
    path: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The group's freshest published window reading, in :func:`grant`'s shape.

    A lift is workstation-wide, and so is the headroom document the observer
    publishes, so the reading a grant projects from is the group's own freshest
    member reading there rather than a project's recorded evidence. No document,
    or one reaching none of the group's members, yields an empty list, and the
    grant then records no projection rather than an invented one.
    """
    from reckon.crew import paid_lanes

    members = _declared_group_members(config, group)
    if not members:
        return []
    moment = _aware(now) if now is not None else None
    document = paid_lanes.read_document(path)
    windows = paid_lanes.document_windows(document, moment=moment)
    best = None
    for member in members:
        reading = windows.get(member)
        if reading is None or reading.observed_at is None:
            continue
        if best is None or reading.observed_at > best.observed_at:
            best = reading
    if best is None:
        return []
    payload: dict[str, Any] = {"observed_at": best.observed_at.isoformat()}
    for figure in best.figures:
        payload[str(figure.period)] = {
            "utilisation": float(figure.utilisation),
            "resets_at": figure.resets_at,
        }
    return [payload]


def read_document(path: str | Path | None = None) -> dict[str, Any]:
    """Read the lift document, returning an empty one when none is written.

    A missing file, an unreadable one and one carrying no ``lifts`` list are all
    an empty record rather than an error: a workstation that has never granted a
    lift and one whose document cannot be read both resolve no lift, and
    resolving no lift is the un-lifted state, which is safe.
    """
    target = Path(path) if path is not None else lifts_path()
    if not target.exists():
        return {"version": 0, "lifts": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 0, "lifts": []}
    if not isinstance(payload, Mapping):
        return {"version": 0, "lifts": []}
    lifts = payload.get("lifts")
    version = payload.get("version")
    return {
        "version": int(version) if isinstance(version, int) else 0,
        "lifts": [dict(lift) for lift in lifts] if isinstance(lifts, list) else [],
    }


def _write_document(document: Mapping[str, Any], *, path: str | Path | None) -> Path:
    """Write the document under the next version, atomically.

    ``version`` increases by one on every write so a reader can tell a re-read
    document from a stale one, exactly as the router's gate document does.
    """
    target = Path(path) if path is not None else lifts_path()
    current = read_document(target)
    next_version = int(current.get("version", 0)) + 1
    payload = {"version": next_version, "lifts": list(document.get("lifts", []))}
    return store.write_json_atomically(target, payload)


def grant(
    config: Mapping[str, Any] | None,
    *,
    group: str,
    reason: str | None,
    form: str = MULTIPLE,
    multiple: float | None = None,
    target: str | datetime | None = None,
    clock: str = SEVEN_DAY,
    ends: Mapping[str, Any] | None = None,
    scope: str = GLOBAL,
    session: str | None = None,
    starts_at: str | datetime | None = None,
    readings: Sequence[Mapping[str, Any]] | None = None,
    granted_by: str | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    path: str | Path | None = None,
    credits: float | None = None,
    balance_at_grant: float | None = None,
    reserve_fraction: float | None = None,
) -> dict[str, Any]:
    environment = environ if environ is not None else os.environ
    moment = _aware(now) if now is not None else datetime.now(UTC)

    if str(form) not in FORMS:
        raise LiftRefusedError(
            f"unknown lift form {form!r} (one of {', '.join(FORMS)})"
        )
    if environment.get(RUN_ID_ENV):
        raise LiftRefusedError(
            "a lift is spend, and this environment names a run "
            f"({RUN_ID_ENV} is set), so a worker cannot lift its own budget"
        )
    group_name = str(group or "").strip()
    if not group_name:
        raise LiftRefusedError("a lift names a declared budget group")
    declared = _declared_group_names(config)
    if group_name not in declared:
        listed = ", ".join(sorted(declared)) or "none"
        raise LiftRefusedError(
            f"{group_name!r} is not a declared budget group (declared: {listed})"
        )
    if not (isinstance(reason, str) and reason.strip()):
        raise LiftRefusedError(
            "a lift without a reason is refused; --reason is required"
        )

    selected_scope = _scope(scope, session)
    selected_clock = str(clock) if clock in CLOCKS else SEVEN_DAY
    newest = _newest_reading(readings)
    bound = ceilings(config)
    configured = configured_pace_multiple(config)

    resolved_multiple: float | None = None
    drain_fields: dict[str, Any] = {}
    if str(form) == MULTIPLE:
        if multiple is None:
            raise LiftRefusedError("the multiple form names --multiple")
        resolved_multiple = float(multiple)
        if resolved_multiple <= configured:
            raise LiftRefusedError(
                "a lift must exceed the configured pace multiple "
                f"{configured:g}; {resolved_multiple:g} does not"
            )
        if resolved_multiple > bound.max_multiple:
            raise LiftRefusedError(
                "a lift multiple may not exceed budget.lift.max_multiple "
                f"{bound.max_multiple:g}; {resolved_multiple:g} does"
            )
    elif str(form) == CREDITS:
        if credits is None or float(credits) <= 0:
            raise LiftRefusedError("the credits form names a positive --credits cap")
        if balance_at_grant is None:
            raise LiftRefusedError(
                "a credit lift is measured from the balance at grant, and no "
                "credit balance could be read from the account surface"
            )
        fraction = (
            DEFAULT_CREDIT_RESERVE_FRACTION
            if reserve_fraction is None
            else float(reserve_fraction)
        )
        if not 0.0 <= fraction < 1.0:
            raise LiftRefusedError(
                f"the credit reserve fraction must lie in [0, 1); {fraction:g} does not"
            )
        drain_fields = {
            "credit_cap": float(credits),
            "credit_balance_at_grant": float(balance_at_grant),
            "credit_reserve_fraction": fraction,
        }
    elif str(form) == DRAIN_BY:
        if target is None:
            raise LiftRefusedError("the drain-by form names --drain-by")
        target_moment = _parse_stamp(target)
        if target_moment is None:
            raise LiftRefusedError(f"the drain-by target {target!r} could not be read")
        clamped = _clamp_target(target_moment, newest, selected_clock)
        u0, t0, week_start = _placement(newest, selected_clock, moment)
        drain_fields = {
            "target": _iso(clamped if clamped is not None else target_moment),
            "u0": u0,
            "t0": _iso(t0) if t0 is not None else None,
            "week_start": _iso(week_start) if week_start is not None else None,
        }

    granted_at = moment
    granting_user = str(granted_by or environment.get("USER") or "unknown")
    starts = _parse_stamp(starts_at) if starts_at is not None else None
    end_block = _end_block(
        ends=ends,
        form=str(form),
        clock=selected_clock,
        readings=readings,
        target=drain_fields.get("target"),
    )
    lift: dict[str, Any] = {
        "id": _new_id(group_name, granted_at),
        "group": group_name,
        "form": str(form),
        "pace_multiple": resolved_multiple,
        "ends": end_block,
        "scope": selected_scope,
        "starts_at": _iso(starts) if starts is not None else _iso(granted_at),
        "granted_by": granting_user,
        "granted_at": _iso(granted_at),
        "reason": str(reason).strip(),
        "projected_exhaustion": _projection(newest, selected_clock, resolved_multiple),
        "cleared_by": None,
        "cleared_at": None,
        "replaced_by": None,
        "replaces": None,
    }
    lift.update(drain_fields)

    document = read_document(path)
    replaced = _replace_standing(
        document["lifts"],
        group=group_name,
        scope=selected_scope,
        new_id=lift["id"],
        cleared_by=granting_user,
        readings=readings,
        now=granted_at,
        bound=bound,
    )
    if replaced is not None:
        lift["replaces"] = replaced
    document["lifts"].append(lift)
    _write_document(document, path=path)
    return lift


def clear(
    config: Mapping[str, Any] | None = None,
    *,
    group: str,
    lift_id: str | None = None,
    session: str | None = None,
    cleared_by: str | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Revoke an in-force lift on ``group``, recording who and when.

    The lift revoked is named one of two ways. ``lift_id`` revokes that exact
    record, but only when it belongs to ``group``: an id naming a different
    group's lift is refused rather than revoked, because the id path may revoke
    only what the group path could, and the group path never reaches another
    group's lift. Otherwise the lift is resolved for ``session``: a session's
    own lift governs it over a global one, and with no session given only a
    global lift is a candidate, so a bare clear never revokes a session-scoped
    lift out from under the session that holds it.

    Ended lifts are kept as history so the multiple can be tuned against what it
    did; a clear stamps the record rather than removing it. ``None`` is returned
    when no in-force lift matched -- an id that names no lift, or one past its
    end, older than the hard ceiling, or already cleared -- so a caller can tell
    a revoke from a no-op.
    """
    environment = environ if environ is not None else os.environ
    moment = _aware(now) if now is not None else datetime.now(UTC)
    document = read_document(path)
    if lift_id is not None:
        named = _lift_by_id(document["lifts"], lift_id=str(lift_id))
        if named is None:
            return None
        named_group = str(named.get("group") or "")
        if named_group != str(group):
            raise LiftRefusedError(
                f"lift {lift_id!r} belongs to budget group {named_group!r}, not "
                f"{str(group)!r}; clearing by id revokes only a lift the group "
                "path could name, so --group and --id must agree"
            )
        governing = named
    else:
        governing = _governing_lift(
            document["lifts"],
            group=str(group),
            readings=None,
            now=moment,
            session=session,
            bound=ceilings(config),
        )
    if governing is None:
        return None
    if not _in_force(governing, readings=None, now=moment, bound=ceilings(config)):
        return None
    governing["cleared_by"] = str(cleared_by or environment.get("USER") or "unknown")
    governing["cleared_at"] = _iso(moment)
    _stamp_end(
        governing,
        reading=_newest_reading(
            published_readings(config, group=str(group), now=moment)
        ),
        lapse=moment,
    )
    _write_document(document, path=path)
    return governing


def _replace_standing(
    lifts: Sequence[Mapping[str, Any]],
    *,
    group: str,
    scope: str,
    new_id: str,
    cleared_by: str,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> str | None:
    """Clear the lift already in force on ``group`` and ``scope``, if any.

    A grant on a group and scope supersedes the lift already standing there
    rather than sitting beside it. The superseded record is cleared the way
    :func:`clear` clears one — ``cleared_by`` naming the granting user, which
    is what the caller passes, so a single name serves whichever lift was
    standing — and gains ``replaced_by`` naming the new lift. The new lift's
    ``replaces`` names the record it cleared. The two links are separate fields
    so ``cleared_by`` always holds a name and the pair is readable from either
    side. A lift on another scope, or on another group, is untouched. Returns
    the replaced lift's id, or ``None`` when none on that group and scope was in
    force.
    """
    moment = _aware(now)
    candidates = [
        lift
        for lift in lifts
        if lift.get("group") == group
        and str(lift.get("scope") or GLOBAL) == scope
        and _in_force(lift, readings=readings, now=moment, bound=bound)
    ]
    if not candidates:
        return None
    standing = max(candidates, key=lambda lift: str(lift.get("granted_at") or ""))
    standing["cleared_by"] = cleared_by
    standing["cleared_at"] = _iso(moment)
    standing["replaced_by"] = new_id
    return str(standing.get("id"))


def list_lifts(
    *,
    group: str | None = None,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List active and recent lifts, newest first, optionally for one group."""
    document = read_document(path)
    rows = [
        lift
        for lift in document["lifts"]
        if group is None or lift.get("group") == str(group)
    ]
    rows.sort(key=lambda lift: str(lift.get("granted_at") or ""), reverse=True)
    return rows


def record_observed_end(
    lift: dict[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> bool:
    """Stamp a lift's end the first time a read observes it, and no later.

    A lift's end is read at the moment of use, so a reading that flips between
    two figures ends a reset-anchored lift on one read and revives it on the
    next. Recording the end the first time it is observed makes it final:
    :func:`end_cause` reads ``ended_at`` ahead of every reading, so no later
    reading returns the id to in force, and the producer emits one
    ``lift-granted`` and one ``lift-ended`` for the id.

    A lift that has not started, or that carries no grant, is never stamped,
    because neither is an observed end. Returns whether the record changed.
    """
    if lift.get("ended_at"):
        return False
    if _parse_stamp(lift.get("granted_at")) is None:
        return False
    starts = _parse_stamp(lift.get("starts_at"))
    if starts is not None and _aware(now) < starts:
        return False
    cause = end_cause(lift, readings=readings, now=now, bound=bound)
    if cause is None:
        return False
    lift["ended_at"] = _iso(now)
    lift["ended_cause"] = cause
    return True


def settled_document(
    *,
    readings_for: Callable[[str], Sequence[Mapping[str, Any]] | None],
    now: datetime,
    bound: LiftCeilings,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Read the lift document, stamping every end this pass observes.

    ``readings_for`` resolves one lift's group to the window readings the end is
    judged against, exactly as the pace path and the producer's sweep each hand
    readings over. Every lift whose end this pass observes is stamped once by
    :func:`record_observed_end`, and the document is written back only when a
    stamp was added, so an unchanged document costs no write.

    The producer's sweep reads the document through here rather than through
    :func:`read_document`, so the flip that would otherwise alternate a lift
    between in force and ended is settled on the record at the first observation.
    """
    document = read_document(path)
    changed = False
    for lift in document["lifts"]:
        readings = readings_for(str(lift.get("group") or ""))
        changed = (
            record_observed_end(lift, readings=readings, now=now, bound=bound)
            or changed
        )
    if changed:
        _write_document(document, path=path)
    return document


def effective_budget(
    config: Mapping[str, Any] | None,
    *,
    group: str,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    session: str | None = None,
    path: str | Path | None = None,
) -> Mapping[str, Any]:
    """Return the budget block in force for ``group`` at ``now``.

    A lift raises the group's pace multiple and releases all three reserves, so
    the group may spend its whole window. While one is in force the returned
    block carries the lifted multiple — or the drain-by hold or uncapped release
    — its id, and ``resume_reserve_pct``, ``coordinator_reserve_pct`` and
    ``bookend_reserve_pct`` all zero. Otherwise ``config["budget"]`` is returned
    unchanged.

    Expiry is read here rather than run anywhere: the governing lift is resolved
    from the record, the group's readings and the wall clock alone, and the
    readings may end a reset-anchored lift just as the clock's own reset stamp
    does.

    A session lift applies only to the session it names, a global one to every
    session, and where both apply the session lift governs its own session. The
    session is taken only from the argument: a resolver that reached for the
    environment would answer a different question from the one its caller asked.

    The returned block never carries ``pace_multiple`` as ``None``. Only the
    multiple form raises the multiple, so it carries the lifted figure; the
    drain-by and uncapped forms keep the configured multiple and mark their own
    hold in ``pace_hold``, which is what a reader consults for those forms.
    """
    block = (config or {}).get("budget") or {}
    moment = _aware(now)
    document = read_document(path)
    stamped = _stamp_lapsed_ends(
        document,
        config=config,
        group=str(group),
        readings=readings,
        now=moment,
        bound=ceilings(config),
    )
    if _stamp_cleared_ends(
        document,
        config=config,
        group=str(group),
        readings=readings,
        now=moment,
        path=path,
    ):
        stamped = True
    if _stamp_observed_ends(
        document,
        group=str(group),
        readings=readings,
        now=moment,
        bound=ceilings(config),
    ):
        stamped = True
    if stamped:
        _write_document(document, path=path)
    lift = _governing_lift(
        document["lifts"],
        group=str(group),
        readings=readings,
        now=moment,
        session=session,
        bound=ceilings(config),
    )
    if lift is None:
        return block

    released = dict(block)
    released["resume_reserve_pct"] = 0.0
    released["coordinator_reserve_pct"] = 0.0
    released["bookend_reserve_pct"] = 0.0
    released["lift_id"] = lift["id"]
    released["pace_multiple"] = (
        float(lift["pace_multiple"])
        if lift.get("form") == MULTIPLE
        else configured_pace_multiple(config)
    )
    released["lift"] = _lift_summary(lift, readings=readings, now=moment)
    released["pace_hold"] = _hold_marker(lift, readings=readings, now=moment)
    if lift.get("form") == CREDITS:
        released["credit_lift"] = {
            "id": lift["id"],
            "cap": lift.get("credit_cap"),
            "balance_at_grant": lift.get("credit_balance_at_grant"),
            "reserve_fraction": lift.get("credit_reserve_fraction"),
        }
    return released


def under_pace_hold(
    lift: Mapping[str, Any],
    *,
    utilisation: float,
    now: datetime,
) -> bool:
    """Whether a lift withholds a dispatch at this utilisation.

    Only the drain-by form withholds by utilisation: it holds while the reading
    stands above the straight line from the utilisation and time recorded at
    grant to 100% at its target, and admits at or below it. An uncapped lift
    marks no pace hold at all. The multiple form's hold is judged by burn
    against its multiple, which a single utilisation figure cannot express, so
    this predicate reports ``False`` for it and the pace module decides.
    """
    if str(lift.get("form")) != DRAIN_BY:
        return False
    return float(utilisation) > drain_line(lift, now=_aware(now))


if TYPE_CHECKING:  # pragma: no cover - typing only
    from reckon.budget import BudgetState


def _credit_lift_verdict(
    state: BudgetState, lift: Mapping[str, Any], purpose: str
) -> tuple[bool, str] | None:
    """Judge a spent window against a credit lift: ``(held, reason)``.

    The provider keeps serving past an exhausted window while the account holds
    credits, so a credit lift admits work until the credits spent since the
    grant reach its cap. Spend is the balance at grant less the balance now, so
    it counts every draw on the account, not only this host's runs. A fresh
    dispatch stops short of the cap by the lift's reserve, because a hold stops
    only new work and every worker already running keeps drawing; a resume may
    spend the reserve, which is what it was withheld for.

    Returns ``None`` when the reading carries no balance, so the window ceiling
    decides: a lift must never admit spend it cannot measure.
    """
    if state.credit_balance is None:
        return None
    try:
        cap = float(lift.get("cap"))
        at_grant = float(lift.get("balance_at_grant"))
    except (TypeError, ValueError):
        return None
    reserve = lift.get("reserve_fraction")
    fraction = (
        float(reserve)
        if isinstance(reserve, (int, float)) and not isinstance(reserve, bool)
        else DEFAULT_CREDIT_RESERVE_FRACTION
    )
    stop = cap if purpose == "resume" else cap * (1.0 - fraction)
    spent = max(0.0, at_grant - state.credit_balance)
    position = (
        f"credit lift {lift.get('id')!r} has spent {spent:,.0f} of its "
        f"{cap:,.0f}-credit cap (a {purpose} stops at {stop:,.0f}); "
        f"balance {state.credit_balance:,.2f}"
    )
    if not state.credits_available:
        return True, f"the account reports no usable credits; {position}"
    if spent >= stop:
        return True, f"{position}, so the lift is spent"
    utilisation = (
        f"{state.utilisation_pct:g}%" if state.utilisation_pct is not None else "spent"
    )
    return (
        False,
        (
            f"the {state.backend!r} window is {utilisation}, and the provider "
            f"serves past it on credits; {position}"
        ),
    )


def drain_line(lift: Mapping[str, Any], *, now: datetime) -> float:
    """The utilisation a drain-by lift uses as its threshold at ``now``.

    The line runs from the utilisation and instant recorded at grant to 100% at
    the lift's target, with no lean applied: the target already says how fast to
    spend. Outside the interval the line is clamped to its endpoints.
    """
    moment = _aware(now)
    u0 = float(lift.get("u0") or 0.0)
    t0 = _parse_stamp(lift.get("t0"))
    target = _parse_stamp(lift.get("target"))
    if t0 is None or target is None or target <= t0:
        return 1.0
    fraction = (moment - t0).total_seconds() / (target - t0).total_seconds()
    fraction = min(max(fraction, 0.0), 1.0)
    return u0 + (1.0 - u0) * fraction


def re_anchor_origin(
    *,
    group: str,
    path: str | Path | None = None,
) -> tuple[float, datetime, str] | None:
    """Where a group's most recently ended lift left its utilisation.

    A lift that stopped inside its window records the utilisation it stopped at
    as ``u_end`` and the instant as ``t_end``. The pace path reads the newest
    such point — the largest ``t_end`` on the group — because the group's
    remaining budget is spread from the last time a lift let it go. A group
    whose lifts all ended on a window reset, or that never carried one, has no
    stamped end and reports ``None``, which is an absence rather than a point at
    zero. The lift id rides back with the point so a reader can name the record.
    """
    best: tuple[float, datetime, str] | None = None
    for lift in read_document(path)["lifts"]:
        if lift.get("group") != str(group):
            continue
        end = _parse_stamp(lift.get("t_end"))
        utilisation = lift.get("u_end")
        if end is None or not _is_number(utilisation):
            continue
        if best is None or end > best[1]:
            best = (float(utilisation), end, str(lift.get("id") or ""))
    return best


def _stamp_end(
    lift: dict[str, Any],
    *,
    reading: Mapping[str, Any] | None,
    lapse: datetime,
) -> bool:
    """Record the group's utilisation at ``lapse`` and the instant, once.

    ``u_end`` and ``t_end`` travel together: a reading that carries no figure
    for the lift's own clock stamps neither, so the record still reads as
    unstamped and a later read with a usable reading can fill it in. The reading
    used is named by when it was observed, and a reading observed after the end
    instant is marked, because a figure taken later than the end is the best
    available account of it rather than a measurement of it. ``False`` when the
    lift carries no figure to record, so a caller does not write a document for
    a stamp that did not happen.
    """
    utilisation = _clock_utilisation(reading, _lift_clock(lift))
    if utilisation is None:
        return False
    lift["u_end"] = utilisation
    lift["t_end"] = _iso(lapse)
    observed = _parse_stamp((reading or {}).get("observed_at"))
    if observed is not None:
        lift["u_end_taken_at"] = _iso(observed)
        if observed > _aware(lapse):
            lift["u_end_late"] = True
    return True


def _stamp_lapsed_ends(
    document: dict[str, Any],
    *,
    config: Mapping[str, Any] | None,
    group: str,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> bool:
    """Stamp each lift that lapsed inside its window, once, from its readings.

    A lift that reaches its stated end or its hard ceiling before the window it
    was anchored to resets has stopped by its own record, and nothing runs at
    that moment. The first budget read that resolves the group after the lapse
    records the group's utilisation then as ``u_end`` and the lapse instant as
    ``t_end``, so the pace path reads one point rather than one per read. A lift
    a clear already stamped, one already stamped, and one the window's reset
    ended are all left alone, and a read carrying no reading stamps nothing so
    the next read that has one can. ``True`` when the document changed.
    """
    newest = _newest_reading(readings)
    if newest is None:
        return False
    changed = False
    for lift in document["lifts"]:
        if lift.get("group") != str(group):
            continue
        if lift.get("t_end") is not None or lift.get("cleared_at"):
            continue
        cause = end_cause(lift, readings=readings, now=now, bound=bound)
        if cause not in (END_AT, END_CEILING):
            continue
        lapse = _lapse_moment(lift, cause=cause, bound=bound)
        if lapse is None:
            continue
        clock = _lift_clock(lift)
        reset = _parse_stamp((lift.get("ends") or {}).get("resets_at"))
        if reset is None:
            reset = _clock_reset(newest, clock)
        if reset is not None:
            reset = _aware(reset)
            # Inside the window that has just closed, or an earlier one: the
            # reset already took the spend back, so there is no remaining
            # budget to spread from the lapse and no point to record.
            if not lapse < reset:
                continue
            if lapse < reset - timedelta(hours=CLOCK_HOURS.get(clock, 168.0)):
                continue
        changed = _stamp_end(lift, reading=newest, lapse=lapse) or changed
    return changed


def _stamp_cleared_ends(
    document: dict[str, Any],
    *,
    config: Mapping[str, Any] | None,
    group: str,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    path: str | Path | None,
) -> bool:
    """Backfill the end of a lift cleared before its stamp was recorded.

    A clear records where the group stood as it stopped, but a lift cleared by a
    successor, or by a build that predates the stamp, carries ``cleared_at``
    alone. The re-anchor reads the end point and cannot see the lift without it,
    so the newest cleared lift on the group with no stamp is filled in once by
    the next budget read that has a reading. Its end instant is the clear, and
    the figure is the newest reading observed at or before that instant when one
    is available, so a read made long after the clear still reports where the
    group actually stood. With none available the current reading is used and
    marked late, which is a worse account of the clear and still a truer one
    than continuing to hold on spend the lift authorised. A clear the window's
    reset has already passed is left alone: it ended with its window, and the
    re-anchor is for a lift that stopped inside one. ``True`` when the document
    changed.
    """
    newest = _newest_reading(readings)
    if newest is None:
        return False
    candidates = [
        lift
        for lift in document["lifts"]
        if lift.get("group") == str(group)
        and lift.get("cleared_at")
        and lift.get("t_end") is None
    ]
    if not candidates:
        return False
    lift = max(candidates, key=lambda item: str(item.get("cleared_at") or ""))
    cleared = _parse_stamp(lift.get("cleared_at"))
    if cleared is None:
        return False
    clock = _lift_clock(lift)
    reset = _clock_reset(newest, clock)
    if reset is None:
        return False
    if not cleared < _aware(reset):
        return False
    window_start = _aware(reset) - timedelta(hours=CLOCK_HOURS.get(clock, 168.0))
    if cleared < window_start:
        return False
    published = published_readings(config, group=str(group), now=now, path=path)
    history = [item for item in (readings or []) if isinstance(item, Mapping)]
    history.extend(item for item in published if isinstance(item, Mapping))
    before = [
        item
        for item in history
        if (_parse_stamp(item.get("observed_at")) or cleared) <= cleared
    ]
    pool = before or history
    if not pool:
        return False
    reading = max(
        pool,
        key=lambda item: (
            _parse_stamp(item.get("observed_at")) or datetime.min.replace(tzinfo=UTC)
        ),
    )
    return _stamp_end(lift, reading=reading, lapse=cleared)


def _stamp_observed_ends(
    document: dict[str, Any],
    *,
    group: str,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> bool:
    """Stamp every end of ``group`` this read observes, once.

    The pace path's half of :func:`settled_document`: a read that reaches one
    group's lifts stamps their observed ends without touching another group's,
    exactly as :func:`_stamp_lapsed_ends` reaches only its own group's lapse.
    ``True`` when the document changed.
    """
    changed = False
    for lift in document["lifts"]:
        if lift.get("group") != str(group):
            continue
        changed = (
            record_observed_end(lift, readings=readings, now=now, bound=bound)
            or changed
        )
    return changed


def _lapse_moment(
    lift: Mapping[str, Any],
    *,
    cause: str,
    bound: LiftCeilings,
) -> datetime | None:
    """The instant a lift stopped by its own record, or ``None``."""
    if cause == END_CEILING:
        granted = _parse_stamp(lift.get("granted_at"))
        return granted + timedelta(hours=bound.max_hours) if granted else None
    return _parse_stamp((lift.get("ends") or {}).get("at"))


def _hold_marker(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
) -> dict[str, Any]:
    """A machine-readable account of how the lift withholds at ``now``."""
    form = str(lift.get("form"))
    if form == UNCAPPED:
        return {"kind": UNCAPPED, "held": False}
    if form == CREDITS:
        # The credit cap is enforced where the window ceiling is judged, against
        # the account's live balance; the pace has nothing left to meter.
        return {"kind": CREDITS, "held": False}
    if form == DRAIN_BY:
        newest = _newest_reading(readings)
        utilisation = _clock_utilisation(newest, _lift_clock(lift))
        line = drain_line(lift, now=now)
        return {
            "kind": DRAIN_BY,
            "line": line,
            "held": None if utilisation is None else utilisation > line,
        }
    return {"kind": MULTIPLE, "multiple": lift.get("pace_multiple"), "held": None}


def _lift_summary(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
) -> dict[str, Any]:
    """The compact lift block a budget view or a dispatch row carries."""
    end, ends_on = _end_moment(lift, readings=readings)
    return {
        "id": lift.get("id"),
        "group": lift.get("group"),
        "form": lift.get("form"),
        "scope": lift.get("scope"),
        "pace_multiple": lift.get("pace_multiple"),
        "ends_at": _iso(end) if end is not None else None,
        "ends_on": ends_on,
        "remaining_seconds": (
            None if end is None else max(0.0, (end - _aware(now)).total_seconds())
        ),
    }


def _lift_clock(lift: Mapping[str, Any]) -> str:
    """The clock a lift's own end anchors to, defaulting to the week."""
    ends = lift.get("ends") or {}
    clock = str(ends.get("clock") or SEVEN_DAY)
    return clock if clock in CLOCKS else SEVEN_DAY


def _lift_by_id(
    lifts: Sequence[Mapping[str, Any]],
    *,
    lift_id: str,
) -> Mapping[str, Any] | None:
    """Return the recorded lift with ``lift_id``, or ``None``.

    ``cleared_at`` and expiry are not consulted here: a caller clearing by id
    has named the exact record, and whether it is still in force is settled by
    the same check every clear applies to whatever it resolves.
    """
    for lift in lifts:
        if str(lift.get("id")) == str(lift_id):
            return lift
    return None


def _governing_lift(
    lifts: Sequence[Mapping[str, Any]],
    *,
    group: str,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    session: str | None,
    bound: LiftCeilings,
) -> Mapping[str, Any] | None:
    """Return the lift in force for ``group``, or ``None``.

    A session lift is chosen over a global one for the session it names, since
    that lift governs its own session; otherwise the newest in-force lift of the
    applicable scope wins.
    """
    moment = _aware(now)
    in_force = [
        lift
        for lift in lifts
        if _applies(lift, group=group, session=session)
        and _in_force(lift, readings=readings, now=moment, bound=bound)
    ]
    if not in_force:
        return None
    named = [
        lift
        for lift in in_force
        if str(lift.get("scope", "")).startswith(SESSION_PREFIX)
    ]
    pool = named or in_force
    return max(pool, key=lambda lift: str(lift.get("granted_at") or ""))


def _applies(lift: Mapping[str, Any], *, group: str, session: str | None) -> bool:
    """Whether a lift's group and scope cover this dispatch."""
    if lift.get("group") != group:
        return False
    scope = str(lift.get("scope") or GLOBAL)
    if scope == GLOBAL:
        return True
    if scope.startswith(SESSION_PREFIX):
        return session is not None and scope[len(SESSION_PREFIX) :] == str(session)
    return False


def end_cause(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> str | None:
    """Why a lift is no longer in force at ``now``, or ``None`` while it is.

    The four conditions that end a lift, in the order the pace path reads them:
    an explicit clear, the hard ceiling, a stated instant, and a reset observed
    on the group's window — the scheduled rollover the recorded stamp names, or
    the early limit reset the stamp would hide. The result is one of
    :data:`END_CLEAR`, :data:`END_CEILING`, :data:`END_AT` or :data:`END_RESET`.

    An end already recorded on the lift is returned ahead of every reading, so a
    reading that flips between two figures cannot revive the lift under the same
    id once its end has been observed.

    A lift that has not yet started, or that carries no grant, is not in force
    either, but neither is an end: both answer ``None`` here, and the caller
    judging a start or a grant reads those fields directly. :func:`_in_force`
    and :func:`effective_budget` are bound to this one ladder, so a reader of the
    pace path and a reader of the watch stream cannot disagree about why a lift
    stopped — whether it is still in force, or what ended it.
    """
    moment = _aware(now)
    if lift.get("ended_at"):
        cause = str(lift.get("ended_cause") or "")
        return cause if cause in END_CAUSES else END_RESET
    if lift.get("cleared_at"):
        return END_CLEAR
    granted = _parse_stamp(lift.get("granted_at"))
    if granted is not None and not moment < granted + timedelta(hours=bound.max_hours):
        return END_CEILING
    ends = lift.get("ends") or {}
    kind = str(ends.get("kind") or "")
    if kind == "at":
        at = _parse_stamp(ends.get("at"))
        if at is None or not moment < at:
            return END_AT
    if kind == "reset":
        if _reset_observed(lift, readings):
            return END_RESET
        recorded = _parse_stamp(ends.get("resets_at"))
        if recorded is not None and not moment < recorded:
            return END_RESET
    return None


def _in_force(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> bool:
    """Whether a lift is in force at ``now``.

    A lift that has not started, or that carries no grant, is inert but not
    ended, and neither condition is one of the four :func:`end_cause` names, so
    both are read here. Every other condition is read through that one ladder,
    which the pace path and the watch stream share.
    """
    moment = _aware(now)
    starts = _parse_stamp(lift.get("starts_at"))
    if starts is not None and moment < starts:
        return False
    if _parse_stamp(lift.get("granted_at")) is None:
        return False
    return end_cause(lift, readings=readings, now=moment, bound=bound) is None


def _end_moment(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
) -> tuple[datetime | None, str]:
    """The lift's end instant and what ends it."""
    ends = lift.get("ends") or {}
    kind = str(ends.get("kind") or "")
    if kind == "at":
        return _parse_stamp(ends.get("at")), "time"
    if kind == "reset":
        return _parse_stamp(ends.get("resets_at")), "reset"
    return None, "unknown"


def _reset_observed(
    lift: Mapping[str, Any],
    readings: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Whether the clock named by a reset-anchored lift has rolled over.

    Three signals, any one decisive: the newest reading's ``resets_at`` has
    moved past the stamp the lift recorded by at least half the clock's window,
    the span a genuine rollover moves a window's end, so a stamp that drifts by
    well over a minute while the window stays where it is is not read as a
    rollover; the newest reading's utilisation stands below the figure recorded
    at grant, which catches an early reset that left the stamp in place; and a
    fall between two consecutive readings that both stand above the grant
    figure, which catches a reset the newest reading alone would miss.
    """
    ends = lift.get("ends") or {}
    if str(ends.get("kind") or "") != "reset":
        return False
    clock = _lift_clock(lift)
    series = _clock_series(readings, clock)
    if not series:
        return False
    newest_utilisation, newest_reset = series[-1]
    recorded_reset = _parse_stamp(ends.get("resets_at"))
    if _stamp_moved_past_half_window(recorded_reset, newest_reset, clock=clock):
        return True
    grant_figure = ends.get("utilisation")
    if not _is_number(grant_figure):
        return False
    floor = float(grant_figure)
    if newest_utilisation is not None and newest_utilisation < floor:
        return True
    for earlier, later in pairwise(series):
        before, after = earlier[0], later[0]
        if before is None or after is None:
            continue
        if before > floor and after > floor and after < before:
            return True
    return False


def _stamp_moved_past_half_window(
    recorded: datetime | None,
    newest: datetime | None,
    *,
    clock: str,
) -> bool:
    """Whether ``newest`` has moved past ``recorded`` by at least half a window.

    The stamp signal a reset-anchored lift reads: a reset moves a window's end by
    about a whole window, so only a move of at least half the clock's own length
    is read as a rollover. The provider's stamps drift by well over a minute
    while the window stays where it is, and such a move is the same window, so it
    leaves the lift in force. Only a move *ahead* of the recorded stamp counts,
    since the next window's end is always later, so a stamp that drifts backwards
    leaves the lift in force just as a small forward drift does.
    """
    if recorded is None or newest is None:
        return False
    moved = (newest - recorded).total_seconds()
    half_window = CLOCK_HOURS.get(clock, 168.0) / 2.0 * 3600.0
    return moved >= half_window


# ── Reading helpers ─────────────────────────────────────────────────────────


def _newest_reading(
    readings: Sequence[Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    """The freshest reading, in observed order, or ``None`` when none is given."""
    if not readings:
        return None
    newest = readings[-1]
    return newest if isinstance(newest, Mapping) else None


def _clock_value(
    reading: Mapping[str, Any] | None,
    clock: str,
) -> Mapping[str, Any] | None:
    if not isinstance(reading, Mapping):
        return None
    value = reading.get(clock)
    return value if isinstance(value, Mapping) else None


def _clock_utilisation(
    reading: Mapping[str, Any] | None,
    clock: str,
) -> float | None:
    clock_value = _clock_value(reading, clock)
    if clock_value is None:
        return None
    raw = clock_value.get("utilisation")
    return float(raw) if _is_number(raw) else None


def _clock_reset(
    reading: Mapping[str, Any] | None,
    clock: str,
) -> datetime | None:
    clock_value = _clock_value(reading, clock)
    if clock_value is None:
        return None
    return _parse_stamp(clock_value.get("resets_at"))


def _clock_series(
    readings: Sequence[Mapping[str, Any]] | None,
    clock: str,
) -> list[tuple[float | None, datetime | None]]:
    """Each reading's ``(utilisation, resets_at)`` for one clock, in order."""
    series: list[tuple[float | None, datetime | None]] = []
    for reading in readings or ():
        if not isinstance(reading, Mapping):
            continue
        series.append(
            (_clock_utilisation(reading, clock), _clock_reset(reading, clock))
        )
    return series


def _placement(
    reading: Mapping[str, Any] | None,
    clock: str,
    now: datetime,
) -> tuple[float | None, datetime | None, datetime | None]:
    """Return ``(u0, t0, week_start)`` for the newest reading on ``clock``."""
    utilisation = _clock_utilisation(reading, clock)
    reset = _clock_reset(reading, clock)
    observed = (
        _parse_stamp(reading.get("observed_at"))
        if isinstance(reading, Mapping)
        else None
    )
    t0 = observed or now
    week_start = None
    if reset is not None:
        week_start = reset - timedelta(hours=CLOCK_HOURS.get(clock, 168.0))
    return utilisation, t0, week_start


def _clamp_target(
    target: datetime,
    reading: Mapping[str, Any] | None,
    clock: str,
) -> datetime:
    """Clamp a drain-by target to the clock's reset, since a window ends there."""
    reset = _clock_reset(reading, clock)
    if reset is not None and target > reset:
        return reset
    return target


def _projection(
    reading: Mapping[str, Any] | None,
    clock: str,
    multiple: float | None,
) -> str | None:
    """The instant the clock's window is spent at this multiple, from its start.

    At a burn of ``m`` times the linear rate the window fills when its elapsed
    fraction reaches ``clock_hours / m`` into it, so the projected exhaustion is
    the window's own start plus that span. It is recorded on the lift for
    tuning, and a projection that precedes the reset is the pattern a lift
    targets rather than a reason to refuse.
    """
    if multiple is None or multiple <= 0:
        return None
    reset = _clock_reset(reading, clock)
    if reset is None:
        return None
    hours = CLOCK_HOURS.get(clock, 168.0)
    return _iso(reset - timedelta(hours=hours) + timedelta(hours=hours / multiple))


def _end_block(
    *,
    ends: Mapping[str, Any] | None,
    form: str,
    clock: str,
    readings: Sequence[Mapping[str, Any]] | None,
    target: str | None,
) -> dict[str, Any]:
    """Build the lift's ``ends`` block from the caller's intent and the reading."""
    if isinstance(ends, Mapping) and ends.get("kind") == "at":
        moment = _parse_stamp(ends.get("at"))
        if moment is not None:
            return {"kind": "at", "at": _iso(moment)}
    if form == DRAIN_BY and target is not None:
        moment = _parse_stamp(target)
        if moment is not None:
            return {"kind": "at", "at": _iso(moment)}
    selected_clock = clock
    if isinstance(ends, Mapping) and ends.get("kind") == "reset":
        selected_clock = str(ends.get("clock") or clock)
    newest = _newest_reading(readings)
    reset = _parse_stamp(ends.get("resets_at")) if isinstance(ends, Mapping) else None
    if reset is None:
        reset = _clock_reset(newest, selected_clock)
    utilisation = ends.get("utilisation") if isinstance(ends, Mapping) else None
    if utilisation is None:
        utilisation = _clock_utilisation(newest, selected_clock)
    return {
        "kind": "reset",
        "clock": selected_clock,
        "resets_at": _iso(reset) if reset is not None else None,
        "utilisation": utilisation,
    }


# ── Small helpers ───────────────────────────────────────────────────────────


def _declared_group_names(config: Mapping[str, Any] | None) -> set[str]:
    """The declared budget group names, read from the group module."""
    budget_group = import_module("reckon.crew.budget_group")
    return set(budget_group.declared_groups(config))


def _declared_group_members(
    config: Mapping[str, Any] | None, group: str
) -> tuple[str, ...]:
    """The backend members of one declared budget group, in declaration order."""
    budget_group = import_module("reckon.crew.budget_group")
    return tuple(budget_group.declared_groups(config).get(str(group), ()))


def _scope(scope: str, session: str | None) -> str:
    """Resolve a lift's scope, requiring a session for a session-scoped lift."""
    value = str(scope or GLOBAL)
    if value == GLOBAL:
        return GLOBAL
    if value.rstrip(":") == SESSION_PREFIX.rstrip(":"):
        if not session:
            raise LiftRefusedError("a session-scoped lift names --session <id>")
        return f"{SESSION_PREFIX}{session}"
    if value.startswith(SESSION_PREFIX):
        if not value[len(SESSION_PREFIX) :]:
            raise LiftRefusedError("a session-scoped lift names --session <id>")
        return value
    raise LiftRefusedError(f"unknown lift scope {scope!r} (global or session:<id>)")


def _new_id(group: str, granted_at: datetime) -> str:
    return f"{group}-{int(granted_at.timestamp())}-{uuid.uuid4().hex[:6]}"


def _finite(value: Any, default: float) -> float:
    if not _is_number(value):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_stamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp or an aware datetime, else ``None``."""
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return parse_utc(text)
    except (TypeError, ValueError):
        return None


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _iso(moment: datetime) -> str:
    return _aware(moment).isoformat()
