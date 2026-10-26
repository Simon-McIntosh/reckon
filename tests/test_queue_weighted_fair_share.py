"""Admission chooses the project by weighted deficit round robin.

A project's share of launched work tracks its configured fair-use weight
whatever its queue depth, a project with less ready work than its share never
holds a slot idle, and once weights are configured the allowance is read
lane-wide rather than from the dispatching session's own share.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon import flight
from reckon.crew import dispatch_plan, queue_admission

PROJECTS = ("alpha", "beta")


def _queued(project: str, index: int, at: datetime) -> dict[str, Any]:
    return {
        "run_id": f"{project}-{index}",
        "project": project,
        "session": "coordinator-a",
        "backend": "local",
        "phase": "queued",
        "queued_at": at.isoformat(),
    }


def _queue(project: str, count: int, *, start: datetime) -> list[dict[str, Any]]:
    return [
        _queued(project, index, start + timedelta(seconds=index))
        for index in range(count)
    ]


def _admissions(
    weights: dict[str, float], *, rounds: int, per_project: int
) -> list[str]:
    """The project of each admission, taken from the ordering the pass uses."""
    start = datetime(2026, 10, 26, tzinfo=UTC)
    ordered = [
        *_queue("alpha", per_project, start=start),
        *_queue("beta", per_project, start=start + timedelta(seconds=per_project)),
    ]
    sequence = queue_admission.weighted_project_order(ordered, weights)
    return [str(pointer["project"]) for pointer in sequence[:rounds]]


def test_two_projects_weighted_three_to_one_launch_near_their_share() -> None:
    launches = _admissions({"alpha": 3.0, "beta": 1.0}, rounds=80, per_project=100)

    alpha = launches.count("alpha")
    beta = launches.count("beta")
    assert alpha + beta == 80
    assert 3 / 4 * 80 * 0.9 <= alpha <= 3 / 4 * 80 * 1.1, (alpha, beta)
    assert 1 / 4 * 80 * 0.9 <= beta <= 1 / 4 * 80 * 1.1, (alpha, beta)


def test_equal_weights_move_the_split_to_one_to_one() -> None:
    launches = _admissions({"alpha": 1.0, "beta": 1.0}, rounds=80, per_project=100)

    alpha = launches.count("alpha")
    beta = launches.count("beta")
    assert alpha + beta == 80
    assert 40 * 0.9 <= alpha <= 40 * 1.1, (alpha, beta)
    assert 40 * 0.9 <= beta <= 40 * 1.1, (alpha, beta)


def test_equal_weights_alternate_from_the_first_admission() -> None:
    launches = _admissions({"alpha": 1.0, "beta": 1.0}, rounds=8, per_project=4)

    assert launches == ["alpha", "beta"] * 4


def test_a_shallow_project_does_not_hold_a_slot_idle() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    ordered = [
        *_queue("alpha", 6, start=start),
        *_queue("beta", 2, start=start + timedelta(seconds=6)),
    ]

    sequence = queue_admission.weighted_project_order(
        ordered, {"alpha": 1.0, "beta": 1.0}
    )

    assert [pointer["project"] for pointer in sequence[:8]] == [
        "alpha",
        "beta",
        "alpha",
        "beta",
        "alpha",
        "alpha",
        "alpha",
        "alpha",
    ]
    assert len(sequence) == 8


def test_a_project_under_its_share_leaves_the_rest_of_the_pass_to_the_other() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    ordered = [
        *_queue("alpha", 1, start=start),
        *_queue("beta", 9, start=start + timedelta(seconds=1)),
    ]

    sequence = queue_admission.weighted_project_order(
        ordered, {"alpha": 3.0, "beta": 1.0}
    )

    assert [pointer["project"] for pointer in sequence].count("alpha") == 1
    assert [pointer["project"] for pointer in sequence][:3] == [
        "alpha",
        "beta",
        "beta",
    ]


def test_priority_order_within_a_project_is_kept() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    ordered = [
        *_queue("alpha", 3, start=start),
        *_queue("beta", 3, start=start + timedelta(seconds=3)),
    ]

    sequence = queue_admission.weighted_project_order(
        ordered, {"alpha": 1.0, "beta": 1.0}
    )

    assert [
        pointer["run_id"] for pointer in sequence if "alpha" in pointer["run_id"]
    ] == [
        "alpha-0",
        "alpha-1",
        "alpha-2",
    ]


def test_weights_are_declared_in_flight_configuration_with_equal_default() -> None:
    assert flight.project_weights({}, PROJECTS) == {"alpha": 1.0, "beta": 1.0}
    assert flight.project_weights({"queue": {}}, PROJECTS) == {
        "alpha": 1.0,
        "beta": 1.0,
    }

    configured = {"queue": {"project_weights": {"alpha": 3, "beta": 1}}}

    assert flight.project_weights(configured, PROJECTS) == {
        "alpha": 3.0,
        "beta": 1.0,
    }
    assert flight.project_weights(configured, (*PROJECTS, "gamma"))["gamma"] == 1.0
    assert flight.fair_use_weights_configured(configured) is True
    assert flight.fair_use_weights_configured({}) is False


def test_a_weight_that_is_not_a_positive_number_reads_as_equal() -> None:
    configured = {"queue": {"project_weights": {"alpha": 0, "beta": "heavy"}}}

    assert flight.project_weights(configured, PROJECTS) == {
        "alpha": 1.0,
        "beta": 1.0,
    }
    assert flight.fair_use_weights_configured(configured) is False


def _lane_document(tmp_path, *, lane_wide: float, session_share: float):
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 9,
                "admission": {
                    "worker_slots": lane_wide,
                    "observed_seconds": 900,
                    "sessions": {"coordinator-a": {"worker_slots": session_share}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _backend(tmp_path, *, lane_wide: float, session_share: float) -> dict[str, Any]:
    return {
        "lane_document": str(
            _lane_document(tmp_path, lane_wide=lane_wide, session_share=session_share)
        )
    }


def test_weights_configured_reads_the_lane_wide_worker_slots(tmp_path) -> None:
    backend = _backend(tmp_path, lane_wide=6, session_share=1)

    decision = dispatch_plan._dispatch_lane_allowance(
        backend, session="coordinator-a", weights_configured=True
    )

    assert decision["allowance"] == 6
    assert decision["held"] is False


def test_without_weights_the_session_share_still_governs(tmp_path) -> None:
    backend = _backend(tmp_path, lane_wide=6, session_share=1)

    decision = dispatch_plan._dispatch_lane_allowance(backend, session="coordinator-a")

    assert decision["allowance"] == 1


def test_a_zero_session_share_holds_nothing_once_weights_are_configured(
    tmp_path,
) -> None:
    backend = _backend(tmp_path, lane_wide=4, session_share=0)

    held_before = dispatch_plan._dispatch_lane_allowance(
        backend, session="coordinator-a"
    )
    held_after = dispatch_plan._dispatch_lane_allowance(
        backend, session="coordinator-a", weights_configured=True
    )

    assert held_before["held"] is True
    assert held_after["held"] is False
    assert held_after["allowance"] == 4


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, None),
        (
            {"queue": {"project_weights": {"alpha": 3, "beta": 1}}},
            {"alpha": 3.0, "beta": 1.0},
        ),
    ],
)
def test_the_pass_resolves_weights_only_when_they_are_declared(
    config: dict[str, Any], expected: dict[str, float] | None
) -> None:
    pointers = [
        {"project": "alpha", "session": "coordinator-a"},
        {"project": "beta", "session": "coordinator-a"},
    ]

    weights = queue_admission._fair_use_weights(
        pointers, config_for=lambda _pointer: config
    )

    assert weights == expected
