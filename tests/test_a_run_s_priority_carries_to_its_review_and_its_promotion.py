"""A run's priority carries to its review and to its promotion.

The points a session placed on a request outlive the request: the run that
launched carries them, the review composed for it inherits them, and the
promotion request recorded for it snapshots them. Both queues rank by them, and
a coordinator may raise a delivered run's points through the same
``reprioritise`` verb, charged to the same session budget; the points then
return when the run lands.

The queue and request readers are the real ones, so what these tests read is the
order admission and the drain would actually take.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon.crew import (
    promotion_requests,
    queue_admission,
    queue_edit,
    queue_order,
    runs,
)

PROJECT = "priority-carry-fixture"
SESSION = "coordinator-a"
OTHER_SESSION = "coordinator-b"
LANE = "clive"
NOW = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)


def _queued(
    run_id: str,
    session: str,
    *,
    priority: int = 0,
    age: float = 0.0,
    role: str = "implement",
    node_id: str = "",
) -> dict[str, Any]:
    """A queued pointer, written where the queue readers find it."""
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "backend": LANE,
        "role": role,
        "phase": "queued",
        "queued_at": (NOW - timedelta(seconds=age)).isoformat(),
        "node": {"id": node_id or run_id, "plan": "", "section": ""},
        "priority": priority,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _delivered(run_id: str, session: str, *, priority: int = 0) -> dict[str, Any]:
    """A delivered run's pointer, as its launch and its delivery left it."""
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "backend": LANE,
        "role": "deliver",
        "phase": "complete",
        "node": {"id": run_id, "plan": "", "section": ""},
        "priority": priority,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _order(queued: list[dict], live: list[dict] | None = None) -> list[str]:
    return [
        str(row["run_id"])
        for row, _reason in queue_order.admission_order_details(
            queued, live or [], now=NOW
        )
    ]


def _promotion_order() -> list[str]:
    return [
        str(request["run_id"]) for request in promotion_requests._pending_requests()
    ]


def _charge_with_budget(monkeypatch: pytest.MonkeyPatch, budget: int = 10) -> None:
    """Resolve the flight config the reprioritise verb charges against."""
    from reckon import flight

    monkeypatch.setattr(
        queue_admission, "_resolve_lane_config", lambda _record: {"queue": {}}
    )
    monkeypatch.setattr(flight, "queue_priority_budget", lambda _config: budget)


# --- The review and the promotion inherit the run's points -------------------


def test_a_priority_four_review_is_admitted_before_an_earlier_priority_zero_review() -> (
    None
):
    # One session's own two reviews: within a session the older request was
    # admitted first, so only the points can put the later one ahead.
    quiet = _queued(
        "review-quiet",
        SESSION,
        age=600,
        role="review",
        node_id="review-of-r-quiet",
    )
    raised = _queued(
        "review-raised",
        SESSION,
        priority=4,
        age=60,
        role="review",
        node_id="review-of-r-raised",
    )

    assert _order([quiet, raised]) == ["review-raised", "review-quiet"]


def test_a_review_composed_for_a_prioritised_run_inherits_its_points() -> None:
    from reckon.crew import recovery_review_dispatch

    options = recovery_review_dispatch._composed_review_options(
        {"subject": "run", "priority": 4}, allow_unreconciled_runs=False
    )

    assert options.get("priority") == 4, options


def test_a_priority_four_promotion_is_drained_before_an_earlier_priority_zero_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promotion_requests, "_now", NOW.isoformat)
    quiet = _delivered("r-quiet", OTHER_SESSION)
    raised = _delivered("r-raised", SESSION, priority=4)
    quiet_request = promotion_requests.request_promotion(str(quiet["run_id"]))
    raised_request = promotion_requests.request_promotion(str(raised["run_id"]))
    quiet_request["requested_at"] = (NOW - timedelta(seconds=600)).isoformat()
    runs._write_json(
        promotion_requests._request_path(str(quiet["run_id"])), quiet_request
    )
    raised_request["requested_at"] = (NOW - timedelta(seconds=60)).isoformat()
    runs._write_json(
        promotion_requests._request_path(str(raised["run_id"])), raised_request
    )

    assert raised_request["priority_override"] == 4, raised_request
    assert _promotion_order() == ["r-raised", "r-quiet"]


# --- Reprioritising a delivered run spends the budget it moves with ----------


