"""A pick records each stage's time and a fallback names the stage that spent it.

A dispatch-path pick is asked for inside a bounded wait, and the stages it runs
are not all inside that bound: the ledger rows, the verdict inputs and the budget
snapshot are read once, the context estimate is measured, and the picker is
called. When the bound expires the recorded reason used to read ``timeout`` for
all of them, which says the picker was slow whether it was or not. These tests
make one stage outlast the bound and require the fallback to name that stage,
with the stages that did finish carrying their elapsed milliseconds, and require
a completed pick to carry every stage's time on its selection.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon.crew import picker
from reckon.crew.node import TaskNode
from reckon.crew.picker import Candidate
from reckon.crew.picker import snapshot as picker_snapshot
from reckon.crew.picker import outcomes


def _quiet_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the three dispatch-scope input readers with cheap stand-ins."""
    monkeypatch.setattr(
        dispatch_picker_module, "resolve_dispatch_authority", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        dispatch_picker_module,
        "resolve_dispatch_ledger_root",
        lambda *_a, **_k: Path("/tmp"),
    )
    monkeypatch.setattr(dispatch_picker_module, "_picker_ledger_rows", lambda *_a: [])
    monkeypatch.setattr(
        dispatch_picker_module, "_picker_verdict_inputs", lambda *_a: {}
    )


def test_a_slow_input_names_keeps_its_own_name(monkeypatch):
    """A slow budget snapshot is named, not reported as the picker timing out."""
    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)
    _quiet_inputs(monkeypatch)

    def slow_budget(*_args, **_kwargs):
        time.sleep(0.4)
        return {}

    monkeypatch.setattr(dispatch_picker_module, "_picker_budget_snapshot", slow_budget)

    started = time.monotonic()
    result = dispatch_picker_module.dispatch_picker_selection(
        node=SimpleNamespace(),
        config={},
        project="proj",
        repo=Path("/tmp"),
        stage_ms={},
    )
    assert time.monotonic() - started < 0.4
    assert result["action"] == "fallback"
    assert result["fallback_reason"] == "timeout in budget-snapshot"
    # The stages that finished before the deadline carry their elapsed ms; the
    # one still running carries none.
    assert result["stage_ms"]["records"] is not None
    assert result["stage_ms"]["verdict-inputs"] is not None
    assert result["stage_ms"]["budget-snapshot"] is None
    assert result["stage_ms"]["context-estimate"] is None


def test_a_completed_pick_carries_every_stage_time(monkeypatch):
    """A normal pick records all five stages' elapsed milliseconds."""
    _quiet_inputs(monkeypatch)
    monkeypatch.setattr(
        dispatch_picker_module, "_picker_budget_snapshot", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        picker_snapshot,
        "estimated_context_tokens",
        lambda *_a, **_k: 1234,
    )
    monkeypatch.setattr(
        picker,
        "pick",
        lambda *_a, **_k: SimpleNamespace(
            as_dict=lambda: {"action": "route", "backend": "beta"}
        ),
    )

    result = dispatch_picker_module.dispatch_picker_selection(
        node=SimpleNamespace(),
        config={},
        project="proj",
        repo=Path("/tmp"),
        stage_ms={},
    )
    assert result["action"] == "route"
    for stage in dispatch_picker_module.PICK_STAGES:
        assert isinstance(result["stage_ms"][stage], (int, float))


