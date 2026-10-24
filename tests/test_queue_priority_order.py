"""Queued requests use roadmap section priority, explicit overrides, and age."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon.crew import queue_admission, queue_order

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _request(
    run_id: str,
    *,
    age: int,
    section: str,
    session: str = "one",
    role: str = "implement",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": "fixture",
        "plan": "example",
        "section": section,
        "session": session,
        "backend": "local",
        "phase": "queued",
        "queued_at": (NOW - timedelta(seconds=age)).isoformat(),
        "node": {"id": run_id, "role": role},
    }


@pytest.fixture()
def roadmap(monkeypatch: pytest.MonkeyPatch) -> None:
    from reckon import mcp_discovery
    from reckon import roadmap as roadmap_module

    rows = [
        {
            "ref": f"example#{section}",
            "sprint": "current",
            "roi": roi,
            "unlocks": [],
            "stuck": False,
            "after_hold": False,
            "remaining_effort_hours": 1.0,
        }
        for section, roi in (("low", "low"), ("high", "high"), ("review", "mid"))
    ]
    monkeypatch.setattr(
        mcp_discovery,
        "_discover_project",
        lambda project, root: {
            "inventory": [{"slug": "example"}],
            "sprints": [{"id": "current"}],
        },
    )
    monkeypatch.setattr(mcp_discovery, "_inventory_row", lambda item: item)
    monkeypatch.setattr(
        roadmap_module,
        "build_roadmap",
        lambda *args, **kwargs: {
            "immediate_roadmap": rows,
            "critical_path": {"sections": ["example#high"]},
        },
    )


def test_roadmap_signals_rank_a_newer_section_ahead_of_an_older_one(
    roadmap: None,
) -> None:
    queued = [
        _request("older-low", age=120, section="low"),
        _request("newer-high", age=60, section="high"),
    ]

    assert [
        row["run_id"] for row in queue_order.admission_order(queued, queued, now=NOW)
    ] == [
        "newer-high",
        "older-low",
    ]
    placements = queue_admission.queued_placements(queued, now=NOW)
    assert placements["newer-high"]["effective_rank"] == 1
    assert placements["newer-high"]["reason"] == "critical path"
    assert placements["older-low"]["effective_rank"] == 2


def test_reprioritised_request_leads_older_roadmap_request(roadmap: None) -> None:
    queued = [
        _request("older-high", age=120, section="high"),
        _request("raised-low", age=60, section="low", session="two"),
    ]
    queued[1]["priority_override"] = 7

    assert [
        row["run_id"] for row in queue_order.admission_order(queued, queued, now=NOW)
    ] == [
        "raised-low",
        "older-high",
    ]
    assert queue_admission.queued_placements(queued, now=NOW)["raised-low"] == {
        "position": 1,
        "rank": 1,
        "effective_rank": 1,
        "reason": "priority override 7",
    }


def test_lowest_priority_ages_ahead_of_fresh_high_priority(roadmap: None) -> None:
    queued = [
        _request("aged-low", age=queue_order.STARVATION_AGE_SECONDS, section="low"),
        _request("fresh-high", age=1, section="high"),
    ]

    assert (
        queue_order.admission_order(queued, queued, now=NOW)[0]["run_id"] == "aged-low"
    )
    assert (
        queue_admission.queued_placements(queued, now=NOW)["aged-low"]["reason"]
        == "ageing"
    )


def test_delivered_review_retains_precedence(roadmap: None) -> None:
    queued = [
        _request("aged-low", age=queue_order.STARVATION_AGE_SECONDS, section="low"),
        _request("raised-high", age=60, section="high"),
        _request("review-of-raised-high", age=1, section="review", role="review"),
    ]
    queued[1]["priority_override"] = 9

    assert (
        queue_order.admission_order(queued, queued, now=NOW)[0]["run_id"]
        == "review-of-raised-high"
    )
    assert (
        queue_admission.queued_placements(queued, now=NOW)["review-of-raised-high"][
            "reason"
        ]
        == "delivered review"
    )


def test_missing_roadmap_section_has_no_fabricated_priority(roadmap: None) -> None:
    queued = [
        _request("unknown", age=120, section="missing"),
        _request("known", age=60, section="high"),
    ]

    assert queue_order.admission_order(queued, queued, now=NOW)[0]["run_id"] == "known"
    assert (
        "unavailable"
        in queue_admission.queued_placements(queued, now=NOW)["unknown"]["reason"]
    )
