from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

from reckon.mcp_views import _blocking
from reckon.roadmap import build_roadmap
from reckon.serve import _derive_lifecycle


def _plan(
    slug: str,
    *,
    status: str = "active",
    impl: float = 0.0,
    depends_on: list[str] | None = None,
    sprint: str | None = None,
    effort: str = "M",
) -> dict:
    return {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": status,
        "impl": impl,
        "depends_on": depends_on or [],
        "sprint": sprint,
        "effort": effort,
        "roi": "high",
        "blocking": [],
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "next", "status": "open"}],
    }


def test_roadmap_returns_ready_work_and_weighted_critical_path() -> None:
    inventory = [
        _plan("foundation", effort="S"),
        _plan("integration", depends_on=["foundation"], effort="L"),
        _plan("release", depends_on=["integration"], effort="M"),
    ]
    sprints = [
        {
            "id": "current",
            "status": "active",
            "items": ["foundation", "integration", "release"],
        }
    ]

    result = build_roadmap("sample", inventory, sprints, active_sprint_id="current")

    assert [item["slug"] for item in result["ready_now"]] == ["foundation"]
    assert result["critical_path"] == {
        "plans": ["foundation", "integration", "release"],
        "remaining_effort": 7.0,
    }
    assert result["immediate_roadmap"][0]["reason"] == "critical path"
    assert result["completion"]["pending"] == 3
    assert result["sprints"][0]["ready"] == 1


def test_completed_dependency_clears_without_hiding_stored_progress() -> None:
    inventory = [
        _plan("foundation", status="shipped", impl=1.0),
        _plan("integration", depends_on=["foundation"], impl=0.25),
    ]

    result = build_roadmap("sample", inventory, [])

    assert [item["slug"] for item in result["ready_now"]] == ["integration"]
    assert result["completion"]["lifecycle_completion_pct"] == 50.0
    assert result["completion"]["implementation_pct"] == 62.5


def test_reference_input_in_depends_on_is_a_wiring_error() -> None:
    inventory = [
        _plan("consumer", depends_on=["survey"]),
        {"slug": "survey", "title": "Survey", "type": "research"},
    ]

    result = build_roadmap("sample", inventory, [])

    finding = next(
        item
        for item in result["wiring_findings"]
        if item["code"] == "non-executable-hard-dependency"
    )
    assert finding["severity"] == "error"
    assert "use informs" in finding["message"]
    assert result["ready_now"] == []


def test_cycles_and_sprint_order_inversions_are_reported() -> None:
    inventory = [
        _plan("early", depends_on=["later"], sprint="first"),
        _plan("later", depends_on=["early"], sprint="second"),
    ]
    sprints = [
        {"id": "first", "status": "active", "items": ["early"]},
        {"id": "second", "status": "planned", "items": ["later"]},
    ]

    result = build_roadmap("sample", inventory, sprints)
    codes = {item["code"] for item in result["wiring_findings"]}

    assert "dependency-cycle" in codes
    assert "sprint-order-inversion" in codes
    assert result["cycles"] == [["early", "later", "early"]]
    assert result["ready_now"] == []


def test_sprint_scope_includes_transitive_prerequisites() -> None:
    inventory = [
        _plan("foundation"),
        _plan("selected", depends_on=["foundation"], sprint="target"),
        _plan("unrelated"),
    ]
    sprints = [{"id": "target", "status": "active", "items": ["selected"]}]

    result = build_roadmap("sample", inventory, sprints, sprint_id="target")

    assert {item["slug"] for item in result["pending_work"]} == {
        "foundation",
        "selected",
    }
    assert result["scope"] == {"sprint": "target", "plans": 2}


def test_explicit_project_scope_is_exposed_for_allocation_preflight() -> None:
    result = build_roadmap(
        "sample",
        [_plan("work")],
        [],
        project_manifest={
            "scope": {
                "owns": ["runtime orchestration"],
                "routes": [{"work": "vocabulary", "project": "language"}],
            }
        },
    )

    assert result["allocation"]["configured"] is True
    assert result["allocation"]["scope"]["owns"] == ["runtime orchestration"]


def test_terminal_plan_wiring_does_not_pollute_immediate_roadmap() -> None:
    result = build_roadmap(
        "sample",
        [_plan("historical", status="shipped", impl=1.0, depends_on=["removed"])],
        [{"id": "closed", "status": "done", "items": []}],
    )

    assert result["wiring_findings"] == []
    assert result["sprints"][0]["lifecycle_completion_pct"] == 100.0
    assert result["sprints"][0]["implementation_pct"] == 100.0


def test_terminal_sprint_item_does_not_expand_scoped_prerequisites() -> None:
    result = build_roadmap(
        "sample",
        [
            _plan("complete", status="shipped", impl=1.0, depends_on=["old-work"]),
            _plan("old-work"),
        ],
        [{"id": "current", "status": "active", "items": ["complete"]}],
        sprint_id="current",
    )

    assert result["scope"] == {"sprint": "current", "plans": 1}
    assert result["pending_work"] == []


def test_archived_plan_is_excluded_from_live_roadmap() -> None:
    historical = _plan(
        "historical", status="active", depends_on=["missing"], sprint="closed"
    )
    historical["archived"] = True

    result = build_roadmap(
        "sample",
        [historical, _plan("current", sprint="active")],
        [
            {"id": "closed", "status": "done", "items": []},
            {"id": "active", "status": "active", "items": ["current"]},
        ],
    )

    assert result["scope"]["plans"] == 1
    assert [item["slug"] for item in result["ready_now"]] == ["current"]
    assert result["wiring_findings"] == []


