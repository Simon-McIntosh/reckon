"""One UTC timestamp parser shared by every module that reads a timber.

A timestamp crosses this repository in several shapes: an ISO-8601 string
with a ``Z`` suffix or a numeric offset, the same string without a zone, and
a numeric epoch in either seconds or milliseconds. Callers have each resolved
those shapes slightly differently, so the parsing rule lives here once and the
callers migrate to it.

The policy this module fixes, and the reasons it fixes each part:

* An explicit zone — ``Z`` or a numeric offset — is authoritative. The result
  is normalised to UTC, so a caller comparing two moments never has to know
  which zone each arrived in.
* A value carrying no zone is read as UTC. That is the rule the existing
  callers already assume: every stamp written by this repository is UTC, and
  the alternative — reading a naive value as local time — makes the result
  depend on the machine that happened to run the code.
* A number is an epoch. Magnitude tells seconds from milliseconds, split at
  ``_MS_THRESHOLD`` (``1e11``): a count at or above it is milliseconds, below
  it is seconds. The threshold is a decade above any plausible epoch seconds
  (1e11 seconds is the year 5138) and a decade below any plausible epoch
  milliseconds, so it separates the two without reading a clock.
* Malformed input returns ``None`` rather than raising. A caller that cannot
  parse a stamp must decide for itself whether the missing moment is a refusal
  or a neutral absence, and it cannot do that from an exception raised several
  frames down.
"""

from __future__ import annotations

from datetime import UTC, datetime

_MS_THRESHOLD = 1e11


def parse_utc(value: object) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime, or None when malformed.

    Accepts an ISO-8601 string (a ``Z`` suffix, a numeric offset, or neither),
    or a numeric epoch in seconds or milliseconds. A numeric epoch below
    ``1e11`` is read as seconds, at or above it as milliseconds. A value of any
    other type — including ``None`` and ``bool`` — is malformed and returns
    ``None``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        return _from_iso8601(value)
    return None


def _from_epoch(value: float) -> datetime | None:
    seconds = value / 1000.0 if abs(value) >= _MS_THRESHOLD else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _from_iso8601(text: str) -> datetime | None:
    candidate = text.strip()
    if not candidate:
        return None
    if candidate[-1] in ("Z", "z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