def test_a_production_pick_names_the_estimate_it_ran_out_in(monkeypatch, tmp_path):
    """A caller that builds the inputs and hands them in still names the stage.

    Production builds the three dispatcher-scope inputs outside the pick's bound
    and hands them in, so the stage the pick is inside when the deadline expires
    is the context estimate. The fallback must name it rather than read the bare
    ``timeout``, and the handed-in inputs' elapsed milliseconds must still be on
    the fallback.
    """
    _quiet_inputs(monkeypatch)
    monkeypatch.setattr(
        dispatch_picker_module, "_picker_budget_snapshot", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)

    def slow_estimate(*_args, **_kwargs):
        time.sleep(0.4)
        return 0

    monkeypatch.setattr(picker_snapshot, "estimated_context_tokens", slow_estimate)

    records, verdicts, budget, errors = dispatch_picker_module.build_picker_inputs(
        "proj", {}, tmp_path
    )
    assert not errors
    result = dispatch_picker_module.dispatch_picker_selection(
        node=SimpleNamespace(),
        config={},
        project="proj",
        repo=tmp_path,
        records=records,
        verdict_inputs=verdicts,
        budget_snapshot=budget,
    )
    assert result["action"] == "fallback"
    assert result["fallback_reason"] == "timeout in context-estimate"
    # The stage still running carries none; the inputs built by the caller carry
    # their measured times even though they ran outside this pick's bound.
    assert result["stage_ms"]["context-estimate"] is None
    for stage in ("records", "verdict-inputs", "budget-snapshot"):
        assert isinstance(result["stage_ms"][stage], (int, float))


def test_a_normal_production_pick_carries_every_stage_time(monkeypatch, tmp_path):
    """A completed production pick records every stage's elapsed milliseconds."""
    _quiet_inputs(monkeypatch)
    monkeypatch.setattr(
        dispatch_picker_module, "_picker_budget_snapshot", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(picker_snapshot, "estimated_context_tokens", lambda *_a, **_k: 7)
    monkeypatch.setattr(
        picker,
        "pick",
        lambda *_a, **_k: SimpleNamespace(
            as_dict=lambda: {"action": "route", "backend": "beta"}
        ),
    )
    records, verdicts, budget, errors = dispatch_picker_module.build_picker_inputs(
        "proj", {}, tmp_path
    )
    assert not errors
    result = dispatch_picker_module.dispatch_picker_selection(
        node=SimpleNamespace(),
        config={},
        project="proj",
        repo=tmp_path,
        records=records,
        verdict_inputs=verdicts,
        budget_snapshot=budget,
    )
    assert result["action"] == "route"
    for stage in dispatch_picker_module.PICK_STAGES:
        assert isinstance(result["stage_ms"][stage], (int, float)), stage


def test_a_failed_input_build_leaves_no_stale_stage_times(monkeypatch, tmp_path):
    """A build that failed part-way leaves nothing for the next pick to read."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("input build failed")

    _quiet_inputs(monkeypatch)
    monkeypatch.setattr(dispatch_picker_module, "_picker_verdict_inputs", boom)
    records, verdicts, budget, errors = dispatch_picker_module.build_picker_inputs(
        "proj", {}, tmp_path
    )
    assert errors
    result = dispatch_picker_module.dispatch_picker_selection(
        node=SimpleNamespace(),
        config={},
        project="proj",
        repo=tmp_path,
        records=records,
        verdict_inputs=verdicts,
        budget_snapshot=budget,
        input_errors=errors,
    )
    assert result["action"] == "fallback"
    assert dispatch_picker_module._BUILD_STAGE_MS.times is None


def test_outcomes_classify_a_staged_timeout_by_its_stage():
    """A summary separates a staged timeout from the bare unattributed one."""
    assert outcomes._fallback_kind("timeout in budget-snapshot") == (
        "timeout:budget-snapshot"
    )
    assert outcomes._fallback_kind("timeout in records") == "timeout:records"
    assert outcomes._fallback_kind("timeout") == "timeout"
    report = outcomes.summarize(
        {
            "demo": [
                {
                    "run_id": "one",
                    "picker_selection": {
                        "action": "fallback",
                        "fallback_reason": "timeout in budget-snapshot",
                    },
                },
                {
                    "run_id": "two",
                    "picker_selection": {
                        "action": "fallback",
                        "fallback_reason": "timeout",
                    },
                },
            ]
        },
        {},
    )
    assert report["mechanics"]["fallback_reasons"] == {
        "timeout:budget-snapshot": 1,
        "timeout": 1,
    }


def _candidate() -> Candidate:
    return Candidate(
        backend="beta",
        family="test",
        model="beta-model",
        effort="medium",
        local=False,
        availability="served",
        utilisation_pct=10,
        burn_multiple=1,
        pace_allowance=50,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={},
    )


def _node() -> TaskNode:
    return TaskNode(
        id="pick",
        plan="",
        role="implement",
        spec_level="guided",
        goal="g",
        done_when="done",
        time_budget="60m",
    )


def _fast_pick_collaborators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every pick step before the one under test with a cheap stand-in."""
    monkeypatch.setattr(
        picker_snapshot, "estimated_context_tokens", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        picker, "_declared_difficulty", lambda *_a, **_k: (None, None, None)
    )
    monkeypatch.setattr(picker.snapshot, "candidates", lambda *_a, **_k: [_candidate()])


def _render(source: str):
    """Render the state as valid JSON and the questions as valid JSON."""

    def render(name: str, **_kwargs) -> str:
        if name == "questions.jinja":
            return json.dumps({"route": {"criteria": {"hold": {}}}})
        return json.dumps({"note": source})

    return render


def test_a_state_render_that_outlasts_the_bound_is_named(monkeypatch):
    """A slow state render is named by pick()'s own step, not as the picker.

    The render, not the pick, is what outlasts the bound, so the real pick runs
    and marks the step it is inside. The fallback names that step in pick()'s
    vocabulary rather than reading the bare timeout the whole call used to
    record.
    """
    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)
    _quiet_inputs(monkeypatch)
    monkeypatch.setattr(
        dispatch_picker_module, "_picker_budget_snapshot", lambda *_a, **_k: {}
    )
    _fast_pick_collaborators(monkeypatch)

    def slow_state_render(name: str, **_kwargs) -> str:
        if name == "questions.jinja":
            return json.dumps({"route": {"criteria": {"hold": {}}}})
        time.sleep(0.4)
        return "state"

    monkeypatch.setattr(picker.prompts, "render", slow_state_render)

    result = dispatch_picker_module.dispatch_picker_selection(
        node=_node(),
        config={},
        project="proj",
        repo=Path("/tmp"),
        stage_ms={},
    )
    assert result["action"] == "fallback"
    assert result["fallback_reason"] == "timeout in pick:state-render"


