"""Ready sections carry their own ranked rows, and each row names its signal.

The roadmap's ready list has always held plan rows. A section is the unit the
work actually sits in, so it is ranked beside the plan that owns it and says
which signal placed it: critical-path membership, the reach it holds over the
sections that wait on it, or the stuck signal a section that keeps coming back
carries.

Every fixture is written into a synthetic docs tree under the test's own
``tmp_path``, so no test reads a mounted project's state. Every plan in these
fixtures is ROI-high and effort-M, so ROI and effort never decide an order here:
what separates two rows is the signal the test is about.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
import reckon.roadmap as roadmap_module
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"

# ``wide#s1`` costs the most hours and releases the most sections, so it is both
# the critical-path head and the largest reach. ``wide#s3`` releases two and
# ``wide#s2`` releases none, at the same hours and off the path, so reach is the
# only signal that separates them.
REACH_HEAD_HOURS = 30.0
REACH_SIDE_HOURS = 2.0
ORDINARY_HOURS = 1.0
REASONS = {"critical path", "reach", "stuck"}


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


def _meta(name: str, value: Any) -> str:
    return f'<meta name="{name}" content="{escape(value, quote=True)}">'


def _write_plan(
    docs: Path,
    slug: str,
    *,
    sections: dict[str, float],
    section_depends_on: dict[str, list[str]] | None = None,
) -> Path:
    """Write one ROI-high, effort-M plan whose sections are all implementable."""

    declarations = json.dumps(
        dict.fromkeys(sections, "implementable"), separators=(",", ":")
    )
    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        _meta("plan-slug", slug),
        _meta("plan-title", slug),
        '<meta name="plan-status" content="active">',
        '<meta name="plan-roi" content="high">',
        '<meta name="plan-effort" content="M">',
        _meta("plan-section-declarations", declarations),
    ]
    if section_depends_on:
        head.append(
            _meta(
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
def reach_project(mounted_project):
    """One plan whose sections differ only in the work waiting behind them."""

    project, docs = mounted_project
    _write_plan(
        docs,
        "wide",
        sections={
            "s1": REACH_HEAD_HOURS,
            "s2": ORDINARY_HOURS,
            "s3": REACH_SIDE_HOURS,
        },
    )
    waiters = {
        "waiter-a": "wide#s1",
        "waiter-b": "wide#s1",
        "waiter-c": "wide#s1",
        "waiter-d": "wide#s1",
        "waiter-e": "wide#s3",
        "waiter-f": "wide#s3",
    }
    for slug, target in waiters.items():
        _write_plan(
            docs,
            slug,
            sections={"s1": ORDINARY_HOURS},
            section_depends_on={"s1": [target]},
        )
    return project, docs


@pytest.fixture()
def equal_sections_project(mounted_project):
    """One plan whose two sections are equal in every signal but attempts.

    Neither section releases anything, both cost the same hours and both sit
    in the same plan, so ROI, effort, reach and sprint position separate
    nothing here: only the attempts a section has been given can move it.
    """

    project, docs = mounted_project
    _write_plan(
        docs,
        "twin",
        sections={"s1": ORDINARY_HOURS, "s2": ORDINARY_HOURS},
    )
    return project, docs


def _section_rows(result: dict) -> list[dict]:
    """The ranked section rows the ready list carries beside the plan rows."""

    return [row for row in result["immediate_roadmap"] if row.get("plan")]


def _refs(result: dict) -> list[str]:
    return [str(row["ref"]) for row in _section_rows(result)]


def _reason(result: dict, ref: str) -> str:
    return str(
        next(row for row in _section_rows(result) if row["ref"] == ref)["reason"]
    )


def test_the_section_with_the_largest_reach_leads_the_ranked_rows(reach_project):
    result = mcp_module._roadmap(reach_project[0])

    assert _refs(result), "no ready section rows were ranked"
    assert _refs(result)[0] == "wide#s1"


def test_every_section_row_says_which_signal_placed_it(reach_project):
    result = mcp_module._roadmap(reach_project[0])

    reasons = {str(row["reason"]) for row in _section_rows(result)}

    assert reasons
    assert reasons <= REASONS


def test_a_section_placed_by_reach_says_reach_and_outranks_a_side_section(
    reach_project,
):
    result = mcp_module._roadmap(reach_project[0])

    refs = _refs(result)

    assert _reason(result, "wide#s3") == "reach"
    assert refs.index("wide#s3") < refs.index("wide#s2")


def test_two_attempts_outrank_an_equal_section_with_none(
    equal_sections_project, monkeypatch
):
    project = equal_sections_project[0]

    def fabricated_attempts(*_args, **_kwargs):
        return {"twin": {"s2": {"attempts": 2, "attempt_outcomes": []}}}

    monkeypatch.setattr(roadmap_module, "section_attempts_by_plan", fabricated_attempts)

    result = mcp_module._roadmap(project)

    refs = _refs(result)

    assert _reason(result, "twin#s2") == "stuck"
    assert refs.index("twin#s2") < refs.index("twin#s1")
