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
