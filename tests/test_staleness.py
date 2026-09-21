"""A quota reading past its shelf life is re-queried rather than reported."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import flight
from reckon.crew import staleness

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SHELF_LIFE = timedelta(minutes=60)
STALE_AGE = timedelta(hours=2.4)


def _reading(
    used_percent: float = 13.0,
    age: timedelta = STALE_AGE,
    *,
    serving_state: str = "unknown",
    observed_at: datetime | None = None,
) -> staleness.Reading:
    moment = NOW - age if observed_at is None else observed_at
    return staleness.Reading(
        used_percent=used_percent,
        observed_at=moment,
        source="receipt",
        serving_state=serving_state,
    )


def _probe_returning(reading):
    calls: list[int] = []

    def probe():
        calls.append(1)
        return reading

    return probe, calls


def _probe_refusing(*, raises: bool):
    calls: list[int] = []

    def probe():
        calls.append(1)
        if raises:
            raise RuntimeError("probe transport refused")

    return probe, calls


def test_stale_reading_is_requeried_and_the_probe_figure_is_returned():
    fresh = staleness.Reading(
        used_percent=14.0,
        observed_at=NOW - timedelta(seconds=30),
        source="probe",
        serving_state="will_serve",
    )
    probe, calls = _probe_returning(fresh)

    reported = staleness.resolve_reading(
        _reading(used_percent=13.0), probe=probe, shelf_life=SHELF_LIFE, now=NOW
    )

    assert calls == [1], "the stale reading did not ask the probe"
    assert reported.used_percent == 14.0
    assert reported.source == "probe"
    assert reported.requeried is True


@pytest.mark.parametrize("raises", [True, False])
def test_failed_requery_returns_the_old_figure_with_its_age_and_unknown_state(raises):
    probe, calls = _probe_refusing(raises=raises)

    reported = staleness.resolve_reading(
        _reading(used_percent=13.0), probe=probe, shelf_life=SHELF_LIFE, now=NOW
    )

    assert len(calls) == 1
    # A failed re-query is neither a fresh-looking number nor nothing: the old
    # figure survives, carrying the age that disqualifies it.
    assert reported.used_percent == 13.0
    assert reported.age_seconds == pytest.approx(STALE_AGE.total_seconds(), abs=1.0)
    assert reported.serving_state == "unknown"
    assert reported.requeried is True


def test_probe_answering_without_a_figure_is_a_failed_requery():
    probe, _ = _probe_returning(staleness.Reading(used_percent=None, observed_at=NOW))

    reported = staleness.resolve_reading(
        _reading(used_percent=13.0), probe=probe, shelf_life=SHELF_LIFE, now=NOW
    )

    assert reported.used_percent == 13.0
    assert reported.serving_state == "unknown"


def test_reading_inside_its_shelf_life_does_not_requery():
    def forbidden_probe():
        raise AssertionError("a fresh reading must not trigger a re-query")

    reported = staleness.resolve_reading(
        _reading(
            used_percent=42.0,
            age=timedelta(minutes=5),
            serving_state="will_serve",
        ),
        probe=forbidden_probe,
        shelf_life=SHELF_LIFE,
        now=NOW,
    )

    assert reported.requeried is False
    assert reported.used_percent == 42.0
    assert reported.serving_state == "will_serve"


def test_undated_reading_is_requeried_and_freshness_is_never_assumed():
    fresh = staleness.Reading(used_percent=14.0, observed_at=NOW, source="probe")
    probe, calls = _probe_returning(fresh)
    undated = staleness.Reading(used_percent=13.0, observed_at=None)

    reported = staleness.resolve_reading(
        undated, probe=probe, shelf_life=SHELF_LIFE, now=NOW
    )

    assert calls == [1], "a reading with no observation time was trusted as current"
    assert reported.used_percent == 14.0


def test_undated_failed_requery_reports_age_unknown_not_a_number():
    probe, _ = _probe_refusing(raises=True)
    undated = staleness.Reading(used_percent=13.0, observed_at=None)

    reported = staleness.resolve_reading(
        undated, probe=probe, shelf_life=SHELF_LIFE, now=NOW
    )

    assert reported.used_percent == 13.0
    assert reported.age_seconds is None
    assert reported.serving_state == "unknown"


def test_shelf_life_flight_key_moves_which_readings_requery():
    """The same bounded-age reading re-queries under one key value and not another."""
    reading = _reading(used_percent=13.0, age=timedelta(minutes=90))
    fresh = staleness.Reading(used_percent=14.0, observed_at=NOW, source="probe")

    narrow = flight.resolve(overrides={"budget": {"evidence_shelf_life_minutes": 60}})
    wide = flight.resolve(overrides={"budget": {"evidence_shelf_life_minutes": 180}})
    assert staleness.configured_shelf_life_minutes(narrow.config) == 60.0
    assert staleness.configured_shelf_life_minutes(wide.config) == 180.0

    probe, calls = _probe_returning(fresh)
    requery = staleness.resolve_configured_reading(
        reading, probe=probe, config=narrow.config, now=NOW
    )

    def forbidden_probe():
        raise AssertionError("a reading inside the wider shelf life must not re-query")

    trusted = staleness.resolve_configured_reading(
        reading, probe=forbidden_probe, config=wide.config, now=NOW
    )

    assert calls == [1]
    assert requery.used_percent == 14.0
    assert trusted.used_percent == 13.0
    assert trusted.requeried is False