def test_closed_sprint_history_does_not_duplicate_live_membership() -> None:
    result = build_roadmap(
        "sample",
        [_plan("continued", sprint="current")],
        [
            {"id": "closed", "status": "done", "items": ["continued"]},
            {"id": "current", "status": "active", "items": ["continued"]},
        ],
    )

    assert result["wiring_findings"] == []
    assert result["pending_work"][0]["sprint"] == "current"


def test_dispatchability_is_derived_separately_from_authorisation() -> None:
    without_gate = _plan("without-gate")
    without_gate["gates"] = []
    without_gate["followups"] = []
    without_followup = _plan("without-followup")
    without_followup["followups"] = []
    result = build_roadmap(
        "sample",
        [
            _plan("authorised"),
            _plan("awaiting-authorisation", status="draft"),
            without_gate,
            without_followup,
        ],
        [],
    )

    rows = {row["slug"]: row for row in result["pending_work"]}
    assert rows["authorised"]["dispatchable"] is True
    assert rows["authorised"]["authorised"] is True
    assert rows["authorised"]["ready"] is True
    assert rows["awaiting-authorisation"]["dispatchable"] is True
    assert rows["awaiting-authorisation"]["authorised"] is False
    assert rows["awaiting-authorisation"]["ready"] is False
    assert rows["without-gate"]["dispatchable"] is True
    assert rows["without-gate"]["missing_dispatchability"] == []
    assert rows["without-gate"]["ready"] is True
    assert rows["without-followup"]["missing_dispatchability"] == ["open_followup"]
    assert result["blocked"] == []
    assert {item["slug"] for item in result["deferred"]} == {
        "awaiting-authorisation",
        "without-followup",
    }


def test_authorisation_report_lists_every_draft_with_age_and_decay_verdict() -> None:
    today = date.today()
    inventory = []
    for slug, age_days in (
        ("recent-draft", 12),
        ("older-one", 61),
        ("older-two", 75),
        ("older-three", 90),
        ("older-four", 120),
    ):
        plan = _plan(slug, status="draft")
        plan["modified"] = (today - timedelta(days=age_days)).isoformat()
        inventory.append(plan)
    authorised = _plan("authorised")
    authorised["modified"] = (today - timedelta(days=180)).isoformat()
    inventory.append(authorised)

    result = build_roadmap("sample", inventory, [])
    report = result["authorisation"]
    rows = {row["slug"]: row for row in report["authored_but_unauthorised"]}

    assert report["count"] == 5
    assert report["stale_count"] == 4
    assert set(rows) == {
        "recent-draft",
        "older-one",
        "older-two",
        "older-three",
        "older-four",
    }
    assert {slug: rows[slug]["age_days"] for slug in rows} == {
        "recent-draft": 12,
        "older-one": 61,
        "older-two": 75,
        "older-three": 90,
        "older-four": 120,
    }
    assert rows["recent-draft"]["age_verdict"] == "current"
    assert all(
        rows[f"older-{name}"]["age_verdict"] == "stale"
        for name in ("one", "two", "three", "four")
    )
    assert {row["age_source"] for row in rows.values()} == {"plan-modified"}


def test_authorisation_report_derives_age_from_existing_plan_file(
    tmp_path, monkeypatch
) -> None:
    docs_dir = tmp_path / "docs"
    plan_path = docs_dir / "plans" / "unmodified.html"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="unmodified">
<meta name="plan-status" content="draft">
</head><body></body></html>
"""
    )
    modified = date.today() - timedelta(days=17)
    timestamp = datetime.combine(modified, time.min).timestamp()
    os.utime(plan_path, (timestamp, timestamp))
    monkeypatch.setattr("reckon.roadmap._load_mounts", lambda: {"sample": docs_dir})

    result = build_roadmap("sample", [_plan("unmodified", status="draft")], [])
    row = result["authorisation"]["authored_but_unauthorised"][0]

    assert row["age_days"] == 17
    assert row["age_source"] == "file-mtime"
    assert row["age_verdict"] == "current"


def test_gate_verdict_blocks_and_releases_downstream_sections_without_status_edit() -> (
    None
):
    plan = _plan("measured-work")
    plan["gates"] = [
        {
            "id": "coverage",
            "section": "evaluation",
            "gated_sections": ["deployment", "release"],
            "status": "closed",
            "measure": "Coverage threshold",
            "verdict": "failed",
            "evidence": "/sample/evidence/coverage",
        }
    ]
    sprint = [{"id": "current", "status": "active", "items": ["measured-work"]}]

    typed_blocking = _blocking(plan, [])
    discovered, _hydrated = _derive_lifecycle("sample", [plan], sprint)
    roadmap = build_roadmap("sample", discovered, sprint)

    expected_sections = ["deployment", "release"]
    assert typed_blocking[0]["gated_sections"] == expected_sections
    assert discovered[0]["blocking"][0]["gated_sections"] == expected_sections
    assert discovered[0]["effective_status"] == "blocked"
    assert (
        roadmap["blocked"][0]["gate_blockers"][0]["gated_sections"] == expected_sections
    )
    assert roadmap["blocked"][0]["effective_status"] == "blocked"
    assert roadmap["ready_now"] == []

    workflow_status = plan["status"]
    plan["gates"][0]["verdict"] = "passed"

    typed_blocking = _blocking(plan, [])
    discovered, _hydrated = _derive_lifecycle("sample", [plan], sprint)
    roadmap = build_roadmap("sample", discovered, sprint)

    assert plan["status"] == workflow_status == "active"
    assert typed_blocking == []
    assert discovered[0]["blocking"] == []
    assert discovered[0]["effective_status"] == "active"
    assert roadmap["blocked"] == []
    assert roadmap["ready_now"][0]["slug"] == "measured-work"
    assert roadmap["ready_now"][0]["gate_blockers"] == []
    assert roadmap["ready_now"][0]["effective_status"] == "active"
