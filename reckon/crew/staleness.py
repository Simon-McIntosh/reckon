"""A quota reading older than its configured shelf life is re-queried, not reported.

A lane's newest receipt is the only figure a caller can usually see, and that
receipt is minutes to hours old. A figure that old is not a position: the
five-hour clock it measures rolls over several times inside the interval, so
reporting it is reporting a number about a window that has already closed. So a
reading is only reported as a position while it is inside its configured shelf
life. Past that, the lane's own probe is asked again and the fresh figure is
what the caller reports.

A re-query that cannot answer does not erase the reading and does not invent
one. The old figure is returned with the age that disqualifies it and a serving
state of ``unknown``, so a caller cannot mistake it and cannot read a failed
probe as either full or empty -- absence of a signal is not exhaustion and is
equally not headroom.

The shelf life is a flight key, shared with the budget fence so the two never
disagree about when evidence stops speaking for now.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from reckon import budget, flight

SERVING_STATE_UNKNOWN = "unknown"

Probe = Callable[[], "Reading | None"]


@dataclass(frozen=True, slots=True)
class Reading:
    """One quota figure with the moment it was observed.

    ``observed_at`` is the instant the figure describes, and it is what makes
    the figure a position rather than a snapshot. A reading whose observation
    time is missing cannot be shown to be inside any shelf life, so it is not
    trusted as current and is re-queried like a stale one.
    """

    used_percent: float | None
    observed_at: datetime | None
    source: str = "receipt"
    serving_state: str = SERVING_STATE_UNKNOWN


@dataclass(frozen=True, slots=True)
class ReportedReading:
    """The reading a caller reports, with the age and provenance that qualify it.

    ``requeried`` records whether the lane's probe was asked for a fresh figure,
    so a caller can tell a reading that was trusted from one that was re-read.
    ``serving_state`` is ``unknown`` whenever no trusted figure stands behind the
    row -- notably when a re-query was attempted and did not answer.
    """

    used_percent: float | None
    observed_at: datetime | None
    age_seconds: float | None
    serving_state: str
    source: str
    requeried: bool


def _age_seconds(observed_at: datetime | None, now: datetime) -> float | None:
    """Seconds between the observation and now, or ``None`` when undated."""
    if observed_at is None:
        return None
    return max(0.0, (now - observed_at).total_seconds())


def configured_shelf_life_minutes(config: Mapping[str, Any] | None = None) -> float:
    """Return the configured quota-reading shelf life in minutes.

    Read from the flight budget block so a host can widen or narrow the horizon
    without a code change. Passing ``config`` keeps a caller's already-resolved
    layer set; without it the four flight layers are resolved here.
    """
    resolved = config if config is not None else flight.resolve().config
    return float(budget.policy(resolved)["evidence_shelf_life_minutes"])


def _asked_probe(probe: Probe) -> Reading | None:
    """Ask the probe for a fresh reading, or ``None`` when it cannot answer.

    A probe that raises, or that answers with no figure, has not answered: both
    are the failure the caller must report as ``unknown`` rather than as a
    measurement.
    """
    try:
        answer = probe()
    except Exception:  # noqa: BLE001 - a failed probe must not raise through the reader
        return None
    if answer is None or answer.used_percent is None:
        return None
    return answer


def resolve_reading(
    reading: Reading,
    *,
    probe: Probe,
    shelf_life: timedelta,
    now: datetime | None = None,
) -> ReportedReading:
    """Resolve one lane reading to the figure a caller may report as a position.

    A reading observed inside its shelf life is returned without touching the
    probe, so the re-query is not unconditional and a live figure is never
    disturbed. A reading past its shelf life -- or one with no observation time
    to age it -- is re-queried; the probe's figure is reported on success, and
    the old figure with its age and ``unknown`` on failure.
    """
    moment = now or datetime.now(UTC)
    age = _age_seconds(reading.observed_at, moment)
    if age is not None and age <= shelf_life.total_seconds():
        return ReportedReading(
            used_percent=reading.used_percent,
            observed_at=reading.observed_at,
            age_seconds=age,
            serving_state=reading.serving_state,
            source=reading.source,
            requeried=False,
        )

    fresh = _asked_probe(probe)
    if fresh is not None:
        return ReportedReading(
            used_percent=fresh.used_percent,
            observed_at=fresh.observed_at,
            age_seconds=_age_seconds(fresh.observed_at, moment),
            serving_state=fresh.serving_state,
            source=fresh.source,
            requeried=True,
        )

    return ReportedReading(
        used_percent=reading.used_percent,
        observed_at=reading.observed_at,
        age_seconds=age,
        serving_state=SERVING_STATE_UNKNOWN,
        source=reading.source,
        requeried=True,
    )


def resolve_configured_reading(
    reading: Reading,
    *,
    probe: Probe,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ReportedReading:
    """Resolve a reading against the shelf life the flight configuration declares."""
    shelf_life = timedelta(minutes=configured_shelf_life_minutes(config))
    return resolve_reading(reading, probe=probe, shelf_life=shelf_life, now=now)
