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

**A reset is detected two ways, and either ends a reset-anchored lift.** The
provider may roll the window on schedule, or a limit reset may be applied early
and leave ``resets_at`` where it was. So a lift records the clock's utilisation
at grant and treats a current reading below that figure as a reset, which
catches the common case without history; and it reads the readings taken since
it was granted, treating a fall between two consecutive readings as a reset,
which catches the early one a stale stamp would hide.

**Resolution happens one step before the pace policy.** A pace policy sees only
a configuration and has no group, reading or clock, so it cannot itself tell
whether a lift is in force. :func:`effective_budget` takes the group, its
readings, the wall clock and the dispatching session, and returns the budget
block with the lifted multiple, the drain-by hold or the uncapped release — or
``config["budget"]`` unchanged when no lift covers the group.

The record lives in one control document per workstation,
``<config-home>/budget-lifts.json``, written atomically under a monotonically
increasing ``version``, in the same shape as the router's gate document.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from typing import Any

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
FORMS = (MULTIPLE, DRAIN_BY, UNCAPPED)

GLOBAL = "global"
SESSION_PREFIX = "session:"

DEFAULT_MAX_MULTIPLE = 3.0
DEFAULT_MAX_HOURS = 168.0

RUN_ID_ENV = "RECKON_RUN_ID"

__all__ = [
    "CLOCKS",
    "DEFAULT_MAX_HOURS",
    "DEFAULT_MAX_MULTIPLE",
    "DRAIN_BY",
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
    "grant",
    "lifts_path",
    "list_lifts",
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
        "granted_by": str(granted_by or environment.get("USER") or "unknown"),
        "granted_at": _iso(granted_at),
        "reason": str(reason).strip(),
        "projected_exhaustion": _projection(newest, selected_clock, resolved_multiple),
        "cleared_by": None,
        "cleared_at": None,
    }
    lift.update(drain_fields)

    document = read_document(path)
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
    record, whatever its scope, so a session-scoped lift can be cleared by the
    coordinator that sees it in the listing. Otherwise the lift is resolved for
    ``session``: a session's own lift governs it over a global one, and with no
    session given only a global lift is a candidate, so a bare clear never
    revokes a session-scoped lift out from under the session that holds it.

    Ended lifts are kept as history so the multiple can be tuned against what it
    did; a clear stamps the record rather than removing it. ``None`` is returned
    when no in-force lift matched, so a caller can tell a revoke from a no-op.
    """
    environment = environ if environ is not None else os.environ
    moment = _aware(now) if now is not None else datetime.now(UTC)
    document = read_document(path)
    if lift_id is not None:
        governing = _lift_by_id(document["lifts"], lift_id=str(lift_id))
    else:
        governing = _governing_lift(
            document["lifts"],
            group=str(group),
            readings=None,
            now=moment,
            session=session,
            bound=ceilings(config),
        )
    if governing is None or governing.get("cleared_at"):
        return None
    governing["cleared_by"] = str(cleared_by or environment.get("USER") or "unknown")
    governing["cleared_at"] = _iso(moment)
    _write_document(document, path=path)
    return governing


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


def _in_force(
    lift: Mapping[str, Any],
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    bound: LiftCeilings,
) -> bool:
    """Whether every end condition still holds for a lift at ``now``.

    A lift is in force only when it is uncleared, has started, is before the
    hard ceiling, is before its stated end, and — for a reset-anchored lift —
    has not had its clock's reset observed.
    """
    if lift.get("cleared_at"):
        return False
    starts = _parse_stamp(lift.get("starts_at"))
    if starts is not None and now < starts:
        return False
    granted = _parse_stamp(lift.get("granted_at"))
    if granted is None:
        return False
    if not now < granted + timedelta(hours=bound.max_hours):
        return False
    end, _ = _end_moment(lift, readings=readings)
    if end is not None and not now < end:
        return False
    ends = lift.get("ends") or {}
    reset_kind = str(ends.get("kind") or "") == "reset"
    return not (reset_kind and _reset_observed(lift, readings))


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

    Three signals, any one decisive: the newest reading's ``resets_at`` no
    longer matches the stamp the lift recorded; the newest reading's utilisation
    stands below the figure recorded at grant, which catches an early reset that
    left the stamp in place; and a fall between two consecutive readings that
    both stand above the grant figure, which catches a reset the newest reading
    alone would miss.
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
    if (
        recorded_reset is not None
        and newest_reset is not None
        and newest_reset != recorded_reset
    ):
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
