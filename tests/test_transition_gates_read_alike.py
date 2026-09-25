"""Every surface reads the roadmap's split between execution and closure.

A transition gate holds a closure or a decision, never execution, so a plan
whose only open gate is a transition gate stays ready in the roadmap, in the
served plan payload and in the MCP blocking projection, and all three name the
same edge as the thing being held.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import mcp_views, roadmap, serve
from reckon._plan_html import read_state, write_state
from reckon._store import new_plan_html

PROJECT = "alike"
CLOSURE_SLUG = "closure-held"
DECISION_SLUG = "decision-held"
OPEN_SLUG = "open-decision"

CLOSURE_GATE = {
    "id": "outcome-gate",
    "section": "implementation",
    "gated_sections": ["release"],
    "transition": "plan-terminal",
    "gating_plan": "producer#outcome",
    "measure": "The producer records its outcome",
    "verdict": "",
}
DECISION_GATE = {
    "id": "choice-gate",
    "section": "implementation",
    "gated_sections": ["release"],
    "transition": "decision-lockable",
    "gating_plan": "foreign:producer#outcome",
    "decision": "accept-outcome",
    "measure": "The producer records its outcome",
    "verdict": "",
}


def _plan_state(slug: str, gate: dict | None, decisions: dict | None = None) -> dict:
    state = {
        "project": PROJECT,
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": "active",
        "version": 0,
        "gates": [gate] if gate else [],
        "followups": [
            {"id": "continue", "status": "open", "prompt": f"/reckon-build {slug}"}
        ],
    }
    if decisions is not None:
        state["decisions"] = decisions
    return state


@pytest.fixture()
def project_tree(tmp_path, monkeypatch):
    """One temporary project: a closure-held plan and a decision-held plan."""

    docs_dir = tmp_path / "docs"
    plans_dir = docs_dir / "plans"
    plans_dir.mkdir(parents=True)
    fixtures = {
        CLOSURE_SLUG: _plan_state(CLOSURE_SLUG, CLOSURE_GATE),
        DECISION_SLUG: _plan_state(
            DECISION_SLUG,
            DECISION_GATE,
            {"accept-outcome": {"title": "Accept the outcome?"}},
        ),
        # A decision with no transition edge is still unanswered work: nothing
        # but the transition split may move it out of the blocked set.
        OPEN_SLUG: _plan_state(
            OPEN_SLUG, None, {"accept-outcome": {"title": "Accept?"}}
        ),
    }
    for slug, state in fixtures.items():
        (plans_dir / f"{slug}.html").write_text(
            write_state(new_plan_html(PROJECT, slug), state), encoding="utf-8"
        )

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    # No live crew pointers are read or written: the surfaces group runs from
    # the pointer directory, which is not this repository's state.
    monkeypatch.setattr(
        roadmap, "partition_live_runs", lambda *args, **kwargs: ({}, {})
    )
    monkeypatch.setattr(
        mcp_views, "partition_live_runs", lambda *args, **kwargs: ({}, {})
    )
    serve._DISC_CACHE.clear()
    yield docs_dir
    serve._DISC_CACHE.clear()


def _served_rows(docs_dir: Path) -> dict[str, dict]:
    discovered = serve.discover_plans(docs_dir, PROJECT, None)
    return {
        str(item["slug"]): item
        for item in discovered["inventory"]
        if item.get("type") == "plan"
    }


def _roadmap_report(docs_dir: Path) -> dict:
    discovered = serve.discover_plans(docs_dir, PROJECT, None)
    return roadmap.build_roadmap(
        PROJECT,
        discovered["inventory"],
        discovered["sprints"],
        active_sprint_id=discovered.get("active_sprint_id"),
        project_manifest=discovered,
    )


def _roadmap_row(docs_dir: Path, slug: str) -> dict:
    report = _roadmap_report(docs_dir)
    ready = [row for row in report["ready_now"] if row["slug"] == slug]
    assert ready, f"{slug} is not in the roadmap ready set: {report['blocked']}"
    return ready[0]


def _mcp_summary(docs_dir: Path, slug: str) -> dict:
    data = read_state((docs_dir / "plans" / f"{slug}.html").read_text(encoding="utf-8"))
    selector = mcp_views.ResourceSelector(PROJECT, "plan", slug)
    return mcp_views.resource_view(
        selector,
        int(data.get("version") or 0),
        data,
        view="summary",
        provenance={"checkout": str(docs_dir.parent)},
        deps=[],
    )


def _named(rows: list[dict], id_key: str = "id") -> set[tuple[str, str]]:
    return {
        (str(row.get(id_key) or ""), str(row.get("gating_plan") or "")) for row in rows
    }


def _agrees(docs_dir: Path, slug: str) -> tuple[dict, dict, dict]:
    """Return the roadmap row, the served row and the MCP summary for one plan."""

    roadmap_row = _roadmap_row(docs_dir, slug)
    served_row = _served_rows(docs_dir)[slug]
    summary = _mcp_summary(docs_dir, slug)

    assert roadmap_row["gate_blockers"] == []
    assert served_row["blocking"] == [], served_row["blocking"]
    assert summary["blocking"] == [], summary["blocking"]
    assert served_row["effective_status"] == "active"
    assert summary["state"]["effective_status"] == "active"
    return roadmap_row, served_row, summary


def test_plan_terminal_closure_gate_reads_alike(project_tree):
    roadmap_row, served_row, summary = _agrees(project_tree, CLOSURE_SLUG)

    expected = {("outcome-gate", "producer#outcome")}
    assert _named(roadmap_row["closure_blockers"]) == expected
    assert _named(served_row["closure_blockers"]) == expected
    assert _named(summary["closure_blockers"]) == expected
    assert roadmap_row["closure_blockers"] == served_row["closure_blockers"]


def test_decision_transition_gate_reads_alike(project_tree):
    roadmap_row, served_row, summary = _agrees(project_tree, DECISION_SLUG)

    expected = {("accept-outcome", "foreign:producer#outcome")}
    assert [row["status"] for row in roadmap_row["decision_blockers"]] == ["gated"]
    assert _named(roadmap_row["decision_blockers"]) == expected
    assert [row["status"] for row in served_row["decision_blockers"]] == ["gated"]
    assert _named(served_row["decision_blockers"]) == expected
    assert _named(summary["decision_blockers"]) == expected
    assert roadmap_row["decision_blockers"][0]["gate_id"] == "choice-gate"
    assert roadmap_row["decision_blockers"][0]["transition"] == "decision-lockable"


def test_the_repair_is_scoped_to_the_transition_split(project_tree):
    """An unanswered decision with no transition edge still idles its plan."""

    report = _roadmap_report(project_tree)
    assert OPEN_SLUG not in [row["slug"] for row in report["ready_now"]]
    row = next(row for row in report["pending_work"] if row["slug"] == OPEN_SLUG)
    assert [item["status"] for item in row["decision_blockers"]] == ["open"]
