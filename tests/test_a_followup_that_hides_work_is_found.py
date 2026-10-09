"""One predicate names a followup that hides work, and the roadmap reports it.

The plans are written into a temporary docs directory in the store's own
layout and read back through the parser, so the fixture exercises the same
fields a real plan carries rather than dicts shaped to please the predicate.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from reckon._plan_html import parse_plan, write_state
from reckon.followup_pointers import classify_followup
from reckon.roadmap import build_roadmap
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "temp-followup-pointer-project"
HOST_DECLARATIONS = {"s1": "done", "s2": "implementable"}


def _write_plan(
    docs_dir: Path,
    slug: str,
    *,
    project: str = PROJECT,
    status: str = "active",
    declarations: dict[str, str] | None = None,
    followups: tuple[tuple[str, str, str], ...] = (),
) -> Path:
    """Write one plan HTML into ``docs_dir/plans`` in the store's own layout."""

    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    metas = [
        ("docs-project", project),
        ("reckon-type", "plan"),
        ("plan-slug", slug),
        ("plan-status", status),
        ("plan-modified", "2026-10-01"),
        (
            "plan-section-declarations",
            html.escape(json.dumps(declarations or {}, separators=(",", ":"))),
        ),
    ]
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    articles = "".join(
        f'<article class="r-fu" data-id="{fid}" data-status="open"'
        f' data-written-by="test" data-written-at="2026-10-01"'
        f' data-recommends-skill="{html.escape(skill)}">'
        f'<h4 class="r-fu-title">{fid}</h4>'
        f'<div class="r-fu-body"><p>{fid}</p></div>'
        + (f'<pre class="r-fu-prompt">{html.escape(prompt)}</pre>' if prompt else "")
        + "</article>"
        for fid, prompt, skill in followups
    )
    path.write_text(
        "<!doctype html><html><head>"
        f"{head}<title>{slug}</title></head>"
        '<body><main class="plan-doc">'
        '<section data-reckon="followups" id="followups">'
        f"<h2>§ Followups</h2>{articles}</section>"
        "</main></body></html>",
        encoding="utf-8",
    )
    return path


def _five_cases(docs_dir: Path) -> Path:
    """Five plans: two followups that point, three that hide work."""

    _write_plan(
        docs_dir,
        "host",
        declarations=HOST_DECLARATIONS,
        followups=(
            ("f-other-plan", "/reckon-build other-plan", ""),
            ("f-own-section", "/reckon-build host §2", ""),
            ("f-done-section", "/reckon-build host §1", ""),
            ("f-no-section", "/reckon-build host", ""),
        ),
    )
    _write_plan(docs_dir, "other-plan", declarations={"s1": "implementable"})
    _write_plan(
        docs_dir,
        "omega",
        status="shipped",
        followups=(("f-shipped", "/reckon-build omega", ""),),
    )
    return docs_dir


def _rows(docs_dir: Path) -> dict[str, dict]:
    return {
        row["slug"]: row
        for row in (
            parse_plan(path) for path in sorted((docs_dir / "plans").glob("*.html"))
        )
    }


def test_the_fixture_parses_into_declarations_and_open_followups(tmp_path):
    """Positive control: the plans carry what the predicate is judged against."""

    rows = _rows(_five_cases(tmp_path / "docs"))

    assert rows["host"]["section_declarations"] == HOST_DECLARATIONS
    assert [f["id"] for f in rows["host"]["followups"]] == [
        "f-other-plan",
        "f-own-section",
        "f-done-section",
        "f-no-section",
    ]
    assert {f["status"] for f in rows["host"]["followups"]} == {"open"}
    assert rows["omega"]["status"] == "shipped"


@pytest.mark.parametrize(
    ("slug", "followup_id", "pointer", "reason"),
    [
        ("host", "f-other-plan", True, "other-plan"),
        ("host", "f-own-section", True, "implementable-section"),
        ("host", "f-done-section", False, "section-not-implementable"),
        ("host", "f-no-section", False, "no-section"),
        ("omega", "f-shipped", False, "host-complete"),
    ],
)
def test_the_predicate_answers_each_case(tmp_path, slug, followup_id, pointer, reason):
    rows = _rows(_five_cases(tmp_path / "docs"))
    followup = next(f for f in rows[slug]["followups"] if f["id"] == followup_id)

    verdict = classify_followup(rows[slug], followup, project=PROJECT)

    assert verdict.pointer is pointer
    assert verdict.reason == reason
    assert verdict.hides_work is not pointer