def test_reprioritising_a_delivered_run_charges_the_budget_and_landing_returns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _charge_with_budget(monkeypatch)
    pointer = _delivered("r-delivered", SESSION)
    recorded = promotion_requests.request_promotion(str(pointer["run_id"]))
    assert recorded["priority_override"] is None, recorded
    assert runs.session_priority_in_use(SESSION)[0] == 0

    result = queue_edit.reprioritise(str(pointer["run_id"]), 4, project=PROJECT)

    assert result["priority"] == 4, result
    assert runs.session_priority_in_use(SESSION)[0] == 4
    # The request a coordinator had already recorded is the record the drain
    # ranks, so the raise has to reach it and not only the run's pointer.
    queued_request = promotion_requests.read_promotion(str(pointer["run_id"]))
    assert queued_request["priority_override"] == 4, queued_request
    pending = promotion_requests._pending_requests()
    assert pending[0]["run_id"] == "r-delivered", pending
    assert pending[0]["ordering_reason"] == "priority override 4", pending[0]

    # Landing retires the live pointer, which is how the points come back: the
    # budget is read from the pointers rather than tallied beside them.
    from reckon.crew import promotion

    promotion._remove_promoted_pointer(
        str(pointer["run_id"]), runs.read_pointer(str(pointer["run_id"]))
    )
    assert runs.session_priority_in_use(SESSION)[0] == 0


def test_a_reprioritised_delivered_run_moves_its_review_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _charge_with_budget(monkeypatch)
    earlier = _queued(
        "review-other",
        OTHER_SESSION,
        age=600,
        role="review",
        node_id="review-of-r-other",
    )
    later = _queued(
        "review-raised",
        OTHER_SESSION,
        age=60,
        role="review",
        node_id="review-of-r-raised",
    )
    _delivered("r-raised", SESSION)

    assert _order([earlier, later]) == ["review-other", "review-raised"]

    queue_edit.reprioritise("r-raised", 4, project=PROJECT)

    assert _order([earlier, runs.read_pointer("review-raised")]) == [
        "review-raised",
        "review-other",
    ]


def test_a_raise_on_a_delivered_run_is_charged_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One raise, two records, one charge.

    The run is the record a coordinator reads and the review is the record
    admission ranks, so both name the points. A raise charged to both would make
    a session at its budget refuse a raise it can neither avoid paying for nor
    afford twice, so the session's own figure is the check here rather than
    either record's.
    """
    _charge_with_budget(monkeypatch, budget=4)
    pointer = _delivered("r-delivered", SESSION)
    _queued(
        "review-of-delivered",
        SESSION,
        role="review",
        node_id="review-of-r-delivered",
    )
    promotion_requests.request_promotion(str(pointer["run_id"]))
    assert runs.session_priority_in_use(SESSION)[0] == 0

    queue_edit.reprioritise(str(pointer["run_id"]), 4, project=PROJECT)

    total, holders = runs.session_priority_in_use(SESSION)
    assert total == 4, holders
    assert [holder["run_id"] for holder in holders] == ["review-of-delivered"], holders
    assert runs.read_pointer("r-delivered")["priority"] == 4
    assert runs.read_pointer("review-of-delivered")["priority_override"] == 4

    # The second raise replaces the charge rather than adding to it.
    queue_edit.reprioritise(str(pointer["run_id"]), 4, project=PROJECT)
    assert runs.session_priority_in_use(SESSION)[0] == 4


def test_withdrawing_a_raise_stops_the_charge_on_both_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise that is withdrawn must stop being charged, on both records."""
    _charge_with_budget(monkeypatch)
    pointer = _delivered("r-delivered", SESSION)
    _queued(
        "review-of-delivered",
        SESSION,
        role="review",
        node_id="review-of-r-delivered",
    )
    queue_edit.reprioritise(str(pointer["run_id"]), 1, project=PROJECT)
    assert runs.session_priority_in_use(SESSION)[0] == 1

    queue_edit.reprioritise(str(pointer["run_id"]), 0, project=PROJECT)

    assert runs.session_priority_in_use(SESSION)[0] == 0
    assert not runs.read_pointer("review-of-delivered").get("priority_override")
    assert runs.read_pointer("review-of-delivered")["priority"] == 0
    assert not runs.read_pointer("r-delivered").get(runs.PRIORITY_DELIVERED_MARK)
