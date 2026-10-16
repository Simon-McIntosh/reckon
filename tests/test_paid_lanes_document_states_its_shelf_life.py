"""The published headroom document states the shelf life its own clock sets.

A reader has to know how long the document's readings stay usable, and that
figure must move with the refresh cadence rather than sit beside it as a second
literal. A fixed instant keeps every age arithmetic rather than a stroke of the
clock, so a case cannot pass on the day it is written and drift after.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reckon import budget
from reckon.crew import paid_lanes
from reckon.crew.picker import lane_context

#: Two five-minute refresh intervals.
FIVE_MINUTE_SHELF_LIFE = 600.0


def test_a_composed_document_publishes_the_shelf_life_its_clock_sets() -> None:
    document = paid_lanes.compose_document(["acct"])
    assert document["suggested_shelf_life_seconds"] == FIVE_MINUTE_SHELF_LIFE


def test_the_shelf_life_follows_a_patched_refresh_interval(monkeypatch) -> None:
    monkeypatch.setattr(paid_lanes, "REFRESH_INTERVAL", "10min")
    document = paid_lanes.compose_document(["acct"])
    assert document["suggested_shelf_life_seconds"] == 1200.0


def test_the_shelf_life_is_derived_from_the_refresh_interval() -> None:
    for interval, expected in (("5min", 600.0), ("10min", 1200.0), ("3min", 360.0)):
        assert paid_lanes.suggested_shelf_life_seconds(interval) == expected
        assert expected == (
            paid_lanes.refresh_interval_seconds(interval)
            * paid_lanes.SHELF_LIFE_INTERVALS
        )


def test_a_document_reading_is_judged_against_the_documents_shelf_life() -> None:
    moment = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    shelf = paid_lanes.compose_document(["acct"]).get("suggested_shelf_life_seconds")

    def reading(age_seconds: float) -> dict[str, object]:
        return {
            "source": budget.WINDOW_SOURCE_DOCUMENT,
            "observed_at": (moment - timedelta(seconds=age_seconds)).isoformat(),
            "suggested_shelf_life_seconds": shelf,
        }

    fresh = lane_context._budget_block(
        reading(345.0),
        moment=moment,
        shelf_life_minutes=5.0,
        document_backed=True,
    )
    stale = lane_context._budget_block(
        reading(700.0),
        moment=moment,
        shelf_life_minutes=5.0,
        document_backed=True,
    )
    assert fresh["budget_source"] == budget.WINDOW_SOURCE_DOCUMENT
    assert fresh["stale"] is False
    assert stale["stale"] is True
