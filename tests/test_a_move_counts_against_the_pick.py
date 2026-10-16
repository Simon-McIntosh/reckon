"""A coordinator's move off Jev's pick counts against the pick, not the lane.

A run Jev routed to one lane that a coordinator then moved to another is
credited to the lane it landed on as if that lane had been chosen for it: its
lineage records the move, and nothing that builds Jev's evidence reads it. So a
row whose lineage records a lane change away from the backend its
``picker_selection`` chose is a move: its outcome is removed from the receiving
candidate's ``outcomes`` and ``outcomes_at_requirement`` and tallied under that
candidate's ``moved_in``, while the candidate Jev chose gains ``moved_off``, one
per such pick. A row with a lane change but no pick (an explicit route that was
later moved) has no pick to count against and counts under its final lane as
before. Both move figures are counted at the request's requirement: a moved row
recorded at another requirement counts in neither, and a request naming no
requirement counts every move.
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
    "local_backend": "clive",
    "roles": {"implement": {}, "review": {}},
    "backends": {
        "clive": {"model": "m-clive"},
        "codex": {"model": "m-codex"},
    },
}

_DEEP_CRITICAL = {"reasoning": "deep", "risk": "critical"}
_SHALLOW_LOW = {"reasoning": "standard", "risk": "low"}


def _request(requirements: dict[str, str] | None = _DEEP_CRITICAL) -> PickRequest:
    """A request naming no requirement is one whose capability names none."""

    return PickRequest(
        project="reckon",
        node=TaskNode(
            id="move-node",
            goal="count a move against the pick",
            plan="jev-learns-from-its-routes",
            role="implement",
            spec_level="exact",
            done_when="the move is counted against the pick",
            write_paths=[],
        ),
        session="s",
        capability={} if requirements is None else {"requirements": dict(requirements)},
    )


def _selection(backend: str, model: str) -> dict[str, Any]:
    """The pick Jev rendered for a run: the backend and model it chose."""

    return {"action": "route", "backend": backend, "model": model}


def _lineage(chosen: str, landed: str) -> dict[str, Any]:
    """A recorded lane change away from the picked backend."""

    return {
        "kind": "lane-change",
        "lanes": [
            {
                "from_backend": chosen,
                "to_backend": landed,
                "reason": "coordinator moved the node onto another lane",
            }
        ],
    }


def _row(
    *,
    backend: str,
    model: str,
    gate: str,
    requirement: dict[str, str] | None,
    picker_selection: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    days_ago: float = 1.0,
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
    if picker_selection is not None:
        row["picker_selection"] = picker_selection
    if lineage is not None:
        row["lineage"] = lineage
    return row


def _records() -> list[dict[str, Any]]:
    """Three rows: a pick that stayed, a pick that was moved, and a plain run.

    The first is a clive run Jev routed there that passed on clive. The second
    is a clive pick a coordinator moved to codex with a reason, and it failed.
    The third is a codex run Jev routed there that also passed.
    """

    return [
        _row(
            backend="clive",
            model="m-clive",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("clive", "m-clive"),
        ),
        _row(
            backend="codex",
            model="m-codex",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("clive", "m-clive"),
            lineage=_lineage("clive", "codex"),
        ),
        _row(
            backend="codex",
            model="m-codex",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("codex", "m-codex"),
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


def test_the_move_is_counted_against_the_pick_not_the_lane(picker_env):
    """Clive keeps only its own pick and gains moved_off; codex keeps only its
    own run and gains the moved pick under moved_in."""

    by_name = _by_backend(_candidates(_records(), picker_env))

    clive = by_name["clive"]
    assert clive.outcomes == {
        "passed": 1,
        "failed": 0,
        "not-run": 0,
        "unknown": 0,
    }
    assert clive.moved_off == 1

    codex = by_name["codex"]
    assert codex.outcomes == {
        "passed": 1,
        "failed": 0,
        "not-run": 0,
        "unknown": 0,
    }
    assert codex.moved_in == {
        "passed": 0,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }


def test_the_move_is_removed_from_the_at_requirement_block(picker_env):
    """The moved row leaves the receiving candidate's at-requirement block too."""

    by_name = _by_backend(_candidates(_records(), picker_env))

    assert by_name["clive"].outcomes_at_requirement["passed"] == 1
    assert by_name["clive"].outcomes_at_requirement["failed"] == 0
    assert by_name["codex"].outcomes_at_requirement["passed"] == 1
    assert by_name["codex"].outcomes_at_requirement["failed"] == 0


