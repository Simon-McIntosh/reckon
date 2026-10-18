"""A pick sees how many launches each outcome at its requirement took.

A promoted ledger row records ``attempt``: 1 for a node that passed on its
first launch, and 1 plus one for each resume. A blocking review finding is
answered by a resume, so the attempt count already covers the review rounds a
pass needed. Read without it, a pass on the seventh launch weighs exactly as a
pass on the first, so Jev cannot tell a lane that suits deep, critical work
from one whose passes only land after several resumes.

``_candidates_scan`` collects each row's ``attempt`` in the one pass it already
makes over the rows, beside the gate, review total and wall seconds, so the
at-requirement block gains ``attempts_median`` (the median launches those rows
took, null when none) and ``passed_first_attempt`` (how many of them passed on
the first launch). Nothing here routes or overrides a pick: it only changes
what Jev is shown.
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

_DEEP_CRITICAL = {"reasoning": "deep", "risk": "critical"}
_STANDARD = {"reasoning": "standard", "risk": "low"}


def _request(capability: dict[str, Any] | None = None) -> PickRequest:
    return PickRequest(
        project="reckon",
        node=TaskNode(
            id="attempt-node",
            goal="read the launches each outcome took",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="the block reads the launches at the requirement",
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
    attempt: Any = None,
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
    if attempt is not None:
        row["attempt"] = attempt
    return row


def _records() -> list[dict[str, Any]]:
    """Lane A matches four deep, critical rows; lane B matches none.

    Lane A at the requirement: a pass at attempt 1, a pass at attempt 7, a
    failure at attempt 3, and a pass that recorded no attempt. It also carries a
    standard-reasoning pass and a requirement-less pass, both at attempt 1,
    which must add to neither field. Lane B holds a standard-reasoning pass and
    a requirement-less pass alone, so it has no matched row at all.
    """

    return [
        _row(
            backend="lanea",
            model="m-a",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            attempt=1,
        ),
        _row(
            backend="lanea",
            model="m-a",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            attempt=7,
        ),
        _row(
            backend="lanea",
            model="m-a",
            gate="failed",
            requirement=_DEEP_CRITICAL,
            attempt=3,
        ),
        # A pass written before an attempt was recorded: no launch to count.
        _row(backend="lanea", model="m-a", gate="passed", requirement=_DEEP_CRITICAL),
        # A pass at another requirement and one at no requirement: both matched
        # by nothing here, so neither adds a launch.
        _row(
            backend="lanea",
            model="m-a",
            gate="passed",
            requirement=_STANDARD,
            attempt=1,
        ),
        _row(backend="lanea", model="m-a", gate="passed", requirement=None, attempt=1),
        _row(
            backend="laneb",
            model="m-b",
            gate="passed",
            requirement=_STANDARD,
            attempt=1,
        ),
        _row(backend="laneb", model="m-b", gate="passed", requirement=None, attempt=1),
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


def test_the_block_reads_the_launches_each_outcome_took(picker_env):
    """Lane A's three passes and one failure took attempts 1, 7, 3 and none:
    passed 3, attempts_median 3, and one pass on the first launch."""

    block = _by_backend(_candidates(_records(), picker_env))[
        "lanea"
    ].outcomes_at_requirement

    assert block["passed"] == 3
    assert block["failed"] == 1
    assert block["attempts_median"] == 3
    assert block["passed_first_attempt"] == 1


def test_a_candidate_with_no_matched_row_reads_no_launches(picker_env):
    """Lane B matched no row, so it reads null launches and no first-attempt pass."""

    block = _by_backend(_candidates(_records(), picker_env))[
        "laneb"
    ].outcomes_at_requirement

    assert block["passed"] == 0
    assert block["attempts_median"] is None
    assert block["passed_first_attempt"] == 0


def test_rows_at_another_requirement_add_to_neither_field(picker_env):
    """Lane A's standard-reasoning pass and requirement-less pass (both attempt
    1) are matched by nothing, so neither the median nor the first-attempt count
    moves: had they leaked in, passed would read 5, the median 1 and the
    first-attempt count 3."""

    block = _by_backend(_candidates(_records(), picker_env))[
        "lanea"
    ].outcomes_at_requirement

    assert block["unrecorded"] == 1
    assert block["passed"] == 3
    assert block["attempts_median"] == 3
    assert block["passed_first_attempt"] == 1


def test_a_launch_that_is_not_a_positive_integer_is_skipped(picker_env):
    """A matched pass at attempt 0, at a string attempt, and with no attempt: none
    is a positive integer, so none is a launch and none is a first-attempt pass."""

    records = [
        _row(
            backend="lanea",
            model="m-a",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            attempt=0,
        ),
        _row(
            backend="lanea",
            model="m-a",
            gate="passed",
            requirement=_DEEP_CRITICAL,
            attempt="4",
        ),
        _row(backend="lanea", model="m-a", gate="passed", requirement=_DEEP_CRITICAL),
    ]
    block = _by_backend(_candidates(records, picker_env))[
        "lanea"
    ].outcomes_at_requirement

    assert block["passed"] == 3
    assert block["attempts_median"] is None
    assert block["passed_first_attempt"] == 0


def test_the_rendered_state_carries_both_fields_and_the_glossary_names_them(picker_env):
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
    lane_a = state["candidates"][
        prompts.option_key(next(c for c in candidates if c.backend == "lanea"))
    ]["outcomes_at_requirement"]
    assert lane_a["attempts_median"] == 3
    assert lane_a["passed_first_attempt"] == 1

    glossary = json.loads(prompts.render("questions.jinja", candidates=candidates))[
        "route"
    ]["glossary"]
    entry = glossary["outcomes_at_requirement"]
    assert "attempts_median" in entry
    assert "passed_first_attempt" in entry
    assert "resume" in entry
