"""Track a banked budget-group reset that the provider is holding in reserve.

A metered subscription can carry one banked reset: a reset the account has not
yet reached that will grant a whole extra window of allowance when it arrives.
While it is banked the group has one extra full window of budget on top of what
remains in the current one, so the pace derivation that judges a wave should
count it rather than hold work the account can afford.

Every reset the group reaches is also an event in that record: an announcement
writes one through :func:`mark_used` whether or not a flag is there to clear,
and a reset the readings detect writes one through :func:`observe`. The events
are what a pane, a sweep or a listing reads, so the same news relayed by two
sessions is one row rather than two.

The flag is durable and per group, under the crew state home, written through
the store's atomic writer so a reader never sees a partial record. It is set and
cleared from the command surface, and it is also cleared automatically when the
readings show it has been consumed: a window whose reset boundary jumps forward
to about one window length ahead of the observation while the previously
observed boundary had not yet arrived is a reset that was spent early, which is
how a banked reset is used. A window that reaches its scheduled boundary is a
natural reset and leaves the flag alone.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon._store import write_json_atomically
from reckon.crew.obligation_snapshot import crew_home

#: The filename under the crew state home holding every group's flag.
STORE_FILENAME = "budget-resets.json"

#: Serialises this module's in-process critical sections so a nested call from
#: the same thread does not deadlock on the file lock below.
_PROCESS_LOCK = threading.RLock()

#: How far a jumped reset boundary may sit from ``observed_at + window`` and
#: still read as a fresh window. A reset consumed between two observations is
#: located by the second observation, so its boundary is at most the gap between
#: them behind the observation plus one window; a tenth of the window absorbs
#: that gap without admitting a boundary that is merely drifting.
RESET_JUMP_TOLERANCE_FRACTION = 0.1

#: How close two global announcements of a reset for one group and clock may sit
#: and still be the same news. Several sessions relay one reset, so a repeat
#: inside this window reports the event already on record instead of adding a
#: second one.
DUPLICATE_WINDOW = timedelta(minutes=10)

#: How far back :func:`recent_resets` reaches when the caller names no horizon.
DEFAULT_HORIZON = timedelta(hours=24)

__all__ = [
    "DEFAULT_HORIZON",
    "DUPLICATE_WINDOW",
    "RESET_JUMP_TOLERANCE_FRACTION",
    "STORE_FILENAME",
    "available",
    "mark_available",
    "mark_used",
    "observe",
    "recent_resets",
    "record",
    "state_path",
]


def state_path(*, home: Path | None = None) -> Path:
    """Return the durable flag file, under the crew state home by default."""
    return (home or crew_home()) / STORE_FILENAME


def _now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _locked(*, home: Path | None = None) -> Iterator[None]:
    """Serialise a read-check-write of the flag store for one crew home.

    Every writer — the command surface and the automatic detection — takes this
    lock before it reads the record it is about to decide on, so a read and the
    write that follows it are one step. Without it an observe that read the flag
    before a concurrent clear would write back the value it read, and a clear
    that had already landed would be lost.
    """
    path = state_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _PROCESS_LOCK, lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event_id(group: str, moment: datetime) -> str:
    """The stable id one reset event carries.

    A pane's claim, a sweep's memory and a listing all key on this, so it is
    derived from the group and the instant the event belongs to rather than from
    anything that moves between readers.
    """
    return f"reset-{group}-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _clock_key(clock: Any) -> str:
    """The clock an event is filed under, with the surface's own default."""
    text = str(clock or "").strip()
    return text or "seven_day"


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _load(*, home: Path | None = None) -> dict[str, Any]:
    path = state_path(home=home)
    if not path.exists():
        return {"groups": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"groups": {}}
    if not isinstance(data, dict):
        return {"groups": {}}
    if not isinstance(data.get("groups"), dict):
        data["groups"] = {}
    return data


def _write(data: Mapping[str, Any], *, home: Path | None = None) -> None:
    write_json_atomically(state_path(home=home), dict(data))


def record(group: str, *, home: Path | None = None) -> dict[str, Any] | None:
    """Return one group's durable record, or ``None`` when it has never been set."""
    found = _load(home=home)["groups"].get(group)
    return found if isinstance(found, dict) else None


def available(group: str, *, home: Path | None = None) -> bool:
    """Whether a banked reset is currently available for ``group``."""
    found = record(group, home=home)
    return bool(found and found.get("available"))


