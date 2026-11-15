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
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import dispatch_picker, dispatch_plan, runs
from reckon.crew.dispatch_admission import LaneHeld
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


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    """A committed checkout holding the plan, so a dispatch can resolve it."""
    repository = tmp_path / "repo"
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "probe.html").write_text(PLAN_HTML)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the plan fixture")
    return repository


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


def test_a_nodes_own_estimate_outranks_the_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node that states its own hours keeps them past the section's figure.

    The plan's section carries six hours; the node declares two. The dispatch
    resolves the node's own estimate because that is the one its author wrote,
    so a surface that overrides it with the section's figures the node's own
    statement as a stale copy.
    """
    repo = _repo(tmp_path)
    config = _config(bound=30.0)
    node = _node()

    stated = TaskNode(
        id=node.id,
        goal=node.goal,
        plan=node.plan,
        section=node.section,
        role=node.role,
        spec_level=node.spec_level,
        done_when=node.done_when,
        write_paths=list(node.write_paths),
        estimated_hours=2.0,
    )
    capability, hours = dispatch_picker.dispatcher_declared_difficulty(
        node=stated, config=config, project="proj", repo=repo, authority=None
    )
    assert capability == DEEP_CAPABILITY
    assert hours == 2.0


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


def test_a_failure_fallback_names_the_step_the_pick_ran_out_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pick that raises reports the step it died in, not a bare word.

    The real pick runs, so the step named here is the one its own sink recorded
    when the candidate scan raised; a fallback that reported a fixed word would
    fail this assertion whatever the pick did.
    """
    repo = _repo(tmp_path)

    def broken_scan(*_args, **_kwargs):
        raise RuntimeError("the lane census is unreachable")

    monkeypatch.setattr(picker.snapshot, "candidates", broken_scan)
    monkeypatch.setattr(
        picker.client,
        "ask",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("jev did not answer")),
    )
    selection = _select(repo, _config(bound=30.0))

    assert selection["action"] == "fallback"
    defaulted = selection["route_defaulted"]
    assert defaulted["reason"] == "route-defaulted"
    assert defaulted["step"] == "pick:snapshot"
    assert "the lane census is unreachable" in defaulted["detail"]
    assert "awaiting_pick" not in selection


def test_the_within_bound_flag_is_measured_against_the_flight_bound() -> None:
    """The telemetry flag reads the bound dispatch applies, not a constant."""
    from reckon.crew.dispatch_picker import PICKER_DISPATCH_TIMEOUT_SECONDS

    assert (
        picker._dispatch_bound_seconds({"routing": {"picker_timeout_seconds": 7}})
        == 7.0
    )
    assert picker._dispatch_bound_seconds({}) == PICKER_DISPATCH_TIMEOUT_SECONDS
    assert (
        picker._dispatch_bound_seconds({"routing": {"picker_timeout_seconds": "soon"}})
        == PICKER_DISPATCH_TIMEOUT_SECONDS
    )


def test_an_unanswered_pick_is_stored_by_the_queue_not_launched_on_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home: Path
) -> None:
    """The production branch that holds an unanswered pick is covered.

    The pick never answers, the plan is dispatched through the same call the
    live path uses, and the hold it raises is the assertion: the request is
    stored through the queue's own record shape with the marker on it, and no
    dispatch of the node happened. Deleting the branch that raises this hold
    leaves the request on the default lane and fails here.
    """
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
    assert selection.get("awaiting_pick")
    node = _node()
    node.time_budget = "25m"
    with pytest.raises(LaneHeld) as held:
        dispatch_plan.plan_dispatch(
            node=node,
            config=config,
            project="proj",
            repo=repo,
            base="HEAD",
            session="coordinator-a",
            route="picker",
            picker_selection=selection,
        )

    queued = held.value.queued
    assert queued["phase"] == "queued"
    assert queued["reason"] == "awaiting-pick"
    pointer = runs.read_pointer(queued["run_id"])
    assert pointer["phase"] == "queued"
    assert str(pointer["reason"]) == "awaiting-pick"
    record = runs.dispatch_field(pointer, "picker_selection")
    assert record["awaiting_pick"]["bound_seconds"] == pytest.approx(1.0)
    # The record is the queue's own shape, so it carries the keys every other
    # held request carries rather than a hand-built subset.
    assert set(pointer) >= {
        "due",
        "urgency",
        "dispatch_options",
        "queued_at",
        "hold",
    }


def test_a_default_route_is_listed_as_a_notice_naming_its_step() -> None:
    """The obligations view lists a run placed on a default route."""
    from reckon.crew import obligations as obligations_module

    assert obligations_module.ROUTE_DEFAULTED_DUTY_KIND in (
        obligations_module.NON_BLOCKING_DUTY_KINDS
    )
    assert "route_step" in obligations_module.DUTY_ROW_FIELDS
    row = {"run_id": "r-defaulted", "node": "n1", "plan": "probe", "session": "s"}
    pointer = {
        "run_id": "r-defaulted",
        "phase": "running",
        "picker_selection": {
            "action": "fallback",
            "route_defaulted": {
                "reason": "route-defaulted",
                "step": "pick:snapshot",
                "detail": "RuntimeError: the lane census is unreachable",
            },
        },
    }
    items = obligations_module._route_defaulted_items(
        [row], {"r-defaulted": pointer}, session="s", now=datetime.now(UTC)
    )
    assert len(items) == 1
    assert items[0]["kind"] == obligations_module.ROUTE_DEFAULTED_DUTY_KIND
    assert items[0]["route_step"] == "pick:snapshot"
    assert "the lane census is unreachable" in items[0]["reason"]


def test_the_obligations_view_lists_a_run_whose_route_defaulted(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's own assembly carries the defaulted-route notice.

    The duty is derived rather than projected from a recovery row, so it is
    listed by a step the view has to run. Replacing that step with a spy leaves
    the notice missing unless the assembly calls it, which is what makes this
    fail when the listing is dropped.
    """
    from reckon.crew import obligations as obligations_module

    marker = {
        "kind": obligations_module.ROUTE_DEFAULTED_DUTY_KIND,
        "run_id": "r-defaulted",
        "node": "n1",
        "plan": "probe",
        "age_seconds": 0,
        "next_command": "",
        "route_step": "pick:snapshot",
    }
    asked: list = []

    def spy(classified, pointers, *, session, now):
        asked.append(session)
        return [dict(marker)]

    monkeypatch.setattr(obligations_module, "_route_defaulted_items", spy)
    view = obligations_module.obligations("proj", "coordinator-a")
    assert asked == ["coordinator-a"]
    assert [row["run_id"] for row in view["obligations"]] == ["r-defaulted"]
    assert (
        view["obligations"][0]["kind"] == obligations_module.ROUTE_DEFAULTED_DUTY_KIND
    )