def test_a_lane_change_without_a_pick_counts_under_its_final_lane(picker_env):
    """An explicit route that was later moved has no pick to count against, so
    it counts under the lane it landed on as before."""

    rows = [
        _row(
            backend="codex",
            model="m-codex",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("codex", "m-codex"),
        ),
        # An explicit-route run moved to codex, with no picker selection.
        _row(
            backend="codex",
            model="m-codex",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            lineage=_lineage("clive", "codex"),
        ),
    ]
    by_name = _by_backend(_candidates(rows, picker_env))
    codex = by_name["codex"]
    assert codex.outcomes == {
        "passed": 1,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }
    # No pick was moved in, so moved_in and moved_off carry no move.
    assert codex.moved_in == {
        "passed": 0,
        "failed": 0,
        "not-run": 0,
        "unknown": 0,
    }
    assert codex.moved_off == 0


def test_the_rendered_state_carries_the_move_figures_and_the_glossary(picker_env):
    """The state Jev is shown carries moved_in and moved_off, and the glossary
    defines both."""

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
    assert state["candidates"]["clive"]["moved_off"] == 1
    assert state["candidates"]["codex"]["moved_in"]["failed"] == 1

    glossary = json.loads(prompts.render("questions.jinja", candidates=candidates))[
        "route"
    ]["glossary"]
    assert "moved_in" in glossary
    assert "moved_off" in glossary


def test_a_request_naming_no_requirement_renders_the_move_figures(picker_env):
    """A pick that names no requirement still renders the move figures.

    There is no at-requirement block to carry them, so the receiving
    candidate's ``moved_in`` and the chosen candidate's ``moved_off`` must stand
    on their own -- otherwise a node dispatched with no declared capability,
    such as a brief, shows Jev no coordinator moves at all.
    """

    rows = [
        _row(
            backend="codex",
            model="m-codex",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("clive", "m-clive"),
            lineage=_lineage("clive", "codex"),
        ),
    ]
    candidates = [
        candidate
        for candidate in _candidates(
            rows, picker_env, request=_request(requirements=None)
        )
        if not candidate.reasons
    ]
    state = prompts.build_state(
        node=_request(requirements=None).node,
        capability={},
        estimated_context=0,
        comment="",
        candidates=candidates,
        lane={"return_times": {}, "lanes": {}, "local_lane": {}},
        attempts=0,
    )
    codex = state["candidates"]["codex"]
    clive = state["candidates"]["clive"]
    # No requirement was named, so no at-requirement block is rendered...
    assert "outcomes_at_requirement" not in codex
    # ...yet the move figures are, read over every move.
    assert codex["moved_in"]["failed"] == 1
    assert clive["moved_off"] == 1


def test_ignoring_lineage_leaves_the_move_counted_for_the_lane(picker_env, monkeypatch):
    """The negative control's fix: with move detection off, the moved row counts
    under the receiving lane and no moved_in is reported."""

    monkeypatch.setattr(snapshot, "_picked_backend_away", lambda row: None)
    by_name = _by_backend(_candidates(_records(), picker_env))

    codex = by_name["codex"]
    assert codex.outcomes == {
        "passed": 1,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }
    assert codex.moved_in == {
        "passed": 0,
        "failed": 0,
        "not-run": 0,
        "unknown": 0,
    }


def test_a_move_at_another_requirement_counts_in_neither_figure(picker_env):
    """Both moved_in and moved_off are read at the request's requirement, so a
    move recorded at another requirement raises neither: the receiving
    candidate's moved_in keeps only the at-requirement move and the chosen
    candidate's moved_off matches it."""

    rows = [
        # A clive pick a coordinator moved to codex, at the request's requirement.
        _row(
            backend="codex",
            model="m-codex",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("clive", "m-clive"),
            lineage=_lineage("clive", "codex"),
        ),
        # A clive pick moved to codex at a different requirement.
        _row(
            backend="codex",
            model="m-codex",
            gate="passed",
            requirement=_SHALLOW_LOW,
            picker_selection=_selection("clive", "m-clive"),
            lineage=_lineage("clive", "codex"),
        ),
    ]
    by_name = _by_backend(_candidates(rows, picker_env))

    assert by_name["codex"].moved_in == {
        "passed": 0,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }
    assert by_name["clive"].moved_off == 1


def test_a_request_naming_no_requirement_counts_every_move(picker_env):
    """A request with no requirement has no difficulty to read at, so both
    moved_in and moved_off count every moved row."""

    rows = [
        _row(
            backend="codex",
            model="m-codex",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            picker_selection=_selection("clive", "m-clive"),
            lineage=_lineage("clive", "codex"),
        ),
        _row(
            backend="codex",
            model="m-codex",
            gate="passed",
            requirement=_SHALLOW_LOW,
            picker_selection=_selection("clive", "m-clive"),
            lineage=_lineage("clive", "codex"),
        ),
    ]
    by_name = _by_backend(
        _candidates(rows, picker_env, request=_request(requirements=None))
    )

    assert by_name["codex"].moved_in == {
        "passed": 1,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }
    assert by_name["clive"].moved_off == 2
