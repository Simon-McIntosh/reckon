"""``reckon audit`` reports the two sides of sprint membership disagreeing.

A sprint's member list and a plan's ``plan-sprint`` are authored independently,
so they drift. The audit compares them through the sprint resource reader and
reports each plan whose declared sprint is not the sprint that lists it, and
each shipped plan a still-open sprint lists. A plan that agrees with its
sprint, and one no sprint lists and that names no sprint, stay silent.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.doccheck import SPRINT_MEMBERSHIP, audit_lifecycle

PROJECT = "proj"
MEMBER_SPRINT = "S26"
OTHER_SPRINT = "S27"


def _plan_html(slug: str, *, sprint: str, status: str = "active") -> str:
    metas = (
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-status" content="{status}">'
        f'<meta name="plan-sprint" content="{sprint}">'
        '<meta name="plan-impl" content="1.0">'
        '<meta name="plan-modified" content="2026-10-01">'
        f'<meta name="docs-project" content="{PROJECT}">'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{metas}<title>{slug}</title></head>"
        '<body><main class="plan-doc"><h2 id="s1">Body</h2></main></body></html>'
    )


def _sprint_html(sprint_id: str, *, status: str, members: list[str]) -> str:
    state = {
        "type": "sprint",
        "id": sprint_id,
        "status": status,
        "theme": sprint_id,
        "items": [{"slug": member} for member in members],
        "version": 1,
    }
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="sprint">'
        f'<meta name="reckon-id" content="{sprint_id}">'
        '<meta name="reckon-version" content="1">'
        f"<title>{sprint_id}</title></head><body>"
        f'<main class="reckon-resource" data-type="sprint" data-id="{sprint_id}">'
        '<ol data-reckon="sprint-items"></ol>'
        '<script type="application/json" id="reckon-resource-state">'
        f"{json.dumps(state)}</script></main></body></html>"
    )


def _project(tmp_path: Path) -> Path:
    """A distributed project whose sprints and plans live under one docs root.

    ``MEMBER_SPRINT`` lists a, b and d; ``OTHER_SPRINT`` lists nothing. Plan a
    agrees with it, b names the other sprint, c names the member sprint without
    being listed, and d is shipped while still carried by the open sprint.
    """
    docs = tmp_path / PROJECT / "docs"
    (docs / ".reckon").mkdir(parents=True)
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": PROJECT,
                "resources": [],
            }
        ),
        encoding="utf-8",
    )
    (docs / "sprints").mkdir()
    (docs / "sprints" / f"{MEMBER_SPRINT}.html").write_text(
        _sprint_html(MEMBER_SPRINT, status="active", members=["a", "b", "d"]),
        encoding="utf-8",
    )
    (docs / "sprints" / f"{OTHER_SPRINT}.html").write_text(
        _sprint_html(OTHER_SPRINT, status="planned", members=[]),
        encoding="utf-8",
    )
    (docs / "a.html").write_text(
        _plan_html("a", sprint=MEMBER_SPRINT), encoding="utf-8"
    )
    (docs / "b.html").write_text(_plan_html("b", sprint=OTHER_SPRINT), encoding="utf-8")
    (docs / "c.html").write_text(
        _plan_html("c", sprint=MEMBER_SPRINT), encoding="utf-8"
    )
    (docs / "d.html").write_text(
        _plan_html("d", sprint=MEMBER_SPRINT, status="shipped"), encoding="utf-8"
    )
    return docs


def _membership(docs_dir: Path):
    findings = audit_lifecycle(project=PROJECT, docs_dir=docs_dir)
    return [f for f in findings if f.flag == SPRINT_MEMBERSHIP]


def test_membership_reports_both_sides_and_none_for_agreement(tmp_path: Path) -> None:
    docs = _project(tmp_path)
    rows = _membership(docs)

    warns = sorted(f.slug for f in rows if f.severity == "warn")
    assert warns == ["b", "c"]
    assert "a" not in {f.slug for f in rows}


def test_shipped_plan_in_an_open_sprint_reports_once_as_info(tmp_path: Path) -> None:
    docs = _project(tmp_path)
    rows = _membership(docs)

    info = [f for f in rows if f.severity == "info"]
    assert [f.slug for f in info] == ["d"]
