"""Transition gates preserve execution readiness and guard terminal writes."""

from __future__ import annotations

from copy import deepcopy

import pytest

from reckon import _store
from reckon._plan_html import read_state, write_state
from reckon._schema import PlanState
from reckon.lifecycle import TERMINAL_STATUSES
from reckon.roadmap import build_roadmap


def _gate(*, transition="plan-terminal", ref="producer#outcome", verdict=""):
    gate = {
        "id": "outcome-gate",
        "section": "implementation",
        "gated_sections": ["release"],
        "transition": transition,
        "gating_plan": ref,
        "measure": "The producer records its outcome",
        "verdict": verdict,
    }
    if transition == "decision-lockable":
        gate["decision"] = "accept-outcome"
    return gate


def _plan(gate=None):
    return {
        "project": "sample",
        "slug": "consumer",
        "title": "Consumer",
        "type": "plan",
        "status": "active",
        "version": 0,
        "gates": [gate or _gate()],
        "followups": [
            {"id": "continue", "status": "open", "prompt": "/reckon-build consumer"}
        ],
    }


@pytest.mark.parametrize(
    "ref", ["producer", "producer#outcome", "foreign:producer#outcome"]
)
@pytest.mark.parametrize("transition", ["plan-terminal", "decision-lockable"])
def test_transition_gate_round_trips_through_ops_schema_and_html(ref, transition):
    plan = _plan()
    plan["gates"] = []
    if transition == "decision-lockable":
        plan["decisions"] = {"accept-outcome": {"title": "Accept the outcome?"}}
    _store.apply_ops(
        plan, [{"op": "gate", **_gate(transition=transition, ref=ref)}], False
    )
    canonical = PlanState.model_validate(plan).validate_for_write().canonical_dump()
    html = write_state(_store.new_plan_html("sample", "consumer"), canonical)
    parsed = read_state(html)["gates"][0]
    assert parsed["transition"] == transition
    assert parsed["gating_plan"] == ref
    assert parsed["gated_sections"] == ["release"]
    if transition == "decision-lockable":
        assert parsed["decision"] == "accept-outcome"
    assert read_state(write_state(html, read_state(html)))["gates"] == [parsed]


@pytest.mark.parametrize("verdict", ["", "passed", "failed"])
def test_closure_gate_leaves_plan_ready_and_dispatchable(verdict):
    plan = _plan(_gate(verdict=verdict))
    result = build_roadmap("sample", [plan], [])
    assert [row["slug"] for row in result["ready_now"]] == ["consumer"]
    row = result["ready_now"][0]
    assert row["dispatchable"] is True
    assert row["effective_status"] == "active"
    assert row["gate_blockers"] == []
    assert len(row["closure_blockers"]) == (0 if verdict else 1)
    if not verdict:
        assert row["closure_blockers"][0]["gating_plan"] == "producer#outcome"


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_refused_terminal_write(status, tmp_path):
    plan = _plan()
    path = tmp_path / "docs/plans/consumer.html"
    path.parent.mkdir(parents=True)
    path.write_text(write_state(_store.new_plan_html("sample", "consumer"), plan))
    evidence = tmp_path / "docs/evidence/archive/consumer-landed.html"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="evidence">'
        '<meta name="plan-evidence-for" content="consumer">'
    )
    before = path.read_bytes()
    closing = {**plan, "status": status}
    with pytest.raises(_store.OpError, match=r"outcome-gate.*producer#outcome"):
        _store._write_state("sample", "consumer", closing, 0, root=tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("verdict_op", ["pass", "fail"])
def test_recorded_verdict_allows_same_terminal_write(verdict_op, tmp_path):
    plan = _plan()
    path = tmp_path / "docs/plans/consumer.html"
    path.parent.mkdir(parents=True)
    path.write_text(write_state(_store.new_plan_html("sample", "consumer"), plan))
    evidence = tmp_path / "docs/evidence/archive/consumer-landed.html"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="evidence">'
        '<meta name="plan-evidence-for" content="consumer">'
    )
    _store.apply_ops(
        plan, [{"op": verdict_op, "id": "outcome-gate", "evidence": "receipt"}], False
    )
    plan["status"] = "done"
    assert _store._write_state("sample", "consumer", plan, 0, root=tmp_path) == 1
    assert read_state(path.read_text())["status"] == "done"


