"""Admission and promotion apply the same urgency policy."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import promotion_requests, queue_order

NOW = datetime(2026, 11, 17, 10, tzinfo=UTC)


@pytest.fixture
def queues(tmp_path, monkeypatch):
    monkeypatch.setattr(promotion_requests, "_requests_dir", lambda: tmp_path)
    monkeypatch.setattr(promotion_requests, "_recent_drained", lambda *args: [])
    monkeypatch.setattr(queue_order, "_roadmap_priority_keys", lambda rows: {})

    def populate(*, age=10, priority=100, review=False):
        pointers = []
        for run_id, seconds, override in (
            ("r-older", age, None),
            ("r-priority", 1, priority),
        ):
            stamp = (NOW - timedelta(seconds=seconds)).isoformat()
            request = {
                "run_id": run_id,
                "project": "fixture",
                "session": run_id,
                "requested_at": stamp,
                "priority_override": override,
                "phase": promotion_requests.REQUESTED,
                "node": "delivery",
            }
            (tmp_path / f"{run_id}.json").write_text(json.dumps(request))
            pointers.append(
                {
                    **request,
                    "phase": "queued",
                    "queued_at": stamp,
                    "node": {"id": run_id, "role": "implement"},
                }
            )
        if review:
            request["node"] = "review-of-r-older"
            (tmp_path / "r-priority.json").write_text(json.dumps(request))
            pointers[1]["node"] = {"id": request["node"], "role": "review"}
        return pointers

    return populate


def _orders(pointers):
    return [
        [row["run_id"] for row in queue_order.admission_order(pointers, [], now=NOW)],
        [row["run_id"] for row in promotion_requests._pending_requests(now=NOW)],
    ]


def test_changing_the_shared_starvation_bound_moves_both_queues(queues, monkeypatch):
    pointers = queues()
    monkeypatch.setattr(queue_order, "STARVATION_AGE_SECONDS", 1800)
    assert _orders(pointers) == [["r-priority", "r-older"]] * 2

    monkeypatch.setattr(queue_order, "STARVATION_AGE_SECONDS", 3)
    assert _orders(pointers) == [["r-older", "r-priority"]] * 2


@pytest.mark.parametrize("age", [1799, 1800, 1801])
def test_both_queues_agree_at_the_starvation_boundary(queues, age):
    pointers = queues(age=age)
    expected = ["r-older", "r-priority"] if age >= 1800 else ["r-priority", "r-older"]
    assert _orders(pointers) == [expected] * 2


def test_review_precedence_survives_a_changed_starvation_bound(queues, monkeypatch):
    pointers = queues(age=100, review=True)
    monkeypatch.setattr(queue_order, "STARVATION_AGE_SECONDS", 3)
    assert _orders(pointers) == [["r-priority", "r-older"]] * 2
