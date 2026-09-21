"""Read the two metered windows a served stream actually reports.

The provider publishes its own pace on the stream of every served run that
carries a rate-limit event: ``unifiedWindows.five_hour`` and
``unifiedWindows.seven_day``, each with a ``utilization`` and a ``resetsAt``.
Reckon's budget fence consults a single ceiling instead, so a wave may spend
most of a five-hour window in one hour and the fence stays silent until the
window is nearly gone. This module is the reader that fence needs.

**Most streams do not carry the windows, and that shapes the reader.** Over a
400-stream sample on this workstation, 54 carried ``unifiedWindows`` and 346
did not; the unmetered local lane carried it on none of its 28, correctly, as
it is not metered. So a reading means *the newest event that carries a window*,
never merely the newest event, and a figure that comes back is normally minutes
to hours old rather than current. Three consequences are load-bearing here:

* The newest window-carrying event is the only one read; newer events that
  carry no window are skipped rather than allowed to erase it.
* Every figure is about what a served run *reported*, which the run's stream
  records. The account surface is deliberately not consulted: it fails with the
  binary absent from the server's PATH on one side and with a 403 on the other.
* A figure travels with the observation time it came from, because a window
  utilisation with no observation time cannot be told apart from a current one
  and the five-hour clock rolls over. Where no observation time can be
  established the reading is ``unknown`` rather than a figure.

Absence of a signal is not exhaustion, and it is equally not headroom, so an
``unknown`` reading is never reported as ``0.0`` or ``1.0``. The utilisation is
``None`` in that case, which cannot be confused with a real ``0.0`` the
provider actually reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _backends

#: The named periods, in the order a reader expects to see them. A period the
#: provider reports that is not named here is still read, and sorts after these.
PERIODS = ("five_hour", "seven_day")


@dataclass(frozen=True)
class WindowFigure:
    """One metered clock's position, together with when it was observed."""

    period: str
    utilisation: float
    observed_at: datetime
    age_seconds: float
    resets_at: str | None = None


@dataclass(frozen=True)
class WindowReading:
    """A stream's newest window-carrying report, or an explicit unknown.

    ``figures`` is empty exactly when nothing could be read, and the ``reason``
    then says why. An empty reading is not a position: no caller may substitute
    a default for it, which is why the accessor returns ``None`` rather than a
    number.
    """

    figures: tuple[WindowFigure, ...] = field(default_factory=tuple)
    observed_at: datetime | None = None
    age_seconds: float | None = None
    reason: str = ""

    @property
    def known(self) -> bool:
        """Whether any window figure was read at all."""
        return bool(self.figures)

    def figure(self, period: str) -> WindowFigure | None:
        """The figure for one period, or ``None`` when the reading is unknown."""
        return next((f for f in self.figures if f.period == period), None)

    def utilisation(self, period: str) -> float | None:
        """One period's fraction, or ``None`` — never a stand-in for unknown."""
        found = self.figure(period)
        return None if found is None else found.utilisation


def read_windows(source: object, *, now: datetime | None = None) -> WindowReading:
    """Read the newest metered window report out of a run's stream.

    ``source`` is either a stream path or an iterable of stream events (or of
    JSON lines). ``now`` anchors the observation age and defaults to the
    current moment; a caller that has already timestamped its own decision
    passes it so the age is measured against that moment rather than against a
    moment reached by a second, later read.
    """
    events, error = _events_from(source)
    if error:
        return WindowReading(reason=error)
    return _newest_reading(events, now=now)


def _newest_reading(
    events: Iterable[Mapping[str, Any]], *, now: datetime | None = None
) -> WindowReading:
    """Build the reading from the newest event that carries a window."""
    moment = now or datetime.now(tz=UTC)
    ordered = list(events)

    index = None
    for position, event in enumerate(ordered):
        if _carries_window(event):
            index = position
    if index is None:
        return WindowReading(reason="no stream event carried a unified window")

    observed_at = _observation_time(ordered, index)
    if observed_at is None:
        return WindowReading(
            reason="the newest window-carrying event carried no observation time"
        )

    figures = tuple(
        WindowFigure(
            period=period,
            utilisation=value,
            observed_at=observed_at,
            age_seconds=(moment - observed_at).total_seconds(),
            resets_at=_reset_text(window),
        )
        # The top-level `utilization` beside `rateLimitType` is deliberately
        # absent from this comprehension: only `unifiedWindows` holds a window.
        for period, window in _ordered_windows(_unified_windows(ordered[index]))
        if (value := _numeric(window.get("utilization"))) is not None
    )
    if not figures:
        return WindowReading(
            reason="the newest window-carrying event reported no usable period"
        )
    return WindowReading(
        figures=figures,
        observed_at=observed_at,
        age_seconds=(moment - observed_at).total_seconds(),
    )


