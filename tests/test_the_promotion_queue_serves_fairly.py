"""Promotion order respects urgency, session shares, and request age."""

from datetime import UTC, datetime, timedelta

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
