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
from reckon.crew import dispatch_plan, queue_admission, recovery_review_dispatch

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


def _config(weights: dict[str, float] | None) -> dict[str, Any]:
    return {} if weights is None else {"queue": {"project_weights": dict(weights)}}


def _admissions(
    weights: dict[str, float], *, rounds: int, per_project: int
) -> list[str]:
    """The project of each admission, taken from the ordering the pass uses."""
    start = datetime(2026, 10, 26, tzinfo=UTC)
    queued = [
        *_queue("alpha", per_project, start=start),
        *_queue("beta", per_project, start=start + timedelta(seconds=per_project)),
    ]
    config = _config(weights)
    sequence = queue_admission.fair_share_admission_order(
        queued,
        [],
        now=start + timedelta(minutes=1),
        config_for=lambda _pointer: config,
    )
    return [str(pointer["project"]) for pointer in sequence[:rounds]]


def test_two_projects_weighted_three_to_one_launch_near_their_share() -> None:
    launches = _admissions({"alpha": 3.0, "beta": 1.0}, rounds=80, per_project=100)

    alpha = launches.count("alpha")
    beta = launches.count("beta")
    assert launches[:8] == ["alpha", "alpha", "beta", "alpha"] * 2
    assert 3 / 4 * 80 * 0.9 <= alpha <= 3 / 4 * 80 * 1.1, (alpha, beta)
    assert 1 / 4 * 80 * 0.9 <= beta <= 1 / 4 * 80 * 1.1, (alpha, beta)


def test_equal_weights_move_the_split_to_one_to_one() -> None:
    launches = _admissions({"alpha": 1.0, "beta": 1.0}, rounds=80, per_project=100)

    alpha = launches.count("alpha")
    beta = launches.count("beta")
    assert launches[:8] == ["alpha", "beta"] * 4
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
    assert {pointer["run_id"] for pointer in sequence} == {
        *(f"alpha-{index}" for index in range(6)),
        "beta-0",
        "beta-1",
    }


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


def test_no_declared_weight_keeps_the_priority_order() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    queued = [
        *_queue("alpha", 3, start=start),
        *_queue("beta", 3, start=start + timedelta(seconds=3)),
    ]

    configured = queue_admission.fair_share_admission_order(
        queued,
        [],
        now=start + timedelta(minutes=1),
        config_for=lambda _p: _config(None),
    )
    plain = queue_admission.queue_order.admission_order(
        queued, [], now=start + timedelta(minutes=1)
    )

    assert [pointer["run_id"] for pointer in configured] == [
        pointer["run_id"] for pointer in plain
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


def test_strict_flight_resolution_accepts_project_weights() -> None:
    resolved = flight.resolve(
        overrides={"queue": {"project_weights": {"alpha": 3, "beta": 1}}},
        strict=True,
    )

    assert flight.project_weights(resolved.config, PROJECTS) == {
        "alpha": 3.0,
        "beta": 1.0,
    }


@pytest.mark.parametrize("weight", [0, -1, float("nan"), float("inf"), "heavy"])
def test_strict_flight_resolution_refuses_nonpositive_or_nonfinite_weights(
    weight: Any,
) -> None:
    with pytest.raises(
        flight.FlightConfigError, match=r"queue\.project_weights\.alpha"
    ):
        flight.resolve(
            overrides={"queue": {"project_weights": {"alpha": weight}}},
            strict=True,
        )


def test_queue_view_reports_the_weighted_launch_order() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    queued = [
        *_queue("alpha", 4, start=start),
        *_queue("beta", 4, start=start + timedelta(seconds=4)),
    ]
    config = _config({"alpha": 3, "beta": 1})

    placements = queue_admission.queued_placements(
        queued,
        now=start + timedelta(minutes=1),
        config_for=lambda _pointer: config,
    )
    ordered = sorted(placements, key=lambda run_id: placements[run_id]["position"])

    assert ordered[:4] == ["alpha-0", "alpha-1", "beta-0", "alpha-2"]
    assert placements["beta-0"]["effective_rank"] == 3
    assert "weighted fair use (beta: 1)" in placements["beta-0"]["reason"]


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


def test_a_later_projects_declaration_is_not_lost_to_the_first_projects_default() -> (
    None
):
    pointers = [
        {"project": "alpha", "session": "coordinator-a"},
        {"project": "beta", "session": "coordinator-a"},
    ]

    def config_for(pointer: dict[str, Any]) -> dict[str, Any]:
        if pointer["project"] == "alpha":
            return {}
        return {"queue": {"project_weights": {"alpha": 3, "beta": 1}}}

    weights = queue_admission._fair_use_weights(pointers, config_for=config_for)

    assert weights == {"alpha": 3.0, "beta": 1.0}


def test_a_declaration_anywhere_in_the_queue_sets_the_split() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    queued = [
        *_queue("alpha", 100, start=start),
        *_queue("beta", 100, start=start + timedelta(seconds=100)),
    ]

    def config_for(pointer: dict[str, Any]) -> dict[str, Any]:
        if pointer["project"] == "alpha":
            return {}
        return _config({"alpha": 3.0, "beta": 1.0})

    sequence = queue_admission.fair_share_admission_order(
        queued,
        [],
        now=start + timedelta(minutes=1),
        config_for=config_for,
    )
    launches = [str(pointer["project"]) for pointer in sequence[:80]]

    assert launches.count("alpha") == 60
    assert launches.count("beta") == 20


def test_no_declaration_anywhere_keeps_the_priority_order() -> None:
    pointers = [
        {"project": "alpha", "session": "coordinator-a"},
        {"project": "beta", "session": "coordinator-a"},
    ]

    assert queue_admission._fair_use_weights(pointers, config_for=lambda _p: {}) is None


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


def test_a_weighted_review_reads_lane_wide_slots_before_rerouting(tmp_path) -> None:
    backend = _backend(tmp_path, lane_wide=4, session_share=0)
    record = {"review_dispatch": {"status": "awaiting-lane", "backend": "local"}}
    unweighted = {"backends": {"local": backend}}
    weighted = {
        **unweighted,
        "queue": {"project_weights": {"alpha": 3, "beta": 1}},
    }

    assert (
        recovery_review_dispatch._zero_slot_held_lane(
            record, config=unweighted, session="coordinator-a"
        )
        == "local"
    )
    assert (
        recovery_review_dispatch._zero_slot_held_lane(
            record, config=weighted, session="coordinator-a"
        )
        == ""
    )


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
