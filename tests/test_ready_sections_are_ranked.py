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
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
import reckon.roadmap as roadmap_module
from reckon import crew as crew_module
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"

# The section chain that holds the critical path costs far more hours than the
# hub beside it, so nothing on the path can be confused with the reach reading.
SLOW_HOURS = 40.0
ORDINARY_HOURS = 1.0

# Every signal a ranked ready row may report in its reason: one vocabulary
# shared with the plan rows, plus the signal for a row nothing separates.
REASONS = {
    "critical path",
    "sprint",
    "roi",
    "reach",
    "stuck",
    "effort",
    "after hold",
    "ready",
}


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
    roi: str = "high",
) -> Path:
    """Write one effort-M plan whose sections are all implementable."""

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
        _meta("plan-roi", roi),
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
def mixed_project(mounted_project):
    """A long section chain beside a small hub whose section releases two.

    ``slow#s1`` and ``slow-waiter#s1`` cost far more hours than anything else,
    so the critical path runs through them and every row on it outranks every
    row off it. ``hub#s1`` releases two sections and ``hub#s2`` none, in the
    same plan at the same hours, so reach is the only signal that separates
    them — and neither is on the path, so a reach reading there cannot be
    satisfied by membership.

    ``loner`` declares no sections at all: it carries no section row, and it is
    off the path, so a section row that precedes it precedes a plan row.
    """

    project, docs = mounted_project
    _write_plan(docs, "slow", sections={"s1": SLOW_HOURS})
    _write_plan(
        docs,
        "slow-waiter",
        sections={"s1": SLOW_HOURS},
        section_depends_on={"s1": ["slow#s1"]},
    )
    _write_plan(docs, "hub", sections={"s1": ORDINARY_HOURS, "s2": ORDINARY_HOURS})
    for slug in ("hub-waiter-a", "hub-waiter-b"):
        _write_plan(
            docs,
            slug,
            sections={"s1": ORDINARY_HOURS},
            section_depends_on={"s1": ["hub#s1"]},
        )
    _write_plan(docs, "loner", sections={})
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


@pytest.fixture()
def roi_project(mounted_project):
    """Two sections off the path that differ in nothing but their plans' ROI.

    ``anchor`` holds the critical path with a long section, and both other plans
    are one ordinary section off it: they agree on hours, reach, sprint position
    and attempts, so ROI is the only term that separates their sections.
    """

    project, docs = mounted_project
    _write_plan(docs, "anchor", sections={"s1": SLOW_HOURS}, roi="low")
    _write_plan(docs, "alpha", sections={"s1": ORDINARY_HOURS}, roi="high")
    _write_plan(docs, "beta", sections={"s1": ORDINARY_HOURS}, roi="low")
    return project, docs


def _section_rows(result: dict) -> list[dict]:
    """The ranked section rows the ready list carries beside the plan rows."""

    return [row for row in result["immediate_roadmap"] if row.get("plan")]


def _refs(result: dict) -> list[str]:
    return [str(row["ref"]) for row in _section_rows(result)]


def _order(result: dict) -> list[str]:
    """Every ready row's identity in the order the list carries them."""

    return [str(row["slug"]) for row in result["immediate_roadmap"]]


def _row(result: dict, identity: str) -> dict:
    return next(row for row in result["immediate_roadmap"] if row["slug"] == identity)


def _reason(result: dict, ref: str) -> str:
    return str(
        next(row for row in _section_rows(result) if row["ref"] == ref)["reason"]
    )


def test_the_largest_reach_leads_the_off_path_rows(mixed_project):
    result = mcp_module._roadmap(mixed_project[0])

    critical_ids = set(result["critical_path"]["sections"]) | set(
        result["critical_path"]["plans"]
    )
    order = _order(result)
    off_path = [identity for identity in order if identity not in critical_ids]

    # The fixture puts the reach where the path is not, so membership cannot be
    # what places the leading off-path row.
    assert "hub#s1" not in critical_ids
    assert len(_row(result, "hub#s1")["unlocks"]) == 2
    assert off_path[0] == "hub#s1"


def test_every_section_row_says_which_signal_placed_it(mixed_project):
    result = mcp_module._roadmap(mixed_project[0])

    reasons = {str(row["reason"]) for row in _section_rows(result)}

    assert reasons
    assert reasons <= REASONS


def test_a_section_placed_by_reach_says_reach_and_outranks_a_side_section(
    mixed_project,
):
    result = mcp_module._roadmap(mixed_project[0])

    refs = _refs(result)

    assert _reason(result, "hub#s1") == "reach"
    assert refs.index("hub#s1") < refs.index("hub#s2")


def test_a_section_row_outranks_a_plan_row_whose_key_it_beats(mixed_project):
    result = mcp_module._roadmap(mixed_project[0])

    order = _order(result)

    # ``loner`` is a plan row off the path with nothing waiting behind it, and
    # ``hub`` is the plan whose section leads: the section row precedes both,
    # which it cannot do if the section rows are a block after the plan rows.
    assert order.index("hub#s1") < order.index("hub")
    assert order.index("hub#s1") < order.index("loner")


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


def test_a_section_placed_by_roi_says_roi(roi_project):
    result = mcp_module._roadmap(roi_project[0])

    critical_ids = set(result["critical_path"]["sections"])
    refs = _refs(result)

    # Both sections are off the meantime path and equal on hours, reach and
    # attempts, so their plans' ROI is what separates them — and the row says so
    # rather than reporting reach, which releases nothing here.
    assert "alpha#s1" not in critical_ids
    assert _row(result, "alpha#s1")["unlocks"] == []
    assert _reason(result, "alpha#s1") == "roi"
    assert refs.index("alpha#s1") < refs.index("beta#s1")


def test_a_run_blocked_for_over_a_day_marks_its_section_stuck(
    equal_sections_project, monkeypatch
):
    project = equal_sections_project[0]
    now = datetime.now(UTC)

    def pointer(run_id: str, section: str, *, started: datetime) -> dict:
        return {
            "run_id": run_id,
            "project": project,
            "node": {"plan": "twin", "section": section},
            "created_at": started.isoformat(),
        }

    monkeypatch.setattr(
        crew_module,
        "list_live",
        lambda: [
            pointer(
                "run-long-blocked",
                "s1",
                started=now - timedelta(hours=30),
            ),
            pointer(
                "run-briefly-blocked",
                "s2",
                started=now - timedelta(hours=1),
            ),
        ],
    )
    monkeypatch.setattr(
        roadmap_module,
        "recorded_live_run_classifications",
        lambda _project: {
            "run-long-blocked": {"classification": "blocked"},
            "run-briefly-blocked": {"classification": "blocked"},
        },
    )

    result = mcp_module._roadmap(project)

    # Both runs report themselves blocked; only the one that has been blocked
    # for longer than a day lifts its section, so the rule reads the age rather
    # than the classification alone.
    assert _row(result, "twin#s1")["stuck"] is True
    assert _row(result, "twin#s2")["stuck"] is False
    assert _reason(result, "twin#s1") == "stuck"
    assert _refs(result).index("twin#s1") < _refs(result).index("twin#s2")
