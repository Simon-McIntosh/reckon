"""A pick reads each review dimension's median and how many reviews back it.

A pick's at-requirement block reports one review-total median, so a total that
hides a weak dimension reads the same as one that does not, and the block never
says how many rows that median rests on. Each candidate therefore also carries
``reviewed``, the number of matched rows whose review scored every dimension,
and ``review_dimensions_median``, each dimension's median over exactly those
rows. Only a fully scored row feeds the dimension lists and the count, so every
dimension's median rests on one shared sample; ``review_total_median`` keeps its
own sample, because the parser withholds a total when a dimension is absent and
a review scored before ``reuse`` existed carries a total without it. The figures
come from the one pass the pick already makes over its rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon.crew import routing
from reckon.crew.node import TaskNode
from reckon.crew.picker import prompts, snapshot
from reckon.crew.picker.types import PickRequest
from reckon.crew.review import REVIEW_DIMENSIONS

CONFIG = {
    "local_backend": "laneb",
    "roles": {"implement": {}, "review": {}},
    "backends": {
        "lanea": {"model": "m-a"},
        "laneb": {"model": "m-b"},
    },
}

#: The requirement a dispatch records for a deep, critical node, in the
#: capability schema's shape.
_DEEP_CRITICAL = {"reasoning": "deep", "risk": "critical"}
_STANDARD = {"reasoning": "standard", "risk": "low"}


def _request(capability: dict[str, Any] | None = None) -> PickRequest:
    return PickRequest(
        project="reckon",
        node=TaskNode(
            id="dimension-node",
            goal="read each review dimension at the requirement",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="the block carries each dimension median and its count",
            write_paths=[],
        ),
        session="s",
        capability=capability
        if capability is not None
        else {"requirements": dict(_DEEP_CRITICAL)},
    )


def _row(
    *,
    backend: str,
    model: str,
    gate: str,
    requirement: dict[str, str] | None,
    review: dict[str, Any] | None = None,
    days_ago: float = 1.0,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "backend": backend,
        "agent": {"model": model},
        "gate": gate,
        "role": "implement",
        "spec_level": "exact",
        "completed_at": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
    }
    if requirement is not None:
        row["node_definition"] = {"capability": {"requirements": dict(requirement)}}
    if review is not None:
        row["review"] = review
    return row


def _records() -> list[dict[str, Any]]:
    """Five matched rows on lane B at the request's requirement.

    Two are fully scored and build the dimension medians and the count. The
    other three each feed neither: one carries no review, one is a review the
    parser recorded with ``reuse`` absent (its total withheld), and one is a
    review scored before ``reuse`` existed (a total without the dimension). Only
    the recorded totals feed the total median, so it rests on three rows while
    the dimension medians rest on two.
    """

    return [
        _row(
            backend="laneb",
            model="m-b",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            review={
                "scores": {
                    "goal_fidelity": 19,
                    "evidence": 16,
                    "scope_discipline": 20,
                    "durability": 14,
                    "fit": 18,
                    "reuse": 19,
                },
                "total": 106.0,
            },
        ),
        _row(
            backend="laneb",
            model="m-b",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            review={
                "scores": {
                    "goal_fidelity": 10,
                    "evidence": 12,
                    "scope_discipline": 19,
                    "durability": 6,
                    "fit": 17,
                    "reuse": 11,
                },
                "total": 75.0,
            },
        ),
        _row(
            backend="laneb",
            model="m-b",
            gate="passed",
            requirement=_DEEP_CRITICAL,
        ),
        _row(
            backend="laneb",
            model="m-b",
            gate="not-run",
            requirement=_DEEP_CRITICAL,
            review={
                "scores": {
                    "goal_fidelity": 17,
                    "evidence": 15,
                    "scope_discipline": 20,
                    "durability": 16,
                    "fit": 18,
                },
                "absent": ["reuse"],
                "total": None,
            },
        ),
        _row(
            backend="laneb",
            model="m-b",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            review={
                "scores": {
                    "goal_fidelity": 18,
                    "evidence": 17,
                    "scope_discipline": 20,
                    "durability": 15,
                    "fit": 18,
                },
                "total": 88.0,
            },
        ),
    ]


@pytest.fixture
def picker_env(monkeypatch, tmp_path):
    """Isolate candidates() from the ledger, the repository and the network."""

    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    monkeypatch.setattr(snapshot, "_fit", lambda request, name, backend, repo, **k: [])
    monkeypatch.setattr(
        snapshot,
        "_serving_observation",
        lambda backend: {"status": "served", "detail": ""},
    )
    monkeypatch.setattr(
        routing, "_estimated_hours", lambda *a, **k: (None, "unavailable")
    )
    return tmp_path


def _candidates(records, picker_env, request=None):
    return snapshot.candidates(
        request or _request(),
        CONFIG,
        picker_env,
        records=records,
        budget_snapshot={"backends": [], "groups": []},
        verdict_inputs={},
        cached_only=True,
    )


def _by_backend(candidates) -> dict[str, Any]:
    return {candidate.backend: candidate for candidate in candidates}


def test_the_block_reads_each_dimension_median_and_its_count(picker_env):
    """The fully scored rows alone set the dimension medians and their count,
    while the total median keeps its own, larger sample."""

    block = _by_backend(_candidates(_records(), picker_env))[
        "laneb"
    ].outcomes_at_requirement

    assert block["reviewed"] == 2
    assert block["review_total_median"] == 88.0

    dimensions = block["review_dimensions_median"]
    assert list(dimensions) == list(REVIEW_DIMENSIONS)
    assert dimensions["goal_fidelity"] == 14.5
    assert dimensions["durability"] == 10.0
    assert dimensions["reuse"] == 15.0
    assert dimensions == {
        "goal_fidelity": 14.5,
        "evidence": 14.0,
        "scope_discipline": 19.5,
        "durability": 10.0,
        "fit": 17.5,
        "reuse": 15.0,
    }


def test_a_candidate_with_no_matched_row_reads_no_dimensions(picker_env):
    """Lane A never ran at the request's requirement, so it has no sample: the
    count is zero and the medians are null rather than empty."""

    records = [
        _row(
            backend="lanea",
            model="m-a",
            gate="passed",
            requirement=_STANDARD,
            review={
                "scores": {
                    "goal_fidelity": 20,
                    "evidence": 20,
                    "scope_discipline": 20,
                    "durability": 20,
                    "fit": 20,
                    "reuse": 20,
                },
                "total": 120.0,
            },
        )
    ]
    block = _by_backend(_candidates(records, picker_env))[
        "lanea"
    ].outcomes_at_requirement

    assert block["reviewed"] == 0
    assert block["review_dimensions_median"] is None


def test_a_review_at_another_requirement_changes_neither_field(picker_env):
    """A fully scored row at a different requirement, and a fully scored row
    that recorded no requirement, both leave the matched fields untouched."""

    alone = _by_backend(_candidates(_records(), picker_env))[
        "laneb"
    ].outcomes_at_requirement

    extra = _records()
    extra.append(
        _row(
            backend="laneb",
            model="m-b",
            gate="passed",
            requirement=_STANDARD,
            review={
                "scores": {
                    "goal_fidelity": 1,
                    "evidence": 1,
                    "scope_discipline": 1,
                    "durability": 1,
                    "fit": 1,
                    "reuse": 1,
                },
                "total": 6.0,
            },
        )
    )
    extra.append(
        _row(
            backend="laneb",
            model="m-b",
            gate="passed",
            requirement=None,
            review={
                "scores": {
                    "goal_fidelity": 2,
                    "evidence": 2,
                    "scope_discipline": 2,
                    "durability": 2,
                    "fit": 2,
                    "reuse": 2,
                },
                "total": 12.0,
            },
        )
    )
    with_extra = _by_backend(_candidates(extra, picker_env))[
        "laneb"
    ].outcomes_at_requirement

    assert with_extra["reviewed"] == alone["reviewed"] == 2
    assert with_extra["review_dimensions_median"] == alone["review_dimensions_median"]


def test_the_rendered_state_carries_the_block_and_its_glossary_entry(picker_env):
    """The state Jev is shown carries both fields, and the glossary defines both."""

    candidates = [c for c in _candidates(_records(), picker_env) if not c.reasons]
    state = prompts.build_state(
        node=_request().node,
        capability=_DEEP_CRITICAL,
        estimated_context=0,
        comment="",
        candidates=candidates,
        lane={"return_times": {}, "lanes": {}, "local_lane": {}},
        attempts=0,
    )
    lane_b = state["candidates"][
        prompts.option_key(next(c for c in candidates if c.backend == "laneb"))
    ]["outcomes_at_requirement"]
    assert lane_b["reviewed"] == 2
    assert lane_b["review_dimensions_median"]["goal_fidelity"] == 14.5

    glossary = json.loads(prompts.render("questions.jinja", candidates=candidates))[
        "route"
    ]["glossary"]
    entry = glossary["outcomes_at_requirement"]
    assert "reviewed" in entry
    assert "review_dimensions_median" in entry
