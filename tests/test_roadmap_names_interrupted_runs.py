"""Interrupted runs are reported apart from runs in flight.

A pointer for a worker ended by a signal still looks like work in progress to
anything that counts live pointers. The roadmap and the sprint view therefore
separate the two: a run still in flight is waited on, while an interrupted run
needs a decision. These tests drive real pointers through the shared classifier,
so the views exercise the same judgement ``recover`` uses rather than a parallel
one written for them.
"""

from __future__ import annotations

from reckon.crew.node import INTERRUPTED_RUN_PHASE
from reckon.crew.recovery import classify_pointer
from reckon.mcp_views import (
    in_flight_by_plan,
    interrupted_by_plan,
    sprint_state_view,
)
from reckon.roadmap import build_roadmap

PROJECT = "sample"


def _plan(slug: str, status: str = "active", impl: float = 0.3) -> dict:
    return {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": status,
        "impl": impl,
        "depends_on": [],
        "effort": "S",
        "roi": "high",
        "blocking": [],
        "gates": [],
        "followups": [],
    }


def _sprint(sprint_id: str, *slugs: str) -> dict:
    return {"id": sprint_id, "status": "active", "items": list(slugs)}


def _no_live_runs() -> list:
    """Stand in for a fleet with nothing running, without an empty-lambda read."""
    return []


def _running_pointer(plan: str = "alpha", run_id: str = "run-live") -> dict:
    """A pointer whose process is reported alive, so it stays in flight."""

    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-one",
        "created_at": "2026-08-12T18:00:00Z",
        "phase": "working",
        "process_alive": True,
        # Liveness is carried rather than re-derived: the pointer names a host
        # that is not the reading host, which is what a shared crew home
        # produces when a run is launched on one login node and read on another.
        "launcher_host": "another-login-node",
        "node": {"plan": plan, "section": "delivery"},
    }


def _interrupted_pointer(plan: str = "alpha", run_id: str = "run-killed") -> dict:
    """A pointer whose worker ended by SIGTERM, which the classifier reads interrupted."""

    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-two",
        "created_at": "2026-08-12T18:01:00Z",
        "phase": "working",
        "process_alive": False,
        "wait_status": {"signal": 15, "signal_name": "SIGTERM", "exit_code": None},
        "node": {"plan": plan, "section": "delivery"},
    }


def test_the_fixture_pointer_is_the_one_the_classifier_reads_interrupted():
    """The separation below is only meaningful if the classifier agrees on the input."""

    killed = classify_pointer(_interrupted_pointer())
    running = classify_pointer(_running_pointer())

    assert killed["classification"] == INTERRUPTED_RUN_PHASE
    assert "SIGTERM" in killed["detail"]
    assert running["classification"] == "running"


def test_in_flight_by_plan_counts_only_the_running_pointer():
    grouped = in_flight_by_plan(PROJECT, [_running_pointer(), _interrupted_pointer()])

    assert [run["run_id"] for run in grouped["alpha"]] == ["run-live"]


def test_interrupted_by_plan_lists_the_run_with_its_reason_and_next_action():
    interrupted = interrupted_by_plan(
        PROJECT, [_running_pointer(), _interrupted_pointer()]
    )

    assert [run["run_id"] for run in interrupted["alpha"]] == ["run-killed"]
    row = interrupted["alpha"][0]
    assert "SIGTERM" in row["reason"]
    assert row["next_action"]


def test_roadmap_reports_interrupted_runs_apart_from_runs_in_flight(monkeypatch):
    monkeypatch.setattr(
        "reckon.crew.list_live", lambda: [_running_pointer(), _interrupted_pointer()]
    )

    report = build_roadmap(PROJECT, [_plan("alpha")], [_sprint("S1", "alpha")])
    sprint_row = report["sprints"][0]

    assert [run["run_id"] for run in sprint_row["in_flight"]] == ["run-live"]
    assert [run["run_id"] for run in sprint_row["interrupted"]] == ["run-killed"]

    pending = {item["slug"]: item for item in report["pending_work"]}
    assert [run["run_id"] for run in pending["alpha"]["in_flight"]] == ["run-live"]
    assert [run["run_id"] for run in pending["alpha"]["interrupted"]] == ["run-killed"]


def test_sprint_state_view_carries_the_interrupted_run_with_reason_and_action(
    monkeypatch,
):
    monkeypatch.setattr(
        "reckon.crew.list_live", lambda: [_running_pointer(), _interrupted_pointer()]
    )

    report = build_roadmap(PROJECT, [_plan("alpha")], [_sprint("S1", "alpha")])
    view = {row["id"]: row for row in sprint_state_view(report)}

    assert [run["run_id"] for run in view["S1"]["in_flight"]] == ["run-live"]
    interrupted = view["S1"]["interrupted"]
    assert [run["run_id"] for run in interrupted] == ["run-killed"]
    assert "SIGTERM" in interrupted[0]["reason"]
    assert interrupted[0]["next_action"]


def test_a_sprint_with_no_interrupted_runs_reports_an_empty_list(monkeypatch):
    monkeypatch.setattr("reckon.crew.list_live", lambda: [_running_pointer()])

    report = build_roadmap(PROJECT, [_plan("alpha")], [_sprint("S1", "alpha")])
    view = {row["id"]: row for row in sprint_state_view(report)}

    assert "interrupted" in view["S1"]
    assert view["S1"]["interrupted"] == []
    assert [run["run_id"] for run in view["S1"]["in_flight"]] == ["run-live"]
    assert report["sprints"][0]["interrupted"] == []


def test_a_plan_with_no_live_runs_reports_empty_lists_not_missing_keys(monkeypatch):
    monkeypatch.setattr("reckon.crew.list_live", _no_live_runs)

    report = build_roadmap(PROJECT, [_plan("alpha")], [_sprint("S1", "alpha")])

    assert report["sprints"][0]["in_flight"] == []
    assert report["sprints"][0]["interrupted"] == []


def test_a_run_from_another_project_never_carries_a_foreign_interruption(monkeypatch):
    foreign = _interrupted_pointer()
    foreign["project"] = "elsewhere"
    monkeypatch.setattr("reckon.crew.list_live", lambda: [foreign])

    report = build_roadmap(PROJECT, [_plan("alpha")], [_sprint("S1", "alpha")])

    assert report["sprints"][0]["interrupted"] == []
    assert report["sprints"][0]["in_flight"] == []