def test_the_predicate_falls_back_to_the_recommended_skill(tmp_path):
    docs = tmp_path / "docs"
    _write_plan(
        docs,
        "host",
        declarations=HOST_DECLARATIONS,
        followups=(
            (
                "f-dispatch-block",
                "Project: elsewhere\nPlan: host (§2)\nTier: sonnet",
                "/reckon-build host §2",
            ),
        ),
    )
    row = _rows(docs)["host"]
    (followup,) = row["followups"]

    verdict = classify_followup(row, followup, project=PROJECT)

    assert verdict.pointer is True
    assert verdict.reason == "implementable-section"


def test_the_predicate_calls_a_sprint_target_a_pointer(tmp_path):
    docs = tmp_path / "docs"
    _write_plan(
        docs,
        "host",
        declarations=HOST_DECLARATIONS,
        followups=(("f-next-sprint", "/reckon-build S23", ""),),
    )
    row = _rows(docs)["host"]
    (followup,) = row["followups"]

    verdict = classify_followup(
        row, followup, project=PROJECT, sprint_ids={"S22", "S23"}
    )

    assert verdict.pointer is True
    assert verdict.reason == "sprint"


def test_the_roadmap_reports_exactly_the_followups_that_hide_work(tmp_path):
    docs = _five_cases(tmp_path / "docs")
    inventory = list(_rows(docs).values())

    report = build_roadmap(PROJECT, inventory, [], docs_dir=docs)

    findings = [
        finding
        for finding in report["wiring_findings"]
        if finding["code"] == "followup-hides-work"
    ]
    assert [
        (finding["extra"]["followup"], finding["extra"]["reason"])
        for finding in findings
    ] == [
        ("f-done-section", "section-not-implementable"),
        ("f-no-section", "no-section"),
        ("f-shipped", "host-complete"),
    ]
    assert {finding["severity"] for finding in findings} == {"warn"}
    assert {finding["slug"] for finding in findings} == {"host", "omega"}
    assert all(
        finding["extra"]["followup"] in finding["message"] for finding in findings
    )


def test_the_roadmap_reads_declarations_and_followups_from_the_inventoried_tree(
    tmp_path,
):
    """A discovery-shaped row carries neither field; the plan file supplies both."""

    docs = _five_cases(tmp_path / "docs")
    inventory = []
    for parsed in _rows(docs).values():
        row = dict(parsed)
        row.pop("section_declarations", None)
        row.pop("followups", None)
        inventory.append(row)

    report = build_roadmap(PROJECT, inventory, [], docs_dir=docs)

    findings = [
        finding
        for finding in report["wiring_findings"]
        if finding["code"] == "followup-hides-work"
    ]
    assert [
        (finding["extra"]["followup"], finding["extra"]["reason"])
        for finding in findings
    ] == [
        ("f-done-section", "section-not-implementable"),
        ("f-no-section", "no-section"),
        ("f-shipped", "host-complete"),
    ]


def test_the_mcp_roadmap_summary_counts_a_followup_that_hides_work(
    tmp_path, monkeypatch
):
    """The path a coordinator reads: the MCP tool's summary carries the finding."""


    import reckon.mcp as mcp_module

    project = "temp-followup-mcp-project"
    docs = tmp_path / "docs"
    plan = docs / "plans" / "host.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        "<title>host</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    plan.write_text(
        write_state(
            bare,
            {
                "slug": "host",
                "title": "Host",
                "status": "active",
                "section_declarations": {"s1": "implementable"},
                "gates": [{"id": "evidence", "verdict": "passed"}],
                "followups": [
                    {"id": "f-hiding", "status": "open", "prompt": "/reckon-build host"}
                ],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}), encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    summary = mcp_module._roadmap(project, view="summary")

    assert summary["finding_counts"]["by_severity"]["warn"] == 1
    raw = mcp_module._roadmap(project)
    assert [
        (finding["extra"]["followup"], finding["extra"]["reason"])
        for finding in raw["wiring_findings"]
        if finding["code"] == "followup-hides-work"
    ] == [("f-hiding", "no-section")]