def test_ops_and_patch_boundaries_refuse_terminal_transition():
    plan = _plan()
    with pytest.raises(_store.OpError, match=r"outcome-gate.*producer#outcome"):
        _store.apply_ops(
            deepcopy(plan), [{"op": "set", "path": "status", "value": "done"}], False
        )
    with pytest.raises(_store.OpError, match=r"outcome-gate.*producer#outcome"):
        _store.validate_landing_patch({**plan, "status": "done"}, {"status": "done"})


def test_foreign_outcome_appears_in_decision_blockers():
    plan = _plan(_gate(transition="decision-lockable", ref="foreign:producer#outcome"))
    plan["decisions"] = {"accept-outcome": {"title": "Accept the outcome?"}}
    report = build_roadmap("sample", [plan], [])
    assert len(report["decision_blockers"]) == 1
    blocker = report["decision_blockers"][0]
    assert blocker["id"] == "accept-outcome"
    assert blocker["gating_plan"] == "foreign:producer#outcome"
    assert blocker["gate_id"] == "outcome-gate"
    assert blocker["transition"] == "decision-lockable"


def test_decision_lock_waits_for_verdict_and_never_chooses_automatically():
    plan = _plan(_gate(transition="decision-lockable"))
    plan["decisions"] = {"accept-outcome": {"title": "Accept the outcome?"}}
    lock = {"op": "lock", "key": "accept-outcome", "choice": "accept"}
    with pytest.raises(_store.OpError, match=r"outcome-gate.*producer#outcome"):
        _store.apply_ops(deepcopy(plan), [lock], False)
    _store.apply_ops(
        plan, [{"op": "fail", "id": "outcome-gate", "evidence": "receipt"}], False
    )
    assert not plan["decisions"]["accept-outcome"].get("choice")
    report = build_roadmap("sample", [plan], [])
    assert report["decision_blockers"][0]["id"] == "accept-outcome"
    assert not report["decision_blockers"][0].get("gating_plan")
    _store.apply_ops(plan, [lock], False)
    assert plan["decisions"]["accept-outcome"]["choice"] == "accept"


def test_legacy_gate_still_blocks_execution():
    gate = _gate()
    del gate["transition"], gate["gating_plan"]
    report = build_roadmap("sample", [_plan(gate)], [])
    assert not report["ready_now"]
    assert report["pending_work"][0]["gate_blockers"][0]["id"] == "outcome-gate"


@pytest.mark.parametrize(
    "fields",
    [
        {"transition": "unknown"},
        {"gating_plan": ""},
        {"gating_plan": "producer##outcome"},
        {"transition": "decision-lockable", "decision": ""},
    ],
)
def test_malformed_transition_gate_is_refused(fields):
    gate = {**_gate(), **fields}
    with pytest.raises(ValueError, match="gate"):
        PlanState.model_validate(_plan(gate)).validate_for_write()
    with pytest.raises(_store.OpError, match="gate"):
        _store.apply_ops({**_plan(), "gates": []}, [{"op": "gate", **gate}], False)


def test_unrecognised_verdict_cannot_release_closure():
    plan = _plan(_gate(verdict="pending"))
    with pytest.raises(_store.OpError, match=r"outcome-gate.*producer#outcome"):
        _store.apply_ops(
            plan, [{"op": "set", "path": "status", "value": "done"}], False
        )


def test_closure_gate_does_not_add_followup_requirement():
    plan = _plan()
    plan["followups"] = []
    report = build_roadmap("sample", [plan], [])
    assert report["ready_now"][0]["dispatchable"] is True
