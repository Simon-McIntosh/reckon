"""Track a banked budget-group reset that the provider is holding in reserve.

A metered subscription can carry one banked reset: a reset the account has not
yet reached that will grant a whole extra window of allowance when it arrives.
While it is banked the group has one extra full window of budget on top of what
remains in the current one, so the pace derivation that judges a wave should
count it rather than hold work the account can afford.

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
from collections.abc import Iterator, Mapping
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

__all__ = [
    "RESET_JUMP_TOLERANCE_FRACTION",
    "STORE_FILENAME",
    "available",
    "mark_available",
    "mark_used",
    "observe",
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
    reason: str = "marked used",
    home: Path | None = None,
) -> dict[str, Any]:
    """Clear one group's banked-reset flag, recording the event.

    Clearing a group that carries no flag changes nothing and says so, so the
    command is safe to repeat.
    """
    with _locked(home=home):
        data = _load(home=home)
        groups = data["groups"]
        now = moment or _now()
        found = groups.get(group)
        found = found if isinstance(found, dict) else {}
        if not found.get("available"):
            return {
                "ok": True,
                "group": group,
                "available": False,
                "changed": False,
                "detail": "no banked reset is available for this group, so nothing changed",
            }
        found = {
            **found,
            "available": False,
            "cleared_at": _iso(now),
            "cleared_reason": reason,
            "cleared_by": (by or _who()),
        }
        found.setdefault("events", []).append(
            {
                "kind": "used",
                "detected_at": _iso(now),
                "reason": reason,
                "by": (by or _who()),
            }
        )
        groups[group] = found
        _write(data, home=home)
        return {
            "ok": True,
            "group": group,
            "available": False,
            "changed": True,
            "cleared_at": found["cleared_at"],
            "detail": "the banked-reset flag is cleared for this group",
        }


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