def test_a_jev_call_that_outlasts_the_bound_is_named(monkeypatch):
    """The Jev call outlasting the bound is named as pick:jev, not the picker."""
    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)
    _quiet_inputs(monkeypatch)
    monkeypatch.setattr(
        dispatch_picker_module, "_picker_budget_snapshot", lambda *_a, **_k: {}
    )
    _fast_pick_collaborators(monkeypatch)
    monkeypatch.setattr(picker.prompts, "render", _render("state"))

    def slow_client(*_args, **_kwargs):
        time.sleep(0.4)
        return {}

    monkeypatch.setattr(picker.client, "ask", slow_client)

    result = dispatch_picker_module.dispatch_picker_selection(
        node=_node(),
        config={},
        project="proj",
        repo=Path("/tmp"),
        stage_ms={},
    )
    assert result["action"] == "fallback"
    assert result["fallback_reason"] == "timeout in pick:jev"


def test_record_picker_hold_takes_no_docs_parameter():
    """A hold is written under the crew home, so its recorder takes no ``docs``."""
    assert "docs" not in inspect.signature(outcomes.record_picker_hold).parameters


def test_both_dispatch_holds_are_recorded_without_docs():
    """Both hold-recording dispatch sites call the recorder, neither with docs."""
    dispatch = importlib.import_module("reckon.crew.dispatch")
    tree = ast.parse(Path(dispatch.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_picker_hold"
    ]
    assert len(calls) == 2
    for call in calls:
        assert "docs" not in {keyword.arg for keyword in call.keywords}
