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

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon.crew import picker
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