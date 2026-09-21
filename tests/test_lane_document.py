"""The lane-document reader yields every field the document actually carries.

The fixture below is copied from the shape a serving lane publishes in
practice: a ``null`` binding, a ``null`` mean context beside a measured state,
its occupancy figures under their ``_instant`` keys, and a shelf life of 45
seconds. The tests pin the reader's clock rather than reading freshness from
its own ``observed_at``, so freshness is judged against a fixed instant and no
expectation is derived from the calendar day the suite happens to run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew.lane_document import (
    UNKNOWN,
    read_lane_document,
    read_lane_document_file,
)

# The document's own observation stamp, and the instant freshness is judged
# against. Copied from a real publication; the reader never re-derives it.
OBSERVED_AT = "2026-09-15T21:47:23Z"
OBSERVED = datetime.fromisoformat(OBSERVED_AT)

CANONICAL_FIELDS = (
    "headroom",
    "running",
    "waiting",
    "concurrent_requests",
    "mean_context",
    "kv_occupancy",
    "state",
    "observed_at",
)

# The exact shape a serving lane publishes, trimmed to the fields the reader
# resolves. binding_observed is null and state is measured beside it; the
# occupancy figures sit under their ``_instant`` keys. A reader that discards
# the document on the null binding, or that reads only the bare keys, loses the
# numbers carried here.
LANE_DOCUMENT: dict = {
    "binding_observed": None,
    "concurrent_requests_instant": 96,
    "derived_from": {
        "formula": (
            "concurrent_requests = (pool_tokens * occupancy_target) // mean_context, "
            "capped at 96; mean_context = MEDIAN over the window of "
            "(pool_tokens * kv_occupancy / running); "
            "headroom = concurrent_requests - running_at_observation"
        ),
        "mean_context": None,
        "occupancy_target": 0.5,
        "pool_tokens": 4000000,
        "running_at_observation": 0,
    },
    "headroom_instant": 96,
    "headroom_is_upper_bound": True,
    "kv_occupancy": 0.0,
    "mean_context": None,
    "mean_context_instant": None,
    "model_id": "deepseek-v4.1-flash",
    "observed_at": OBSERVED_AT,
    "pool_tokens": 4000000,
    "running": 0,
    "suggested_shelf_life_seconds": 45,
    "state": "measured",
    "waiting": 0,
    "withheld": {
        "concurrent_requests": 96,
        "headroom": 96,
        "why": "nothing is resident, so there is no working context to divide by",
    },
}


def _copy() -> dict:
    """Return a fresh mutable copy of the real-shaped fixture."""
    return json.loads(json.dumps(LANE_DOCUMENT))


def test_null_binding_leaves_the_other_fields_as_the_numbers_carried() -> None:
    """A null document field costs its own field, not the whole carry."""
    report = read_lane_document(_copy(), now=OBSERVED + timedelta(seconds=10))

    assert isinstance(report["headroom"], (int, float))
    assert report["headroom"] == 96
    assert isinstance(report["running"], (int, float))
    assert report["running"] == 0
    assert report["waiting"] == 0
    assert report["concurrent_requests"] == 96
    assert report["kv_occupancy"] == 0.0
    assert report["state"] == "measured"
    assert report["observed_at"] == OBSERVED_AT
    # The document genuinely publishes null here, so this one field is unknown
    # while every sibling above resolved.
    assert report["mean_context"] == UNKNOWN
    assert "mean_context" in report["unknown_fields"]
    assert report["stale"] is False
    assert report["malformed"] is False


def test_a_field_absent_from_the_document_is_unknown_while_siblings_resolve() -> None:
    """Degradation runs in both directions: one absent, the rest carried."""
    document = _copy()
    del document["waiting"]

    report = read_lane_document(document, now=OBSERVED + timedelta(seconds=10))

    assert report["waiting"] == UNKNOWN
    assert "waiting" in report["unknown_fields"]
    assert report["headroom"] == 96
    assert report["running"] == 0
    assert report["state"] == "measured"


def test_a_measured_zero_is_carried_and_not_confused_with_unknown() -> None:
    """A real zero is a measurement, so it stays a number."""
    report = read_lane_document(_copy(), now=OBSERVED + timedelta(seconds=10))

    assert report["running"] == 0
    assert "running" not in report["unknown_fields"]
    assert "kv_occupancy" not in report["unknown_fields"]


def test_a_non_numeric_figure_is_unknown_rather_than_coerced() -> None:
    """A boolean or a string is not a figure, and no ceiling is invented."""
    document = _copy()
    document["headroom_instant"] = True
    document["running"] = "many"

    report = read_lane_document(document, now=OBSERVED + timedelta(seconds=10))

    assert report["headroom"] == UNKNOWN
    assert report["running"] == UNKNOWN
    # The sibling that did carry a number is untouched by the two unknowns.
    assert report["concurrent_requests"] == 96


def test_a_reading_inside_its_shelf_life_is_not_marked_stale() -> None:
    """A figure the document's own shelf life still vouches for is current."""
    report = read_lane_document(_copy(), now=OBSERVED + timedelta(seconds=5))

    assert report["stale"] is False
    assert report["age_seconds"] == 5
    assert report["headroom"] == 96


def test_a_stale_document_carries_its_age_and_a_stale_marker() -> None:
    """An old reading keeps its figures and states plainly that it is old."""
    report = read_lane_document(_copy(), now=OBSERVED + timedelta(seconds=1_000_000))

    assert report["stale"] is True
    assert report["age_seconds"] == 1_000_000
    assert report["shelf_life_seconds"] == 45
    assert report["headroom"] == 96
    assert report["running"] == 0
    assert report["state"] == "measured"
    assert "shelf life" in report["detail"]


@pytest.mark.parametrize(
    "document",
    [
        None,
        "{not json",
        "[1, 2, 3]",
        42,
        b"\xff\xfe",
        "",
    ],
    ids=["none", "not-json", "not-an-object", "a-number", "undecodable", "empty-text"],
)
def test_a_malformed_document_is_all_unknown_and_never_raises(document: object) -> None:
    """The reader is consulted outside a try block, so it must never raise."""
    report = read_lane_document(document, now=OBSERVED + timedelta(seconds=10))

    for field in CANONICAL_FIELDS:
        assert report[field] == UNKNOWN
    assert report["stale"] is False
    assert report["age_seconds"] is None
    assert report["detail"]


def test_the_file_reader_reports_a_missing_path_without_raising(tmp_path: Path) -> None:
    report = read_lane_document_file(tmp_path / "absent.json")

    assert report["headroom"] == UNKNOWN
    assert report["state"] == UNKNOWN
    assert report["malformed"] is False
    assert "cannot be read" in report["detail"]


def test_the_file_reader_resolves_a_published_document(tmp_path: Path) -> None:
    path = tmp_path / "lane.json"
    path.write_text(json.dumps(LANE_DOCUMENT), encoding="utf-8")

    report = read_lane_document_file(path, now=OBSERVED + timedelta(seconds=10))

    assert report["headroom"] == 96
    assert report["state"] == "measured"
    assert report["malformed"] is False
