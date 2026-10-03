"""Bound the phases a dispatch-path pick puts on its critical path.

A pick is asked for inside a five-second dispatch bound, so a phase that runs
twice or that reads a second project's whole ledger is not merely slow: it is
the difference between a routed selection and a recorded timeout fallback. Each
test here pins one such phase by making the removed work fail loudly if it runs
again.
"""

from __future__ import annotations

import importlib
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# ``reckon.crew.dispatch`` names a function on the package, so bind the module
# through the import system rather than the package attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")
picker = importlib.import_module("reckon.crew.picker")
from reckon.crew.picker import lane_context, snapshot
from reckon.crew.picker.types import PickRequest
from reckon.crew.node import TaskNode


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


def test_expected_wait_never_reads_a_foreign_project_ledger(monkeypatch):
    """Only the pick's own project records are in scope for the wait estimate.

    A live local worker from another project has no profile the caller handed
    over, and reading that project's whole ledger would put a second ledger's
    size on the pick's critical path. That read is turned into a hard failure
    so a reverted cut fails here.
    """
    now = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
    stamp = "2026-10-03T03:00:00Z"
    records = [
        {
            "backend": "clive",
            "role": "implement",
            "spec_level": "guided",
            "agent": {"effort": "standard"},
            "wall_seconds": 300.0,
            "completed_at": stamp,
        }
    ]
    own = {
        "project": "proj",
        "backend": "clive",
        "role": "implement",
        "spec_level": "guided",
        "phase": "working",
        "agent": {"local": True, "effort": "standard"},
        "node": {"role": "implement", "spec_level": "guided"},
    }
    foreign = {
        "project": "other",
        "backend": 2,
        "phase": "working",
        "agent": {"local": True, "effort": "standard"},
        "node": {"role": "implement", "spec_level": "guided"},
    }
    monkeypatch.setattr(lane_context, "list_live", lambda: [own, foreign])
    monkeypatch.setattr(
        lane_context,
        "run_time_profile",
        lambda *_a, **_k: pytest.fail("foreign project ledger read on the pick path"),
    )

    wait = lane_context._expected_wait(
        project="proj",
        records=records,
        local_backend="clive",
        now=now,
    )
    assert wait == 300.0


def test_slow_client_still_times_out_as_a_recorded_fallback(monkeypatch):
    """A Jev client slower than the bound yields a fallback, never a refusal."""

    monkeypatch.setattr(dispatch, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)

    def slow_pick(
        request,
        config,
        *,
        repo,
        cached_only=False,
        records=None,
        verdict_inputs=None,
        budget_snapshot=None,
    ):
        time.sleep(0.4)
        raise AssertionError("the bound should have returned before the client did")

    monkeypatch.setattr(picker, "pick", slow_pick)
    started = time.monotonic()
    result = dispatch.dispatch_picker_selection(
        node=SimpleNamespace(),
        config={},
        project="proj",
        repo=Path("/tmp"),
    )
    assert time.monotonic() - started < 0.4
    assert result["action"] == "fallback"
    assert result["fallback_reason"] == "timeout"