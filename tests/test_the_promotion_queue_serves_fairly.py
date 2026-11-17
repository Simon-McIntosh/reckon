"""Promotion order respects urgency, session shares, and request age."""

from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import promotion_requests as requests
from reckon.crew import runs
from reckon.crew.queue_order import STARVATION_AGE_SECONDS

NOW = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)


def _queue(run_id, session, *, age=0, priority=None, reviewed_run=None):
    pointer = {
        "run_id": run_id,
        "project": "promotion-order-fixture",
        "session": session,
        "phase": "complete",
        "node": {"id": f"review-of-{reviewed_run}" if reviewed_run else "delivery"},
    }
    if priority is not None:
        pointer["priority_override"] = priority
    runs._write_json(runs.pointer_path(run_id), pointer)
    request = requests.request_promotion(run_id)
    request["requested_at"] = (NOW - timedelta(seconds=age)).isoformat()
    runs._write_json(requests._request_path(run_id), request)
    return request


def test_a_burst_leaves_other_sessions_in_the_first_four(monkeypatch):
    monkeypatch.setattr(requests, "_now", NOW.isoformat)
    ordinary = [f"r-early-{index:03}" for index in range(67)]
    for index, run_id in enumerate(ordinary):
        _queue(run_id, "A", age=300 - index)
    _queue("r-later-B", "B", age=100)
    _queue("r-later-C", "C", age=90)
    _queue("r-newest-priority", "A", priority=100)

    ordered = requests._pending_requests()
    served = [row["run_id"] for row in ordered]
    places = {run_id: served.index(run_id) + 1 for run_id in served}
    assert served[:4] == [
        "r-newest-priority",
        ordinary[0],
        "r-later-B",
        "r-later-C",
    ], f"B served {places['r-later-B']}th; C served {places['r-later-C']}th"
    assert [run_id for run_id in served if run_id in ordinary] == ordinary

    report = requests.promotion_queue_report(now=NOW)
    assert [row["run_id"] for row in report["requests"]] == served
    assert [row["position"] for row in report["requests"]] == list(range(1, 71))
    assert report["requests"][0]["reason"] == "priority override 100"
    assert "session" in report["requests"][1]["reason"]

    # Completed promotions charge the session even after the service restarts.
    requests._record_drained(
        {
            "run_id": "r-served",
            "session": "A",
            "phase": requests.PROMOTED,
            "requested_at": NOW.isoformat(),
            "finished_at": NOW.isoformat(),
        }
    )
    requests._record_drained(
        {
            "run_id": "r-expired",
            "session": "B",
            "phase": requests.PROMOTED,
            "requested_at": NOW.isoformat(),
            "finished_at": (NOW - timedelta(hours=2)).isoformat(),
        }
    )
    requests._request_path("r-newest-priority").unlink()
    assert [row["run_id"] for row in requests._pending_requests()][:3] == [
        "r-later-B",
        "r-later-C",
        ordinary[0],
    ]


def test_an_aged_request_precedes_a_younger_priority(monkeypatch):
    monkeypatch.setattr(requests, "_now", NOW.isoformat)
    _queue("r-early-priority", "priority", priority=100)
    _queue("r-later-aged", "aged", age=STARVATION_AGE_SECONDS + 1)

    assert [row["run_id"] for row in requests._pending_requests()] == [
        "r-later-aged",
        "r-early-priority",
    ]
    assert requests.promotion_queue_report(now=NOW)["requests"][0]["reason"] == "ageing"


def test_a_queued_review_precedes_the_run_it_reviews(monkeypatch):
    monkeypatch.setattr(requests, "_now", NOW.isoformat)
    _queue("r-early-delivery", "delivery", priority=100, age=STARVATION_AGE_SECONDS + 1)
    _queue("r-later-review", "review", reviewed_run="r-early-delivery")

    assert [row["run_id"] for row in requests._pending_requests()] == [
        "r-later-review",
        "r-early-delivery",
    ]
    assert (
        requests.promotion_queue_report(now=NOW)["requests"][0]["reason"]
        == "delivered review"
    )


@pytest.mark.parametrize("inactive_phase", [requests.RUNNING, requests.WAITING])
def test_only_runnable_requests_consume_future_session_turns(
    monkeypatch, inactive_phase
):
    monkeypatch.setattr(requests, "_now", NOW.isoformat)
    inactive = _queue("r-inactive-A", "A", age=300)
    inactive["phase"] = inactive_phase
    runs._write_json(requests._request_path(inactive["run_id"]), inactive)
    if inactive_phase == requests.RUNNING:
        requests._record_drained(
            {
                "run_id": "r-promoted-B",
                "session": "B",
                "phase": requests.PROMOTED,
                "requested_at": NOW.isoformat(),
                "finished_at": NOW.isoformat(),
            }
        )
    _queue("r-older-A", "A", age=200)
    _queue("r-younger-B", "B", age=100)

    report = requests.promotion_queue_report(now=NOW)
    assert [row["run_id"] for row in report["requests"]] == [
        "r-older-A",
        "r-younger-B",
    ], report
    assert report["running"] == int(inactive_phase == requests.RUNNING)
    assert report["parked"] == int(inactive_phase == requests.WAITING)
    assert any(
        row["run_id"] == inactive["run_id"] for row in requests._pending_requests()
    )

    service = requests.PromotionService(width=1, execute=lambda request: {"ok": True})
    claim = service._claim
    monkeypatch.setattr(
        service,
        "_claim",
        lambda name: (
            None if name == "promotion-request-owner-r-inactive-A" else claim(name)
        ),
    )
    monkeypatch.setattr(
        requests,
        "_review_self_waits",
        lambda request: (request.get("phase") == requests.WAITING, "review pending"),
    )
    try:
        assert service.tick() == ["r-older-A"]
    finally:
        service.settle(timeout=10)


def test_report_rate_window_preserves_the_drains_queue_order(monkeypatch):
    monkeypatch.setattr(requests, "_now", NOW.isoformat)
    _queue("r-older-A", "A", age=200)
    _queue("r-younger-B", "B", age=100)
    completed = (NOW - timedelta(minutes=10)).isoformat()
    requests._record_drained(
        {
            "run_id": "r-promoted-A",
            "session": "A",
            "phase": requests.PROMOTED,
            "requested_at": completed,
            "finished_at": completed,
        }
    )
    drain_order = [row["run_id"] for row in requests._pending_requests()]
    assert drain_order == ["r-younger-B", "r-older-A"]
    for window in (60, 3600, 7200):
        report = requests.promotion_queue_report(now=NOW, window_seconds=window)
        assert [row["run_id"] for row in report["requests"]] == drain_order, report
        assert report["finished_last_window"] == int(window >= 600)

    service = requests.PromotionService(width=1, execute=lambda request: {"ok": True})
    try:
        assert service.tick() == [drain_order[0]]
    finally:
        service.settle(timeout=10)
