"""Bound the phases a dispatch-path pick puts on its critical path.

A pick is asked for inside a five-second dispatch bound, so a phase that runs
twice is not merely slow: it is the difference between a routed selection and a
recorded timeout fallback. Each test here pins one such phase by making the
removed work fail loudly if it runs again. One test pins the opposite edge of
the same bound: the wait estimate must still count every live local worker,
including one from another project, because that figure answers how loaded the
lane is and a narrowed answer is wrong rather than fast.
"""

from __future__ import annotations

import importlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import reckon.crew.dispatch_picker as dispatch_picker_module

# ``reckon.crew.dispatch`` names a function on the package, so bind the module
# through the import system rather than the package attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")
picker = importlib.import_module("reckon.crew.picker")
from reckon.crew.node import TaskNode
from reckon.crew.picker import Candidate, lane_context, snapshot
from reckon.crew.picker.types import PickRequest


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


def test_fit_reuses_the_competence_context_measurement(monkeypatch):
    """A fit measures a backend's context window once, not twice.

    ``_competence_verdict`` already measures the backend's window against the
    node's context and returns the measurement under ``context``. Measuring it
    again in ``_fit`` doubles the instruction-chain read, whose repository
    lookup shells out to git -- the phase this bound removes. The second
    measurement is turned into a hard failure so a reverted cut fails here.
    """

    measured_once = []

    def competence(**kwargs):
        measured_once.append(kwargs["resolution"].backend)
        return {
            "allowed": True,
            "context": {
                "allowed": True,
                "window_tokens": 1_000_000,
                "estimated_tokens": 10,
                "shortfall_tokens": 0,
                "reason": "within-context-window",
            },
        }

    def refuse_a_second_measurement(**kwargs):
        raise AssertionError("context fit measured twice in one _fit")

    monkeypatch.setattr(snapshot.routing, "_competence_verdict", competence)
    monkeypatch.setattr(
        snapshot.routing, "_context_fit_verdict", refuse_a_second_measurement
    )

    request = PickRequest("proj", _node())
    reasons = snapshot._fit(request, "beta", {}, Path("/tmp"))
    assert reasons == []
    assert measured_once == ["beta"]


def test_fit_measures_the_context_when_competence_did_not(monkeypatch):
    """When the competence path skips its context check, _fit still measures."""

    calls = []

    def competence(**kwargs):
        return {"allowed": True}

    def context(**kwargs):
        calls.append(kwargs["resolution"].backend)
        return None

    monkeypatch.setattr(snapshot.routing, "_competence_verdict", competence)
    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", context)

    request = PickRequest("proj", _node())
    assert snapshot._fit(request, "beta", {}, Path("/tmp")) == []
    assert calls == ["beta"]


def test_expected_wait_counts_a_foreign_projects_live_worker(monkeypatch):
    """A live local worker from another project still shapes the wait estimate.

    The figure is the typical wall time for the local runs now live, so a
    worker whose project the pick did not read records for must be profiled
    from its own ledger rather than dropped: dropping it would let the estimate
    read null while the lane is genuinely busy with that project's runs. The
    foreign profile is read exactly once and its median is what the estimate
    reports, so a guard that reinstates the drop fails here.
    """
    now = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
    foreign = {
        "project": "other",
        "backend": "clive",
        "role": "implement",
        "spec_level": "guided",
        "phase": "working",
        "agent": {"local": True, "effort": "standard"},
        "node": {"role": "implement", "spec_level": "guided"},
    }
    monkeypatch.setattr(lane_context, "list_live", lambda: [foreign])
    read: list[str] = []

    def profile(project, **_kwargs):
        read.append(project)
        return {
            "groups": [
                {
                    "backend": "clive",
                    "effort": "standard",
                    "role": "implement",
                    "spec_level": "guided",
                    "runs": 3,
                    "wall_seconds_median": 420.0,
                }
            ]
        }

    monkeypatch.setattr(lane_context, "run_time_profile", profile)

    wait = lane_context._expected_wait(
        project="proj",
        records=[],
        local_backend="clive",
        now=now,
    )
    assert read == ["other"]
    assert wait == 420.0


def _candidate() -> Candidate:
    return Candidate(
        backend="beta",
        family="test",
        model="beta-model",
        effort="medium",
        local=False,
        availability="served",
        utilisation_pct=20,
        burn_multiple=1,
        pace_allowance=5,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={},
    )


def test_slow_client_still_times_out_as_a_recorded_fallback(monkeypatch):
    """A Jev client slower than the bound yields a fallback, never a refusal.

    The client -- not the pick -- is what outlasts the bound, so the real pick
    runs and marks the step it is inside. The fallback names that step in
    pick()'s own vocabulary rather than reading the bare timeout the whole call
    used to record.
    """
    from reckon.crew.picker import prompts
    from reckon.crew.picker import snapshot as picker_snapshot

    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        picker_snapshot, "estimated_context_tokens", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        picker, "_declared_difficulty", lambda *_a, **_k: (None, None, None)
    )
    monkeypatch.setattr(snapshot, "candidates", lambda *_a, **_k: [_candidate()])

    def render(name: str, **_kwargs) -> str:
        if name == "questions.jinja":
            return json.dumps({"route": {"criteria": {"hold": {}}}})
        return json.dumps({"note": "state"})

    monkeypatch.setattr(prompts, "render", render)

    def slow_client(*_args, **_kwargs):
        time.sleep(0.4)
        raise AssertionError("the bound should have returned before the client did")

    monkeypatch.setattr(picker.client, "ask", slow_client)
    started = time.monotonic()
    result = dispatch.dispatch_picker_selection(
        node=_node(),
        config={},
        project="proj",
        repo=Path("/tmp"),
        records=[],
        verdict_inputs={},
        budget_snapshot={},
    )
    assert time.monotonic() - started < 0.4
    assert result["action"] == "fallback"
    # The estimate finishes well inside the bound here, so the step the pick is
    # inside when it expires is the Jev call, named in pick()'s own vocabulary;
    # the estimate's own time beside it shows the expiry was not taken by it.
    assert result["fallback_reason"] == "timeout in pick:jev"
    assert result["stage_ms"]["context-estimate"] is not None
