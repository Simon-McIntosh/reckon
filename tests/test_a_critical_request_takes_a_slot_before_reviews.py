"""A critical override reaches a freed slot ahead of waiting reviews.

A delivered run's review leads the lane, because a run that has finished cannot
land without it and the sections waiting on that run cannot launch. Critical
work takes precedence: a request whose priority override is at or above the
project's critical level takes a freed slot before those reviews. Every request below that level keeps today's order, reviews first.

The critical level is the flight setting ``queue.critical_priority`` (default
100), read through the same resolved config the admission pass uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import flight
from reckon.crew import queue_order
from reckon.crew.admission import ordering as admission_ordering

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
PROJECT = "sample"


def _pointer(
    run_id: str,
    *,
    role: str,
    priority: int | None = None,
    age_seconds: int = 0,
) -> dict[str, Any]:
    """A queued request, plain enough that no roadmap or review lookup runs.

    ``node.plan`` and ``node.section`` are left empty so the ordering reads no
    plan: the review class is decided by ``role`` alone.
    """
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": f"session-{run_id}",
        "backend": "local",
        "role": role,
        "phase": "queued",
        "queued_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "node": {"id": run_id, "plan": "", "section": ""},
    }
    if priority is not None:
        pointer["priority"] = priority
    return pointer


def _review(age_seconds: int = 600) -> dict[str, Any]:
    return _pointer("review-of-delivered", role="review", age_seconds=age_seconds)


def _implement(priority: int | None) -> dict[str, Any]:
    return _pointer("critical-implement", role="implement", priority=priority)


def _head(
    queued: list[dict[str, Any]],
    *,
    config_for: Any = None,
) -> str:
    kwargs: dict[str, Any] = {"now": NOW}
    if config_for is not None:
        kwargs["config_for"] = config_for
    order = queue_order.admission_order(queued, [], **kwargs)
    return str(order[0]["run_id"])


def test_an_override_at_the_critical_level_launches_ahead_of_a_review() -> None:
    review = _review(age_seconds=600)
    critical = _implement(priority=100)

    assert _head([review, critical]) == "critical-implement"
    assert _head([critical, review]) == "critical-implement"
    # The admission pass applies one more hoist over the lane; it must not put
    # the review back in front of a request the ordering already placed first.
    passes = admission_ordering.fair_share_admission_order(
        [review, critical], [], now=NOW, config_for=None
    )
    assert str(passes[0]["run_id"]) == "critical-implement"


def test_an_override_below_the_critical_level_keeps_the_review_first() -> None:
    review = _review(age_seconds=600)
    below = _implement(priority=99)

    assert _head([review, below]) == "review-of-delivered"


def test_no_override_keeps_the_review_first() -> None:
    review = _review(age_seconds=600)
    plain = _implement(priority=None)

    assert _head([review, plain]) == "review-of-delivered"


def test_a_project_critical_level_of_fifty_lets_an_override_of_fifty_lead(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project-flight.yaml"
    project.write_text(
        "queue:\n  critical_priority: 50\n",
        encoding="utf-8",
    )
    resolved = flight.resolve(project=PROJECT, project_path=project)
    config = resolved.config
    assert flight.queue_critical_priority(config) == 50

    review = _review(age_seconds=600)
    at_level = _implement(priority=50)
    assert _head([review, at_level], config_for=lambda _p: config) == (
        "critical-implement"
    )

    # The same override is below the shipped default of 100, so without the
    # project layer the review still leads.
    defaulted = flight.resolve(
        project=PROJECT, project_path=tmp_path / "absent.yaml"
    ).config
    below_default = _implement(priority=50)
    assert (
        _head(
            [_review(age_seconds=600), below_default], config_for=lambda _p: defaulted
        )
        == "review-of-delivered"
    )


def test_the_admission_pass_puts_critical_first_then_reviews_then_the_rest() -> None:
    """The pass the admit service runs, not only the raw ordering.

    ``fair_share_admission_order`` adds a role-share pass and a priority hoist
    over the ordering; with a lower request present, that hoist once rebuilt the
    review's lead and pushed the critical request below it.
    """
    review = _review(age_seconds=600)
    critical = _implement(priority=100)
    lower = _pointer("lower-implement", role="implement", priority=10, age_seconds=30)

    order = admission_ordering.fair_share_admission_order(
        [review, critical, lower], [], now=NOW, config_for=None
    )
    assert [str(row["run_id"]) for row in order] == [
        "critical-implement",
        "review-of-delivered",
        "lower-implement",
    ]


def test_the_promotion_order_honours_a_project_critical_level_of_fifty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion queue's only external ordering call reads the project level.

    ``promotion_requests._pending_requests`` is the sole external caller of
    ``fair_queue_order_details``. Its config reader must reach the project's
    resolved config so a project setting of 50 lets an override of 50 lead,
    rather than falling back to the shipped default of 100.
    """
    import json

    def _write(name: str, *, priority: int | None, node: str) -> None:
        record = {
            "run_id": name,
            "project": PROJECT,
            "session": f"session-{name}",
            "node": node,
            "phase": "promotion-requested",
            "requested_at": (NOW - timedelta(minutes=5)).isoformat(),
        }
        if priority is not None:
            record["priority_override"] = priority
        (tmp_path / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")

    _write("review-of-delivered", priority=None, node="review-of-node-x")
    _write("critical-at-fifty", priority=50, node="critical-at-fifty")
    _write("lower-at-forty-nine", priority=49, node="lower-at-forty-nine")

    from reckon.crew import promotion_requests, queue_admission

    monkeypatch.setattr(promotion_requests, "_requests_dir", lambda: tmp_path)
    monkeypatch.setattr(promotion_requests, "_recent_drained", lambda *a, **k: [])
    monkeypatch.setattr(
        promotion_requests,
        "_reviewed_request_runs",
        lambda requests, **k: (
            (request, str(request["run_id"]))
            for request in requests
            if str(request.get("node") or "").startswith("review-of-")
        ),
    )
    monkeypatch.setattr(
        queue_admission,
        "_resolve_lane_config",
        lambda request, **k: {"queue": {"critical_priority": 50}},
    )

    order = promotion_requests._pending_requests(now=NOW)
    assert [str(row["run_id"]) for row in order] == [
        "critical-at-fifty",
        "review-of-delivered",
        "lower-at-forty-nine",
    ]
