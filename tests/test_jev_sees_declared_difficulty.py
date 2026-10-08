"""Jev sees the capability and estimate a node's plan section declares.

A pick is handed a node and the section it names, not the difficulty that
section declares, so a deep, critical node reached Jev as one it never saw -- an
empty capability and no estimate -- and was routed like an easy node. These
tests fix that the picker reads the declaration from the plan the node names:
the section's effective capability, and the node's own estimate else its
section's declared effort else the plan total. A value the request or the node
already carries is kept, the source is always rendered (null when nothing
resolved), and the read is paid for inside its own timed stage.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")

#: A minimal live plan whose total effort differs from every section's own, so
#: a test can tell which figure reached the state. Section 2 declares effort
#: with a capability; section 3 declares a different effort.
PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="{project}">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="{plan_slug}">\n'
    '<meta name="plan-effort-hours" content="{plan_hours}">\n'
    "</head><body>\n"
    '<h2 id="s2">&sect;2 &mdash; A deep section</h2>\n'
    '<section data-reckon="section" data-id="s2" data-effort-hours="10"'
    ' data-status="implementable"'
    ' data-capability-version="1.0" data-capability-class="orchestrator"'
    ' data-capability-reasoning="deep" data-capability-verification="strict"'
    ' data-capability-risk="critical"></section>\n'
    '<h2 id="s3">&sect;3 &mdash; A lighter section</h2>\n'
    '<section data-reckon="section" data-id="s3" data-effort-hours="4"'
    ' data-status="implementable"'
    ' data-capability-version="1.0" data-capability-class="orchestrator"'
    ' data-capability-reasoning="deep" data-capability-verification="strict"'
    ' data-capability-risk="critical"></section>\n'
    "</body></html>\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    plans = tmp_path / "repo" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "probe.html").write_text(
        PLAN_HTML.format(project="proj", plan_slug="probe", plan_hours="48")
    )
    return tmp_path / "repo"


def _config() -> dict:
    return {
        "default_backend": "local",
        "local_backend": "local",
        "roles": {"implement": {}},
        "backends": {
            "local": {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
                "effort": "high",
            }
        },
    }


def _node(**overrides) -> TaskNode:
    values = {
        "id": "work",
        "goal": "Implement the parser",
        "plan": "probe",
        "section": "s2",
        "role": "implement",
        "spec_level": "guided",
        "done_when": "Parser tests pass",
        "write_paths": ["src/parser.py"],
        "estimated_hours": None,
    }
    values.update(overrides)
    return TaskNode(**values)


def _candidate() -> picker.Candidate:
    return picker.Candidate(
        backend="local",
        family="local",
        model="local-model",
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
        reasons=[],
    )


def _answer(candidate: picker.Candidate) -> dict:
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


def _state_for_jev(
    repo: Path,
    request: picker.PickRequest,
    monkeypatch: pytest.MonkeyPatch | None = None,
    home: Path | None = None,
) -> dict:
    """Run a pick and return the state the caller handed to Jev.

    The real render runs, so the mapping asserted on is exactly the one Jev
    reads; only the candidate scan and the Jev call itself are stubbed.
    """

    if home is not None and monkeypatch is not None:
        monkeypatch.setenv("RECKON_HOME", str(home))
    seen: list[dict] = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return _answer(_candidate())

    selection = picker.pick(
        request,
        _config(),
        repo=repo,
        records=[],
        budget_snapshot={},
        snapshotter=lambda *a, **k: [_candidate()],
        caller=caller,
    )
    assert selection.action == "route"
    assert seen, "the picker handed Jev no state"
    return seen[0]


def test_the_declared_capability_and_section_effort_reach_jev(repo: Path) -> None:
    """A node with no capability of its own is judged at its section's.

    The plan's total effort is 48 and the section's own is 10; the figure the
    state carries is the section's, labelled as the section's.
    """

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(), estimated_context=1000),
    )["node"]
    assert node["capability"]["class"] == "orchestrator"
    assert node["capability"]["requirements"] == {
        "reasoning": "deep",
        "verification": "strict",
        "risk": "critical",
    }
    assert node["estimated_hours"] == 10.0
    assert node["estimated_hours_source"] == "section"


def test_the_section_effort_wins_over_the_plan_total(repo: Path) -> None:
    """A node scoped to a second section carries that section's own effort."""

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(section="s3"), estimated_context=1000),
    )["node"]
    assert node["estimated_hours"] == 4.0
    assert node["estimated_hours_source"] == "section"


def test_a_section_the_plan_does_not_record_falls_back_to_the_plan(
    repo: Path,
) -> None:
    """A section the plan does not record leaves the plan's total standing."""

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(section="s9"), estimated_context=1000),
    )["node"]
    assert node["estimated_hours"] == 48.0
    assert node["estimated_hours_source"] == "plan"


def test_a_capability_the_request_carries_is_kept(repo: Path) -> None:
    """A request that already states a capability is never re-resolved."""

    request = picker.PickRequest(
        "proj",
        _node(estimated_hours=2.5),
        capability={"class": "general", "requirements": {"risk": "low"}},
        estimated_context=1000,
    )
    node = _state_for_jev(repo, request)["node"]
    assert node["capability"] == {"class": "general", "requirements": {"risk": "low"}}
    assert node["estimated_hours"] == 2.5
    assert node["estimated_hours_source"] == "node"


def test_a_node_estimate_is_kept_when_the_capability_is_resolved(repo: Path) -> None:
    """The two facts resolve independently: a node hours figure survives."""

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(estimated_hours=3.0), estimated_context=1000),
    )["node"]
    assert node["capability"]["class"] == "orchestrator"
    assert node["estimated_hours"] == 3.0
    assert node["estimated_hours_source"] == "node"


def test_a_node_with_no_plan_renders_null(repo: Path) -> None:
    """No plan to read leaves the capability, estimate and source null."""

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(plan=""), estimated_context=1000),
    )["node"]
    assert node["capability"] is None
    assert node["estimated_hours"] is None
    assert node["estimated_hours_source"] is None


def test_a_node_with_no_section_keeps_its_plan_estimate(repo: Path) -> None:
    """With no section to read, the capability is null but the plan still
    estimates.
    """

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(section=""), estimated_context=1000),
    )["node"]
    assert node["capability"] is None
    assert node["estimated_hours"] == 48.0
    assert node["estimated_hours_source"] == "plan"


def test_the_source_is_present_null_when_nothing_resolves(repo: Path) -> None:
    """The node's shape does not vary: the source key is always rendered."""

    node = _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(section=""), estimated_context=1000),
    )["node"]
    assert "estimated_hours_source" in node


def test_the_resolution_is_timed_as_its_own_stage(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan read runs inside the pick and its cost is attributed."""

    home = tmp_path / "home"
    _state_for_jev(
        repo,
        picker.PickRequest("proj", _node(), estimated_context=1000),
        monkeypatch=monkeypatch,
        home=home,
    )
    lines = (home / "crew" / picker.PICK_TIMINGS_LOG).read_text().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["capability_ms"] is not None
    assert line["capability_ms"] >= 0
