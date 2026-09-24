"""A sprint-scoped roadmap summary carries one row per pending plan.

The summary existed to answer "which of this sprint's plans are still open,
and which can start" with counts alone, so a coordinator that needed the plan
identities fell back to the overflowing ``detail`` view or to parsing the
sprint HTML. These tests fix the two halves of the contract: a sprint-scoped
summary lists its pending plans in a compact row each, and a summary read
without a sprint is unchanged to the byte.
"""

from __future__ import annotations

import json

import pytest

from reckon._plan_html import write_state
from reckon.mcp_views import roadmap_view
from reckon.roadmap import build_roadmap

SPRINT = "sprint-x"

COMPACT_KEYS = {
    "slug",
    "status",
    "impl",
    "ready",
    "blocking",
    "implementable_sections",
}


def _plan(
    slug: str,
    *,
    status: str = "active",
    impl: float = 0.0,
    depends_on: list[str] | None = None,
    declarations: dict[str, str] | None = None,
) -> dict:
    plan = {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": status,
        "sprint": SPRINT,
        "impl": impl,
        "depends_on": depends_on or [],
        "blocking": [],
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "next", "status": "open"}],
    }
    if declarations is not None:
        plan["section_declarations"] = declarations
    return plan


@pytest.fixture()
def inventory() -> list[dict]:
    """One shipped plan, one ready plan and one blocked plan, one sprint."""

    return [
        _plan("landed-work", status="shipped", impl=1.0),
        _plan(
            "ready-work",
            impl=0.25,
            declarations={"s1": "implementable", "s2": "implementable", "s3": "done"},
        ),
        _plan(
            "blocked-work",
            impl=0.5,
            depends_on=["ready-work"],
            declarations={"s4": "implementable", "s5": "deferred"},
        ),
    ]


@pytest.fixture()
def scoped_report(inventory):
    return build_roadmap("sample", inventory, [], sprint_id=SPRINT)


def test_summary_lists_the_sprints_pending_plans(scoped_report) -> None:
    summary = roadmap_view(scoped_report, view="summary", cursor=None, limit=None)

    rows = summary["pending_plans"]
    assert [row["slug"] for row in rows] == ["ready-work", "blocked-work"]
    assert {frozenset(row) for row in rows} == {frozenset(COMPACT_KEYS)}


def test_pending_row_carries_status_impl_and_readiness(scoped_report) -> None:
    summary = roadmap_view(scoped_report, view="summary", cursor=None, limit=None)
    by_slug = {row["slug"]: row for row in summary["pending_plans"]}

    ready = by_slug["ready-work"]
    assert ready["status"] == "active"
    assert ready["impl"] == 0.25
    assert ready["ready"] is True
    assert ready["blocking"] == []

    blocked = by_slug["blocked-work"]
    assert blocked["status"] == "active"
    assert blocked["impl"] == 0.5
    assert blocked["ready"] is False
    assert blocked["blocking"] == ["ready-work"]


def test_pending_row_carries_implementable_section_ids(scoped_report) -> None:
    summary = roadmap_view(scoped_report, view="summary", cursor=None, limit=None)
    by_slug = {row["slug"]: row for row in summary["pending_plans"]}

    assert by_slug["ready-work"]["implementable_sections"] == ["s1", "s2"]
    assert by_slug["blocked-work"]["implementable_sections"] == ["s4"]

    # The blocked plan is held by a plain plan dependency, not a section-scoped
    # one, so the row that carries its section ids is the plan's own
    # declaration projection rather than a section-blocker arm.
    blocked_row = next(
        row for row in scoped_report["pending_work"] if row["slug"] == "blocked-work"
    )
    assert "section_readiness" not in blocked_row
    assert "ready_sections" not in blocked_row


def test_shipped_plan_is_absent_from_the_pending_rows(scoped_report) -> None:
    summary = roadmap_view(scoped_report, view="summary", cursor=None, limit=None)

    slugs = {row["slug"] for row in summary["pending_plans"]}
    assert "landed-work" not in slugs
    assert not any(
        row["slug"] == "landed-work" for row in scoped_report["pending_work"]
    )


def test_section_ids_come_from_the_plan_file_when_the_row_lacks_them(tmp_path) -> None:
    """A discovery row carries no classification; the plan file does.

    The MCP summary inventories slim rows, so reading only the row would report
    an empty section column for every plan — a false zero. The row falls back to
    the plan's own file, which is the same rule the wiring scan follows.
    """

    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True, exist_ok=True)
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        "<title>declared</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    (docs / "plans" / "declared.html").write_text(
        write_state(
            bare,
            {
                "slug": "declared",
                "title": "Declared",
                "status": "active",
                "sprint": SPRINT,
                "section_declarations": {"s1": "implementable", "s2": "done"},
                "gates": [{"id": "evidence", "verdict": "passed"}],
                "followups": [{"id": "next", "status": "open"}],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )

    slim = _plan("declared")
    assert "section_declarations" not in slim
    report = build_roadmap("sample", [slim], [], sprint_id=SPRINT, docs_dir=docs)
    summary = roadmap_view(report, view="summary", cursor=None, limit=None)

    assert summary["pending_plans"][0]["implementable_sections"] == ["s1"]


def test_summary_without_sprint_is_unchanged(inventory) -> None:
    report = build_roadmap("sample", inventory, [])
    summary = roadmap_view(report, view="summary", cursor=None, limit=None)

    assert "pending_plans" not in summary
    assert json.dumps(summary, sort_keys=True) == NO_SPRINT_SUMMARY_JSON


# The byte-exact summary of the fixture above as produced before the change.
NO_SPRINT_SUMMARY_JSON = '{"blocked": 1, "completion": {"completed": 1, "implementation_pct": 58.3, "lifecycle_completion_pct": 33.3, "pending": 2, "plans": 3}, "critical_path": {"effort_unit": "worker-hours", "length_hours": 2.5, "length_unit": "elapsed-hours", "plans": ["ready-work", "blocked-work"], "uncalibrated_count": 2, "uncalibrated_plans": ["blocked-work", "ready-work"], "worker_hours": 2.5}, "deferred": 0, "dependency_readiness": {"blocked": 1, "deferred": 0, "ready": 1}, "finding_counts": {"by_severity": {"warn": 3}, "total": 3}, "project": "sample", "ready": 1, "schedule_readiness": {"configuration_key": "schedule_horizon_sprints", "configured": false, "deferred": 0, "earliest_open_sprint": null, "horizon_depth": 0, "open_sprints": [], "ready": 2, "ready_sprints": [], "window_sprints": null}, "view": "summary"}'
