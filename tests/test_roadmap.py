from __future__ import annotations

from reckon.roadmap import build_roadmap


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


def test_non_runnable_draft_is_deferred_without_becoming_a_blocker() -> None:
    result = build_roadmap(
        "sample",
        [_plan("future", status="draft", sprint="later")],
        [{"id": "later", "status": "planned", "items": ["future"]}],
    )

    assert result["ready_now"] == []
    assert result["blocked"] == []
    assert [item["slug"] for item in result["deferred"]] == ["future"]
    assert result["deferred"][0]["readiness"] == "deferred"
    assert result["sprints"][0]["deferred"] == 1