def _who() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def mark_available(
    group: str,
    *,
    by: str | None = None,
    moment: datetime | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Flag one banked reset as available; a second call changes nothing.

    A banked reset never stacks: a group carries at most one, so flagging one
    that is already available reports the existing record rather than advancing
    it. The caller can tell the two apart from ``changed``.
    """
    with _locked(home=home):
        data = _load(home=home)
        groups = data["groups"]
        now = moment or _now()
        found = groups.get(group)
        found = found if isinstance(found, dict) else {}
        if found.get("available"):
            return {
                "ok": True,
                "group": group,
                "available": True,
                "changed": False,
                "set_at": found.get("set_at"),
                "set_by": found.get("set_by"),
                "detail": (
                    "a banked reset is already available for this group; it does not "
                    "stack, so the existing record stands"
                ),
            }
        found = {
            **found,
            "available": True,
            "set_at": _iso(now),
            "set_by": (by or _who()),
            "cleared_at": None,
            "cleared_reason": None,
        }
        found.setdefault("events", [])
        groups[group] = found
        _write(data, home=home)
        return {
            "ok": True,
            "group": group,
            "available": True,
            "changed": True,
            "set_at": found["set_at"],
            "set_by": found["set_by"],
            "detail": "a banked reset is now flagged available for this group",
        }


def mark_used(
    group: str,
    *,
    by: str | None = None,
    moment: datetime | None = None,
    reason: str | None = None,
    clock: str | None = None,
    session: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Record one used reset for ``group``, and clear its flag when that applies.

    The event is written whether or not a banked flag is there to clear, so a
    reset announced by the lead is a durable record even when nothing was being
    held: that record is what a pane draws and what a sweep announces. A global
    event — one naming no session — is the account's own reset arriving, so it
    clears an available flag; a session-scoped event is a rehearsal that reaches
    one session and leaves the flag alone.

    Clearing a group that carries no flag is still the common case, and the flag
    is reported unchanged there, with ``changed`` describing the flag rather than
    the record.

    Several sessions relay the same reset, so a global event for a group and
    clock that already has a global event inside :data:`DUPLICATE_WINDOW`
    returns the event on record and writes nothing.
    """
    with _locked(home=home):
        data = _load(home=home)
        groups = data["groups"]
        now = moment or _now()
        found = groups.get(group)
        found = found if isinstance(found, dict) else {}
        events = found.get("events")
        events = events if isinstance(events, list) else []
        said = (reason or "").strip() or "marked used"
        who = by or _who()
        if session is None:
            twin = _recent_twin(events, moment=now, clock=clock)
            if twin is not None:
                return {
                    "ok": True,
                    "group": group,
                    "available": bool(found.get("available")),
                    "changed": False,
                    "recorded": False,
                    "id": twin.get("id"),
                    "event": {**twin, "group": group},
                    "detail": (
                        f"a global reset for this group and clock is already on "
                        f"record at {twin.get('detected_at')}; that event stands"
                    ),
                }
        event = {
            "kind": "used",
            "id": _event_id(group, now),
            "detected_at": _iso(now),
            "clock": clock,
            "reason": said,
            "by": who,
        }
        if session:
            event["session"] = session
        events.append(event)
        found["events"] = events
        cleared = bool(found.get("available")) and session is None
        if cleared:
            found["available"] = False
            found["cleared_at"] = _iso(now)
            found["cleared_reason"] = said
            found["cleared_by"] = who
        groups[group] = found
        _write(data, home=home)
        if cleared:
            detail = "the banked-reset flag is cleared for this group"
        elif found.get("available"):
            detail = (
                "the banked-reset flag stands: a reset scoped to one session "
                "does not clear it, and the used event is recorded"
            )
        else:
            detail = (
                "no banked reset is available for this group, so nothing changed "
                "in the flag; the used event is recorded"
            )
        return {
            "ok": True,
            "group": group,
            "available": bool(found.get("available")),
            "changed": cleared,
            "recorded": True,
            "id": event["id"],
            "event": {**event, "group": group},
            "cleared_at": found.get("cleared_at") if cleared else None,
            "detail": detail,
        }


def _recent_twin(
    events: Iterable[Any],
    *,
    moment: datetime,
    clock: str | None,
) -> Mapping[str, Any] | None:
    """The global event a repeat announcement would duplicate, if one is close.

    Only global events count: a session-scoped event reaches one session, so an
    announcement for the account as a whole is not the same news and is always
    written.
    """
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("kind") != "used" or event.get("session"):
            continue
        if _clock_key(event.get("clock")) != _clock_key(clock):
            continue
        seen = _parse(event.get("detected_at"))
        if seen is None:
            continue
        if abs(moment - seen) <= DUPLICATE_WINDOW:
            return event
    return None


def observe(
    group: str,
    *,
    resets_at: Any,
    window_minutes: int | None,
    moment: datetime | None = None,
    utilisation: float | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Record one reading of a group's window boundary and detect a consumed reset.

    A group with no record is left alone: nothing is being tracked for it, so
    there is nothing to compare and nothing to write. Once a record exists, the
    boundary it last saw is compared against this reading. A boundary that moved
    forward to about one window length ahead of this observation, while the
    boundary last seen had not yet arrived, is a reset consumed ahead of its
    schedule: the flag is cleared and the event recorded with both readings. A
    boundary that is only reached at its scheduled time leaves the flag alone,
    because the previous boundary is behind the observation by then.

    The read, the comparison and the write happen under one lock, the same one
    the command surface takes, so a clear that lands concurrently is never
    overwritten by a reading taken before it. The record is written only when
    there is something new to keep -- a first reading, a moved boundary, or a
    reset consumed -- so a derivation over an unchanged reading costs no write.
    """
    with _locked(home=home):
        data = _load(home=home)
        groups = data["groups"]
        found = groups.get(group)
        if not isinstance(found, dict):
            return {
                "tracked": False,
                "available": False,
                "consumed": False,
                "changed": False,
            }
        now = moment or _now()
        boundary = resets_at if isinstance(resets_at, str) else None
        current = _parse(resets_at)
        previous = _parse(found.get("observed_resets_at"))
        consumed = False
        if found.get("available") and current is not None and previous is not None:
            window = window_minutes or found.get("observed_window_minutes")
            if (
                isinstance(window, int)
                and window > 0
                and previous > now
                and current > previous
            ):
                tolerance = timedelta(minutes=window * RESET_JUMP_TOLERANCE_FRACTION)
                expected = now + timedelta(minutes=window)
                if abs(current - expected) <= tolerance:
                    consumed = True
        if not consumed and _same_observation(
            found, boundary, window_minutes, utilisation
        ):
            return {
                "tracked": True,
                "available": bool(found.get("available")),
                "consumed": False,
                "changed": False,
            }
        if consumed:
            found["available"] = False
            found["cleared_at"] = _iso(now)
            found["cleared_by"] = "detected"
            found["cleared_reason"] = "consumed reset detected"
            found.setdefault("events", []).append(
                {
                    "kind": "consumed",
                    "id": _event_id(group, now),
                    "detected_at": _iso(now),
                    "previous_resets_at": found.get("observed_resets_at"),
                    "resets_at": boundary,
                    "previous_observed_at": found.get("observed_at"),
                    "observed_at": _iso(now),
                    "window_minutes": window_minutes,
                    "utilisation": utilisation,
                }
            )
        found["observed_resets_at"] = boundary
        found["observed_window_minutes"] = window_minutes
        found["observed_at"] = _iso(now)
        if utilisation is not None:
            found["observed_utilisation"] = utilisation
        groups[group] = found
        _write(data, home=home)
        return {
            "tracked": True,
            "available": bool(found.get("available")),
            "consumed": consumed,
            "changed": True,
        }


def _same_observation(
    found: Mapping[str, Any],
    boundary: str | None,
    window_minutes: int | None,
    utilisation: float | None,
) -> bool:
    """Whether ``found`` already carries exactly this observation."""
    if found.get("observed_resets_at") != boundary:
        return False
    if found.get("observed_window_minutes") != window_minutes:
        return False
    return utilisation is None or found.get("observed_utilisation") == utilisation


def _horizon(value: timedelta | float | None) -> timedelta:
    """Read the caller's horizon as a span, defaulting to a day."""
    if value is None:
        return DEFAULT_HORIZON
    if isinstance(value, timedelta):
        return value
    return timedelta(seconds=float(value))


def recent_resets(
    session: str | None = None,
    *,
    now: datetime | None = None,
    horizon: timedelta | float | None = None,
    home: Path | None = None,
) -> list[dict[str, Any]]:
    """Return this crew home's recent reset events across every group, newest first.

    Both kinds come back together — a ``used`` event written by an announcement
    and a ``consumed`` event the window readings detected — because a reader
    announcing or listing resets cares that one happened, not how it was found.
    Each row carries its group beside the event's own fields, including the id a
    pane's claim is keyed on.

    ``session`` selects what applies to one session: every global event, and the
    events scoped to that session. Naming no session returns every event, which
    is what a sweep needs to see the session-scoped ones it must announce.

    ``horizon`` bounds how far back the events reach; an event whose stated
    instant is still ahead is always returned, because a reset the lead states a
    time for becomes due with nothing else moving.
    """
    moment = now or _now()
    floor = moment - _horizon(horizon)
    rows: list[dict[str, Any]] = []
    for group, found in _load(home=home)["groups"].items():
        if not isinstance(found, Mapping):
            continue
        events = found.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            stamp = _parse(event.get("detected_at"))
            if stamp is None or stamp < floor:
                continue
            owner = event.get("session")
            if session is not None and owner and owner != session:
                continue
            row = {**event, "group": group}
            if not row.get("id"):
                row["id"] = _event_id(group, stamp)
            rows.append(row)
    rows.sort(key=lambda row: (_parse(row["detected_at"]), str(row.get("id"))))
    rows.reverse()
    return rows
