"""A refusal or fallback records which lanes were excluded, and why.

The pick-timings log names a refusal's outcome and a one-line reason but not
which lanes were turned away or on what ground, so a refusal whose node shape
is otherwise lost cannot be diagnosed from the log. ``_record_pick_timings``
now adds each excluded candidate's backend and model with its reason code and
the node's estimated context, on a ``refuse`` or ``fallback`` outcome only: a
routed pick had no excluded candidate, so it records neither field.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")

ESTIMATED_CONTEXT = 300000


def _node() -> TaskNode:
    return TaskNode(
        id="parser",
        plan="lane-and-model-routing",
        role="implement",
        spec_level="guided",
        goal="Implement a parser",
        done_when="Parser tests pass",
        time_budget="60m",
    )


def _request(session: str = "s-1") -> picker.PickRequest:
    return picker.PickRequest(
        "example",
        _node(),
        capability={"class": "general"},
        estimated_context=ESTIMATED_CONTEXT,
        comment="",
        session=session,
    )


def _candidate(
    backend: str = "local",
    model: str = "local-model",
    *,
    reasons: list[str] | None = None,
) -> picker.Candidate:
    return picker.Candidate(
        backend=backend,
        family="local",
        model=model,
        effort="high",
        local=True,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={},
        reasons=list(reasons or []),
    )


def _answer(candidate: picker.Candidate) -> dict[str, object]:
    key = picker.prompts.option_key(candidate)
    return {
        "model": "jev-snapshot",
        "answers": {
            "route": {
                "choice": key,
                "confidence": 0.9,
                "probabilities": {key: 0.9, "hold": 0.1},
            }
        },
    }


def _snapshotter(candidate_list: list[picker.Candidate]) -> object:
    def snapshot(request, config, repo, **kwargs):
        return list(candidate_list)

    return snapshot


def _stub_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def render(name: str, **context: object) -> str:
        if name == "state.jinja":
            return json.dumps({"node": {}, "candidates": []})
        return json.dumps({"route": {"criteria": []}})

    monkeypatch.setattr(picker.prompts, "render", render)


def _pick(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: list[picker.Candidate],
    caller,
):
    monkeypatch.setenv("RECKON_HOME", str(home))
    _stub_render(monkeypatch)
    return picker.pick(
        _request(),
        {"default_backend": "local"},
        repo=home,
        records=[],
        budget_snapshot={},
        snapshotter=_snapshotter(candidates),
        caller=caller,
    )


def _line(home: Path) -> dict[str, object]:
    lines = (home / "crew" / picker.PICK_TIMINGS_LOG).read_text().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_a_refused_pick_records_each_excluded_lane(tmp_path, monkeypatch):
    """Every excluded candidate is named with its reason code and the context."""
    home = tmp_path / "home"
    candidates = [
        _candidate("local", "local-model", reasons=["context-fit"]),
        _candidate(
            "codex-sub",
            "gpt-5.4-codex",
            reasons=[
                "reserved-lane: codex is reserved while the orchestrators need it"
            ],
        ),
        _candidate(
            "claude-sub",
            "claude-sonnet-5-5",
            reasons=["budget-ceiling: claude-sub has spent its five-hour window"],
        ),
    ]

    selection = _pick(
        home,
        monkeypatch,
        candidates=candidates,
        caller=lambda *a, **k: _answer(_candidate()),
    )

    assert selection.action == "refuse"
    line = _line(home)
    assert line["outcome"] == "refuse"
    assert line["estimated_context"] == ESTIMATED_CONTEXT
    assert line["exclusions"] == [
        {"backend": "local", "model": "local-model", "reason_code": "context-fit"},
        {
            "backend": "codex-sub",
            "model": "gpt-5.4-codex",
            "reason_code": "reserved-lane",
        },
        {
            "backend": "claude-sub",
            "model": "claude-sonnet-5-5",
            "reason_code": "budget-ceiling",
        },
    ]


def test_a_fallback_pick_records_each_excluded_lane(tmp_path, monkeypatch):
    """A fallback row carries the same exclusions a refusal does."""
    home = tmp_path / "home"
    candidates = [
        _candidate("local", "local-model", reasons=["context-fit"]),
        _candidate(
            "codex-sub",
            "gpt-5.4-codex",
            reasons=["availability: codex endpoint is not serving"],
        ),
        _candidate("local", "fallback-model"),
    ]

    def broken_caller(*args, **kwargs):
        raise RuntimeError("jev is down")

    selection = _pick(home, monkeypatch, candidates=candidates, caller=broken_caller)

    assert selection.action == "fallback"
    line = _line(home)
    assert line["outcome"] == "fallback"
    assert line["estimated_context"] == ESTIMATED_CONTEXT
    assert line["exclusions"] == [
        {"backend": "local", "model": "local-model", "reason_code": "context-fit"},
        {
            "backend": "codex-sub",
            "model": "gpt-5.4-codex",
            "reason_code": "availability",
        },
    ]


def test_a_routed_pick_records_no_exclusions(tmp_path, monkeypatch):
    """A routed pick adds neither field; the log grows only where it is read."""
    home = tmp_path / "home"
    offered = _candidate("local", "local-model")

    selection = _pick(
        home,
        monkeypatch,
        candidates=[offered],
        caller=lambda *a, **k: _answer(offered),
    )

    assert selection.action == "route"
    line = _line(home)
    assert line["outcome"] == "route"
    assert "exclusions" not in line
    assert "estimated_context" not in line
