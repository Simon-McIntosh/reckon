"""The local lane reading includes the router admission gate."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from reckon.crew.lane_document import UNKNOWN, read_lane_document

NOW = datetime(2026, 9, 25, 12, 42, 30, tzinfo=UTC)


def _engine_document() -> dict[str, object]:
    return {
        "headroom": 6,
        "running": 14,
        "waiting": 0,
        "concurrent_requests": 19,
        "observed_at": "2026-09-25T12:42:30Z",
        "state": "measured",
    }


def test_the_router_gate_reduces_headroom_and_reports_congestion() -> None:
    document = _engine_document()
    document["router_generation_gate"] = {
        "width": 14,
        "in_flight": 14,
        "waiting": 9,
    }

    report = read_lane_document(document, now=NOW)

    assert report["engine_headroom"] == 6
    assert report["admission_headroom"] == -9
    assert report["headroom"] == -9
    assert report["admission_verdict"] == "congested"
    assert "width 14 - in_flight 14 - waiting 9" in report["admission_reason"]
    assert report["router_generation_gate"] == {
        "width": 14,
        "in_flight": 14,
        "waiting": 9,
    }


def test_a_published_admission_reading_wins_over_local_arithmetic() -> None:
    document = _engine_document()
    document["router_generation_gate"] = {
        "width": 14,
        "in_flight": 2,
        "waiting": 1,
    }
    document["admission"] = {
        "headroom": -9,
        "verdict": "congested",
        "reason": "the router queue is full",
    }

    report = read_lane_document(document, now=NOW)

    assert report["admission_headroom"] == -9
    assert report["headroom"] == -9
    assert report["admission_verdict"] == "congested"
    assert report["admission_reason"] == "the router queue is full"


def test_an_idle_gate_keeps_the_engine_headroom() -> None:
    document = _engine_document()
    document["router_generation_gate"] = {"width": 14, "in_flight": 0, "waiting": 0}

    report = read_lane_document(document, now=NOW)

    assert report["admission_headroom"] == 14
    assert report["headroom"] == 6
    assert report["admission_verdict"] == "open"


def test_a_document_without_a_gate_keeps_the_existing_engine_reading() -> None:
    report = read_lane_document(_engine_document(), now=NOW)

    assert report["headroom"] == 6
    assert report["engine_headroom"] == 6
    assert report["admission_headroom"] == UNKNOWN
    assert report["admission_verdict"] == UNKNOWN
    assert report["router_generation_gate"] == {
        "width": UNKNOWN,
        "in_flight": UNKNOWN,
        "waiting": UNKNOWN,
    }


def test_the_flight_declares_the_local_lane_document() -> None:
    path = Path(__file__).parents[1] / "docs/state/reckon/flight.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert (
        config["backends"]["clive"]["lane_document"] == "~/public/imas-ambix/lane.json"
    )
