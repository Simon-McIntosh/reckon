"""Ranking the queue reads a run's block from its recorded event, not a classifier.

The station ranks queued requests on every pass that admits work, and the
ranking key asks of each section only whether a run on it has been blocked for
a day. The roadmap build behind that key fell back to classifying every live run
that had no recorded watcher event, and one classification reads the run's
whole review store, so ranking the queue cost a store read per live run. A run
blocked for a day has recorded its block, so the ranking needs no classifier.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import roadmap
from reckon.crew import queue_order, recovery

PROJECT = "rank-proj"
OLD = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pointer(run_id: str, section: str) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "created_at": OLD,
        "node": {"plan": "rank-plan", "section": section},
    }


@pytest.fixture
def classified(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def classify(pointer):
        calls.append(str(pointer.get("run_id")))
        return {"recovery_classification": roadmap.BLOCKED_RUN_CLASSIFICATION}

    monkeypatch.setattr(recovery, "classify_pointer", classify)
    monkeypatch.setattr(
        roadmap,
        "recorded_live_run_classifications",
        lambda _project: {
            "r-recorded": {
                "recovery_classification": roadmap.BLOCKED_RUN_CLASSIFICATION
            }
        },
    )
    return calls


POINTERS = [_pointer("r-recorded", "s1"), _pointer("r-unrecorded", "s2")]


def test_a_ranking_scope_classifies_no_run(classified):
    with roadmap.roadmap_build_scope(resolve_coordinators=False, classify_runs=False):
        seconds = roadmap._blocked_section_run_seconds(PROJECT, POINTERS)
    assert classified == []
    assert set(seconds) == {("rank-plan", "s1")}


def test_outside_a_ranking_scope_an_unrecorded_run_is_classified(classified):
    seconds = roadmap._blocked_section_run_seconds(PROJECT, POINTERS)
    assert classified == ["r-unrecorded"]
    assert set(seconds) == {("rank-plan", "s1"), ("rank-plan", "s2")}


def test_queue_priority_keys_build_in_a_ranking_scope(monkeypatch, tmp_path):
    seen: list[bool] = []

    def build(*_args, **_kwargs):
        scope = roadmap._BUILT_ROADMAP.get()
        seen.append(scope is not None and not scope.classify_runs)
        return {"sprints": [], "critical_path": {}, "immediate_roadmap": []}

    monkeypatch.setattr(roadmap, "build_roadmap", build)
    monkeypatch.setattr(
        "reckon.mcp_discovery._docs_dir_for_project", lambda *_a, **_k: tmp_path
    )
    monkeypatch.setattr(
        queue_order, "_roadmap_input_signature", lambda *_a, **_k: ("fresh",)
    )
    monkeypatch.setattr(queue_order, "_PRIORITY_KEYS", {})
    monkeypatch.setattr(
        "reckon.crew.plan_review._plan_inventory_item", lambda *_a, **_k: None
    )
    queue_order._roadmap_priority_keys(
        [{"project": PROJECT, "plan": "rank-plan", "section": "s1"}]
    )
    assert seen == [True]
