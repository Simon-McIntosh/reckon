"""The path and the unlock set run through the sections that carry the work.

A ``plan-section-depends-on`` mapping declares its edge between two sections,
and the rendered roadmap is expected to keep that edge there: the critical path
names the sections it crosses, in order and priced by their own hours, and the
plan whose section another plan waits on reports that other plan among its
unlocks. The plan itself stays in the ready set, because a section-scoped wait
holds its section and not the whole plan.

A wait between two sections of one plan is a different case: it sequences that
plan's own work, so the plan releases nothing by it and must not report itself
among the plans this plan unlocks.

Every fixture is written into a synthetic docs tree under the test's own
``tmp_path``, so no test reads a mounted project's state.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"
WAITS_ON_A = "plan-b"
WAITS_ON_B = "plan-c"
UPSTREAM = "plan-a"
SELF_WIRED = "plan-self"

# The effort each section records, and the order the edges force on the path.
UPSTREAM_HOURS = 3.0
WAITS_ON_A_HOURS = 5.0
WAITS_ON_B_HOURS = 2.0
SUMMED_HOURS = UPSTREAM_HOURS + WAITS_ON_A_HOURS + WAITS_ON_B_HOURS

# The section identities the chain forces, in the order it forces them. Written
# out here rather than read back off a field a path carries, so a path that
# reports something else is compared against the fixture, not against itself.
EXPECTED_SECTIONS = [
    f"{UPSTREAM}#s2",
    f"{WAITS_ON_A}#s3",
    f"{WAITS_ON_B}#s1",
]


@pytest.fixture()
def mounted_project(tmp_path, monkeypatch):
    project = PROJECT
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
    sections: dict[str, float],
    section_depends_on: dict[str, list[str]] | None = None,
) -> Path:
    """Write one plan holding its open sections, each at its own effort."""

    def meta(name: str, value: Any) -> str:
        return f'<meta name="{name}" content="{escape(value, quote=True)}">'

    declarations = json.dumps(
        dict.fromkeys(sections, "implementable"), separators=(",", ":")
    )
    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        meta("plan-slug", slug),
        '<meta name="plan-title" content="' + slug + '">',
        '<meta name="plan-status" content="active">',
        meta("plan-section-declarations", declarations),
    ]
    if section_depends_on:
        head.append(
            meta(
                "plan-section-depends-on",
                json.dumps(section_depends_on, separators=(",", ":")),
            )
        )
    head.append(f"<title>{slug}</title></head>")
    body = ['<body><main class="plan-doc">']
    for name, hours in sorted(sections.items()):
        body.append(
            f'<h2 id="{name}">Section</h2>'
            '<section data-reckon="section"'
            f' data-id="{name}"'
            f' data-effort-hours="{hours}"'
            ' data-capability-version="1.0" data-capability-class="general"'
            ' data-capability-reasoning="standard"'
            ' data-capability-verification="standard"'
            ' data-capability-risk="low"'
            ' data-status="implementable" data-links=""></section>'
        )
    body.append("</main></body></html>")
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(head) + "".join(body), encoding="utf-8")
    return path


@pytest.fixture()
def three_plan_project(mounted_project):
    """Three plans whose only cross-plan wiring is a section-to-section chain.

    ``plan-b#s3`` waits on ``plan-a#s2`` and ``plan-c#s1`` waits on
    ``plan-b#s3``; no plan declares a whole-plan dependency at all.
    """

    project, docs = mounted_project
    _write_plan(docs, UPSTREAM, sections={"s2": UPSTREAM_HOURS})
    _write_plan(
        docs,
        WAITS_ON_A,
        sections={"s3": WAITS_ON_A_HOURS},
        section_depends_on={"s3": [f"{UPSTREAM}#s2"]},
    )
    _write_plan(
        docs,
        WAITS_ON_B,
        sections={"s1": WAITS_ON_B_HOURS},
        section_depends_on={"s1": [f"{WAITS_ON_A}#s3"]},
    )
    return project, docs


@pytest.fixture()
def self_wired_project(mounted_project):
    """One plan whose second section waits on the first section of that plan.

    Nothing crosses a plan boundary here: the edge sequences the plan's own
    work, so the plan releases nothing and must not name itself.
    """

    project, docs = mounted_project
    _write_plan(
        docs,
        SELF_WIRED,
        sections={"s1": 2.0, "s2": 4.0},
        section_depends_on={"s2": [f"{SELF_WIRED}#s1"]},
    )
    return project, docs


def _rows(result: dict) -> dict[str, dict]:
    return {row["slug"]: row for row in result["pending_work"]}


def test_the_critical_path_names_the_sections_in_the_order_the_edges_force(
    three_plan_project,
):
    result = mcp_module._roadmap(three_plan_project[0])

    assert result["critical_path"]["sections"] == EXPECTED_SECTIONS


def test_the_critical_path_prices_the_sections_it_crosses(three_plan_project):
    result = mcp_module._roadmap(three_plan_project[0])

    critical = result["critical_path"]

    assert critical["plans"] == [UPSTREAM, WAITS_ON_A, WAITS_ON_B]
    assert critical["worker_hours"] == SUMMED_HOURS
    assert critical["length_hours"] == SUMMED_HOURS


def test_the_open_paths_name_the_same_sections_the_chain_forces(three_plan_project):
    result = mcp_module._roadmap(three_plan_project[0])

    assert result["open_paths"][0]["sections"] == EXPECTED_SECTIONS


def test_the_plan_whose_section_is_waited_on_unlocks_the_waiting_plan(
    three_plan_project,
):
    result = mcp_module._roadmap(three_plan_project[0])

    assert WAITS_ON_B in _rows(result)[WAITS_ON_A]["unlocks"]
    assert WAITS_ON_A in _rows(result)[UPSTREAM]["unlocks"]


def test_a_section_scoped_wait_leaves_the_plan_ready(three_plan_project):
    result = mcp_module._roadmap(three_plan_project[0])

    rows = _rows(result)

    assert rows[WAITS_ON_B]["dependency_ready"] is True
    assert rows[WAITS_ON_B]["blocked_sections"] == ["s1"]


def test_a_plan_does_not_unlock_itself_for_its_own_section_wait(self_wired_project):
    result = mcp_module._roadmap(self_wired_project[0])

    row = _rows(result)[SELF_WIRED]

    assert row["unlocks"] == []
    assert SELF_WIRED not in row["unlocks"]
