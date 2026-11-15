"""A dispatch waits for the router's answer, and a route it never gave is named.

The pick is asked inside a bound the dispatcher sets. A pick that answers inside
it routes the dispatch where it answered; a pick that never answers leaves
dispatch with no route it weighed, so the request is stored and asked again when
it launches instead of being placed on the configured default. A pick that fails
rather than times out still falls back, and that fallback names the step it gave
up on, so a default route is never a silent one.
"""

from __future__ import annotations

import importlib
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import dispatch_picker, dispatch_plan, runs
from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")

SECTION_HOURS = 6.0
DEEP_CAPABILITY = {
    "version": "1.0",
    "class": "orchestrator",
    "requirements": {
        "reasoning": "deep",
        "verification": "strict",
        "risk": "critical",
    },
}

PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="proj">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="probe">\n'
    "</head><body>\n"
    '<h2 id="s1">&sect;1 &mdash; A deep section</h2>\n'
    f'<section data-reckon="section" data-id="s1" data-effort-hours="{SECTION_HOURS}"'
    ' data-status="implementable"'
    ' data-capability-version="1.0" data-capability-class="orchestrator"'
    ' data-capability-reasoning="deep" data-capability-verification="strict"'
    ' data-capability-risk="critical"></section>\n'
    "</body></html>\n"
)


def _repo(tmp_path: Path) -> Path:
    plans = tmp_path / "repo" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "probe.html").write_text(PLAN_HTML)
    return tmp_path / "repo"


def _config(bound: float) -> dict:
    return {
        "default_backend": "local",
        "local_backend": "local",
        "roles": {"implement": {}},
        "routing": {"picker": "route", "picker_timeout_seconds": bound},
        "backends": {
            "local": {"launch": "cli", "command": "clive", "model": "local-model"},
            "metered": {"launch": "cli", "command": "codex", "model": "gpt-6-lane"},
        },
    }


def _node() -> TaskNode:
    return TaskNode(
        id="waits-node",
        goal="Produce the deliverable",
        plan="probe",
        section="s1",
        role="implement",
        spec_level="guided",
        done_when="pytest reports passed",
        write_paths=["src/output.py"],
        estimated_hours=None,
    )


def _answer(backend: str) -> dict:
    return {
        "action": "route",
        "backend": backend,
        "family": backend,
        "model": "gpt-6-lane",
        "effort": "high",
        "probabilities": {backend: 0.9, "hold": 0.1},
        "confidence": 0.9,
        "offered": [],
        "excluded": [],
    }


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _install_pick(monkeypatch: pytest.MonkeyPatch, behaviour):
    """Stub the pick, recording every request handed to it."""
    seen: list = []

    def stub_pick(request, config, **_kwargs):
        seen.append(request)
        return SimpleNamespace(as_dict=lambda: behaviour(request, config))

    monkeypatch.setattr(picker, "pick", stub_pick)
    return seen


def _select(repo: Path, config: dict) -> dict:
    return dispatch_picker.dispatch_picker_selection(
        node=_node(),
        config=config,
        project="proj",
        repo=repo,
        session="coordinator-a",
        records=[],
        verdict_inputs={},
        budget_snapshot={},
        authority={"plan": None},
    )


def test_the_declared_capability_reaches_the_pick_without_a_plan_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch resolves the section's difficulty, so the pick reads nothing."""
    repo = _repo(tmp_path)
    config = _config(bound=30.0)
    capability, hours = dispatch_picker.dispatcher_declared_difficulty(
        node=_node(), config=config, project="proj", repo=repo, authority=None
    )
    assert capability == DEEP_CAPABILITY
    assert hours == SECTION_HOURS

    seen = _install_pick(monkeypatch, lambda request, _config: _answer("metered"))
    _select(repo, config)
    assert len(seen) == 1
    request = seen[0]
    assert request.capability == DEEP_CAPABILITY
    assert request.estimated_hours == SECTION_HOURS

    # The pick's own difficulty step resolves nothing for a request carrying
    # both facts: a plan read there would raise, and none does.
    def refuse(*_a, **_k):
        raise AssertionError(
            "the pick read the plan it was handed; the dispatcher had resolved both"
        )

    monkeypatch.setattr(picker, "_resolved_plan_path", refuse)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.routing"), "_estimated_hours", refuse
    )
    resolved, effort, source = picker._declared_difficulty(
        request, dict(config), repo=repo, authority=None
    )
    assert source == "request"
    assert resolved == DEEP_CAPABILITY
    assert effort == SECTION_HOURS


def test_a_pick_that_answers_inside_the_bound_routes_where_it_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer after the old five-second mark is the answer dispatch keeps."""
    repo = _repo(tmp_path)
    config = _config(bound=30.0)

    def slow_answer(_request, _config):
        time.sleep(8)
        return _answer("metered")

    _install_pick(monkeypatch, slow_answer)
    started = time.monotonic()
    selection = _select(repo, config)
    elapsed = time.monotonic() - started

    assert elapsed >= 8
    assert selection["action"] == "route"
    assert selection["backend"] == "metered"
    assert "awaiting_pick" not in selection
    assert "route_defaulted" not in selection


def test_a_pick_that_never_answers_is_queued_awaiting_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home: Path
) -> None:
    """No route means no default: the request waits and is picked at launch."""
    repo = _repo(tmp_path)
    config = _config(bound=1.0)
    release = threading.Event()

    def never_answers(_request, _config):
        release.wait(30)
        return _answer("metered")

    _install_pick(monkeypatch, never_answers)
    try:
        selection = _select(repo, config)
    finally:
        release.set()

    assert selection["action"] == "fallback"
    assert selection["backend"] is None
    marker = selection["awaiting_pick"]
    assert marker["reason"] == "awaiting-pick"
    assert marker["step"]
    assert marker["bound_seconds"] == pytest.approx(1.0)
    assert "route_defaulted" not in selection

    resolution = SimpleNamespace(
        node=_node(),
        project="proj",
        run_id="r-waits",
        backend="local",
        launch="cli",
        local=False,
    )
    held = dispatch_plan._awaiting_pick_hold(
        resolution,
        picker_selection=selection,
        config=config,
        repo=repo,
        session="coordinator-a",
        comment="",
    )
    assert held.queued["phase"] == "queued"
    assert held.queued["reason"] == "awaiting-pick"
    pointer = runs.read_pointer("r-waits")
    assert pointer["phase"] == "queued"
    assert str(pointer["reason"]).startswith("awaiting-pick")
    stored = runs.dispatch_field(pointer, "picker_selection")
    assert stored["awaiting_pick"]["step"] == marker["step"]

    # A stored request whose pick never answered is picked again rather than
    # launched on the default, exactly as a stale route is.
    decision = dispatch_picker.launch_route_decision(
        pointer,
        stored=stored,
        config=config,
        destination="",
        now=datetime.now(UTC),
    )
    assert decision["decision"] == "repick"


def test_a_failure_fallback_names_the_step_the_default_route_ran_out_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A picker failure falls back, and the fallback says which step failed."""
    repo = _repo(tmp_path)

    def broken(_request, _config):
        raise RuntimeError("the router is unreachable")

    _install_pick(monkeypatch, broken)
    selection = _select(repo, _config(bound=30.0))

    assert selection["action"] == "fallback"
    defaulted = selection["route_defaulted"]
    assert defaulted["reason"] == "route-defaulted"
    assert defaulted["step"]
    assert "the router is unreachable" in defaulted["detail"]
    assert "awaiting_pick" not in selection
