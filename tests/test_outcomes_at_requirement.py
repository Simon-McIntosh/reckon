"""A candidate's gate history is read at the node's requirement.

``_candidates_scan`` counts gate outcomes per lane and model filtered by role
and spec level alone, so a deep, critical node is shown the same history as a
one-constant fix -- and that history is shaped by which nodes went where. Each
candidate therefore also carries ``outcomes_at_requirement``: the same gate
vocabulary over the rows whose recorded requirement (the reasoning and risk a
dispatch recorded under ``node_definition.capability``) equals the request's,
with the review-total and wall-time medians over those rows. Rows that recorded
no requirement are counted under ``unrecorded`` and never matched, so history
written before a requirement was recorded reads as unknown rather than as a
match. The figures come from the one pass the pick already makes over its rows.
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
            id="requirement-node",
            goal="read outcomes at the requirement",
            plan="jev-learns-from-its-routes",
            role="implement",
            spec_level="exact",
            done_when="the block reads the matched rows",
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
    days_ago: float = 1.0,
    review_total: float | None = None,
    wall_seconds: float | None = None,
) -> dict[str, Any]:
    stamp = datetime.now(UTC) - timedelta(days=days_ago)
    row: dict[str, Any] = {
        "backend": backend,
        "agent": {"model": model},
        "gate": gate,
        "role": "implement",
        "spec_level": "exact",
        "completed_at": stamp.isoformat(),
    }
    if requirement is not None:
        row["node_definition"] = {"capability": {"requirements": dict(requirement)}}
    if review_total is not None:
        row["review"] = {"total": review_total}
    if wall_seconds is not None:
        row["wall_seconds"] = wall_seconds
    return row


def _records() -> list[dict[str, Any]]:
    """Two lanes: A's wins are at the wrong difficulty, B's are at the right one.

    Lane A passes four standard-reasoning rows and fails two deep-critical rows,
    so its pooled record looks strong while its record *at the requirement* is
    two failures. Lane B fails one standard row and passes three deep-critical
    rows, the reverse. One unrecorded row sits on lane A.
    """

    rows: list[dict[str, Any]] = []
    rows.extend(
        _row(backend="lanea", model="m-a", gate="passed", requirement=_STANDARD)
        for _ in range(4)
    )
    rows.extend(
        _row(backend="lanea", model="m-a", gate="failed", requirement=_DEEP_CRITICAL)
        for _ in range(2)
    )
    # A row written before any requirement was recorded: unrecorded, never matched.
    rows.append(_row(backend="lanea", model="m-a", gate="passed", requirement=None))
    rows.append(
        _row(backend="laneb", model="m-b", gate="failed", requirement=_STANDARD)
    )
    for index, review in enumerate((88.0, 92.0, 80.0)):
        rows.append(
            _row(
                backend="laneb",
                model="m-b",
                gate="passed",
                requirement=_DEEP_CRITICAL,
                review_total=review,
                wall_seconds=float(100 + 100 * index),
            )
        )
    return rows


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


def test_the_block_counts_only_the_matched_requirement(picker_env):
    """A's at-requirement record is its two deep, critical failures; B's is its
    three deep, critical passes, while both pooled records stay unchanged."""

    by_name = _by_backend(_candidates(_records(), picker_env))

    a_block = by_name["lanea"].outcomes_at_requirement
    assert a_block["passed"] == 0
    assert a_block["failed"] == 2
    assert by_name["lanea"].outcomes == {
        "passed": 5,
        "failed": 2,
        "not-run": 0,
        "unknown": 0,
    }

    b_block = by_name["laneb"].outcomes_at_requirement
    assert b_block["passed"] == 3
    assert b_block["failed"] == 0
    assert by_name["laneb"].outcomes == {
        "passed": 3,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }


def test_the_block_carries_the_review_and_wall_medians(picker_env):
    """The medians are read over the matched rows alone."""

    by_name = _by_backend(_candidates(_records(), picker_env))
    block = by_name["laneb"].outcomes_at_requirement
    assert block["review_total_median"] == 88.0
    assert block["wall_seconds_median"] == 200.0
    # Lane A matched two failed rows that recorded neither figure.
    assert by_name["lanea"].outcomes_at_requirement["review_total_median"] is None
    assert by_name["lanea"].outcomes_at_requirement["wall_seconds_median"] is None


def test_a_row_with_no_recorded_requirement_counts_that_way_only(picker_env):
    """The unrecorded row is counted under unrecorded and nowhere else."""

    by_name = _by_backend(_candidates(_records(), picker_env))
    a_block = by_name["lanea"].outcomes_at_requirement
    assert a_block["unrecorded"] == 1
    # The unrecorded pass does not leak into the matched counts.
    assert a_block["passed"] == 0
    assert by_name["laneb"].outcomes_at_requirement["unrecorded"] == 0


def test_a_different_requirement_is_neither_matched_nor_unrecorded(picker_env):
    """Lane A's four standard passes match neither the deep-critical filter nor
    the unrecorded count -- they recorded a requirement, just not this one."""

    by_name = _by_backend(_candidates(_records(), picker_env))
    a_block = by_name["lanea"].outcomes_at_requirement
    assert a_block["unrecorded"] == 1  # only the requirement-less row
    assert a_block["passed"] + a_block["failed"] == 2


def test_the_rendered_state_carries_the_block_and_its_glossary_entry(picker_env):
    """The state Jev is shown carries the block, and the glossary defines it."""

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
    lane_b = state["candidates"]["laneb"]
    assert "outcomes_at_requirement" in lane_b
    assert lane_b["outcomes_at_requirement"]["passed"] == 3

    glossary = json.loads(prompts.render("questions.jinja", candidates=candidates))[
        "route"
    ]["glossary"]
    assert "outcomes_at_requirement" in glossary
    assert "not a calibrated ranking" in glossary["outcomes_at_requirement"]


def test_the_block_adds_no_second_pass_over_the_rows(picker_env, monkeypatch):
    """The scan is called exactly once per pick, so the block costs no second walk."""

    calls = {"count": 0}
    original = snapshot._candidates_scan

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshot, "_candidates_scan", counting)
    _candidates(_records(), picker_env)
    assert calls["count"] == 1