def _events_from(source: object) -> tuple[list[dict[str, Any]], str]:
    """Materialise ``source`` into events, with a reason when it cannot be read."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            with path.open(encoding="utf-8") as lines:
                events, _malformed = _backends.parse_events(lines)
        except OSError:
            return [], f"the stream {path.name} could not be read"
        return events, ""

    try:
        items = list(source)  # type: ignore[call-overload]
    except TypeError:
        return [], f"the stream source {type(source).__name__} could not be read"
    if items and all(isinstance(item, str) for item in items):
        events, _malformed = _backends.parse_events(items)
        return events, ""
    return [event for event in items if isinstance(event, Mapping)], ""


def _carries_window(event: Mapping[str, Any]) -> bool:
    """Whether this event is a rate-limit report carrying a usable window."""
    return any(
        _numeric(window.get("utilization")) is not None
        for window in _unified_windows(event).values()
    )


def _unified_windows(event: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The unified window map a rate-limit event carries, or an empty map.

    Only ``unifiedWindows`` is read. The top-level ``utilization`` is the
    account's calendar position — for an overage record its reset lands on a
    month boundary — and it reads as a bare fraction that can exceed ``1``, so
    it can never be handed back as a window figure.
    """
    if event.get("type") != "rate_limit_event":
        return {}
    info = event.get("rate_limit_info")
    if not isinstance(info, Mapping):
        return {}
    windows = info.get("unifiedWindows")
    if not isinstance(windows, Mapping):
        return {}
    return {
        period: window
        for period, window in windows.items()
        if isinstance(window, Mapping)
    }


def _ordered_windows(
    windows: Mapping[str, Mapping[str, Any]],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Order the reported periods, best-known to the reader first."""

    def rank(period: str) -> tuple[int, str]:
        try:
            return (PERIODS.index(period), period)
        except ValueError:
            return (len(PERIODS), period)

    return sorted(windows.items(), key=lambda item: rank(item[0]))


def _observation_time(events: list[Mapping[str, Any]], index: int) -> datetime | None:
    """The observation time of an event that does not carry one itself.

    A rate-limit record carries no timestamp of its own, but its immutable
    stream position places it between records that do, so the closest
    surrounding stamp anchors the age. Where the two are equally near the
    following record wins: ageing a reading further than it truly is would
    cause a needless re-query, while ageing it less is the safe direction.
    """
    own = _parse_stamp(events[index].get("timestamp"))
    if own is not None:
        return own

    distance = 1
    while index - distance >= 0 or index + distance < len(events):
        if index + distance < len(events):
            ahead = _parse_stamp(events[index + distance].get("timestamp"))
            if ahead is not None:
                return ahead
        if index - distance >= 0:
            behind = _parse_stamp(events[index - distance].get("timestamp"))
            if behind is not None:
                return behind
        distance += 1
    return None


def _reset_text(window: Mapping[str, Any]) -> str | None:
    """A window's reset time as text, from an epoch second or an ISO string."""
    moment = window.get("resetsAt")
    if isinstance(moment, (int, float)) and not isinstance(moment, bool):
        return datetime.fromtimestamp(moment, tz=UTC).isoformat()
    return moment if isinstance(moment, str) else None


def _parse_stamp(value: Any) -> datetime | None:
    """A usable UTC instant from an event's timestamp, else ``None``.

    A stamp without a zone is read as UTC, because the streams are written in
    UTC and reading one as local would invert a statement about how old a
    reading is. A trailing ``Z`` is the form the streams actually carry, and
    ``fromisoformat`` reads it as UTC directly.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _numeric(value: Any) -> float | None:
    """A real number, never a ``bool``, which is an ``int`` subclass here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
