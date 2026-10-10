"""Roadmap edges that wait on one section of another plan.

A plan held by a gate on one section of another plan, or by a ``#section`` ref,
shows that section rather than the whole plan: the raw view's edge names the
anchor, and each ``open_paths`` entry carries an ``edges`` list naming the
section and gate beside the plan. The critical path keeps the key set every
summary already carries. A plan-level ``depends_on`` keeps its plan-level edge.

Every fixture is written into a synthetic docs tree under the test's own
``tmp_path``, so no test reads a mounted project's state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

TARGET = "cut-cell-experiment"
GATED = "rebanked-topology"
CARRIER = "corpus-builder"
PLAIN = "plain-consumer"


@pytest.fixture()
def mounted_project(tmp_path, monkeypatch):
    project = "sample"
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()
    return project, docs


def _write_plan(
    docs: Path,
    slug: str,
    *,
    status: str = "active",
    depends_on: list[str] | None = None,
    gates: list[dict] | None = None,
) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            bare,
            {
                "slug": slug,
                "title": slug,
                "status": status,
                "depends_on": depends_on or [],
                "decisions": {},
                "gates": gates or [],
                "followups": [],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    return path


def _section_gate(gate_id: str, section: str, **extra) -> dict:
    return {
        "id": gate_id,
        "section": section,
        "measure": "Required evidence is present",
        "verdict": "",
        **extra,
    }


def _rows(result: dict) -> dict[str, dict]:
    return {row["slug"]: row for row in result["pending_work"]}


def _edges(result: dict) -> list[dict]:
    paths = list(result.get("open_paths") or [])
    critical = result.get("critical_path") or {}
    if critical and critical not in paths:
        paths.append(critical)
    return [edge for path in paths for edge in path.get("edges") or []]


@pytest.fixture()
def wired_project(mounted_project):
    project, docs = mounted_project
    _write_plan(
        docs,
        TARGET,
        gates=[
            _section_gate("cut-cell-mesh-lands", "s1"),
            _section_gate("cut-cell-flux-lands", "s2"),
        ],
    )
    _write_plan(
        docs,
        GATED,
        gates=[
            _section_gate("rebank-dispatch-starts", "s1"),
            _section_gate("rebank-after-rung-a", "s2", gating_plan=f"{TARGET}#s1"),
        ],
    )
    _write_plan(docs, CARRIER, depends_on=[f"{TARGET}#s2"])
    _write_plan(docs, PLAIN, depends_on=[TARGET])
    return project, docs


def test_a_gate_on_one_section_of_another_plan_reports_the_section(wired_project):
    result = mcp_module._roadmap(wired_project[0])

    row = _rows(result)[GATED]
    (edge,) = [dep for dep in row["depends_on"] if dep.get("gate")]

    assert edge["ref"] == f"{TARGET}#s1"
    assert edge["stage"] == "s1"
    assert edge["gate"] == "rebank-after-rung-a"
    assert edge["section_found"] is True
    assert edge["satisfied"] is False
    assert [dep["ref"] for dep in row["section_depends_on"]] == [f"{TARGET}#s1"]


def test_the_gate_edge_holds_only_the_named_section(wired_project):
    result = mcp_module._roadmap(wired_project[0])

    row = _rows(result)[GATED]

    assert row["blocked_sections"] == ["s2"]
    assert "s1" in row["ready_sections"]


def test_a_gate_naming_the_section_in_words_reports_it(wired_project):
    project, docs = wired_project
    _write_plan(
        docs,
        GATED,
        gates=[
            _section_gate(
                "rebank-after-rung-a",
                "s2",
                measure=(
                    f"The bank is regenerated only after {TARGET} section 1 has landed."
                ),
            )
        ],
    )

    result = mcp_module._roadmap(project)

    row = _rows(result)[GATED]
    (edge,) = [dep for dep in row["depends_on"] if dep.get("gate")]
    assert edge["ref"] == f"{TARGET}#s1"
    assert edge["source"] == "gate-text"


def test_a_ref_carrying_a_section_reports_the_section(wired_project):
    result = mcp_module._roadmap(wired_project[0])

    row = _rows(result)[CARRIER]
    (edge,) = [dep for dep in row["depends_on"] if dep.get("stage")]

    assert edge["ref"] == f"{TARGET}#s2"
    assert edge["section_found"] is True


CRITICAL_PATH_KEYS = {
    "plans",
    "length_hours",
    "length_unit",
    "worker_hours",
    "effort_unit",
    "uncalibrated_plans",
    "uncalibrated_count",
}


def test_the_raw_view_paths_carry_the_section_on_each_such_edge(wired_project):
    result = mcp_module._roadmap(wired_project[0])

    edges = _edges(result)

    assert any(
        edge["ref"] == f"{TARGET}#s1" and edge["section"] == "s1" for edge in edges
    )
    assert any(
        edge["ref"] == f"{TARGET}#s2" and edge["section"] == "s2" for edge in edges
    )


def test_the_critical_path_keeps_the_shape_every_summary_carries(wired_project):
    result = mcp_module._roadmap(wired_project[0])

    assert set(result["critical_path"]) == CRITICAL_PATH_KEYS


def test_a_plain_plan_level_depends_on_keeps_its_plan_level_edge(wired_project):
    result = mcp_module._roadmap(wired_project[0])

    row = _rows(result)[PLAIN]
    (edge,) = row["depends_on"]

    assert edge["ref"] == TARGET
    assert "stage" not in edge
    (path_edge,) = [e for e in _edges(result) if e["from"] == PLAIN]
    assert path_edge["section"] == ""
    assert path_edge["gate"] == ""
