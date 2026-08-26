from __future__ import annotations

from datetime import date, timedelta

import pytest

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
    effort_hours: float | None = None,
    effort_calibrated: bool | None = None,
    north_star: str | None = None,
) -> dict:
    plan = {
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
    if north_star is not None:
        plan["north_star"] = north_star
    if effort_hours is not None:
        plan["effort_hours"] = effort_hours
    if effort_calibrated is not None:
        plan["effort_calibrated"] = effort_calibrated
    return plan


def _project_with_north_stars(*ids: str) -> dict:
    return {
        "north_stars": [
            {
                "id": north_star_id,
                "name": north_star_id.replace("-", " ").title(),
                "statement": f"{north_star_id} remains the durable direction.",
            }
            for north_star_id in ids
        ]
    }


def _section_gate(section: str, *, passed: bool = True) -> dict:
    return {
        "id": f"{section}-evidence",
        "section": section,
        "gated_sections": [],
        "status": "closed" if passed else "open",
        "measure": f"Evidence for {section}",
        "verdict": "passed" if passed else "",
        "evidence": "recorded" if passed else "",
    }


def test_roadmap_returns_ready_work_and_hourly_critical_path() -> None:
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
        # No plan declares wall-clock, so elapsed falls back to the serial
        # assumption and matches the labour total.
        "length_hours": 7.0,
        "length_unit": "elapsed-hours",
        "worker_hours": 7.0,
        "effort_unit": "worker-hours",
        "uncalibrated_plans": ["foundation", "integration", "release"],
        "uncalibrated_count": 3,
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


def test_section_dependency_blocks_only_its_matching_section() -> None:
    prerequisite = _plan("catalog")
    prerequisite["gates"] = [
        _section_gate("s1"),
        _section_gate("s2"),
        _section_gate("s3", passed=False),
    ]
    consumer = _plan("consumer", depends_on=["catalog#s3"], effort_hours=8.0)
    consumer["gates"] = [
        _section_gate("s1"),
        _section_gate("s2"),
        _section_gate("s3"),
    ]

    result = build_roadmap("sample", [prerequisite, consumer], [])
    row = next(item for item in result["pending_work"] if item["slug"] == "consumer")

    assert row["ready"] is True
    assert row["effective_status"] == "active"
    assert [item["slug"] for item in result["ready_now"]] == ["consumer"]
    assert row["ready_sections"] == ["s1", "s2"]
    assert row["blocked_sections"] == ["s3"]
    assert row["section_readiness"] == [
        {"section": "s1", "ready": True, "blockers": []},
        {"section": "s2", "ready": True, "blockers": []},
        {
            "section": "s3",
            "ready": False,
            "blockers": [
                {
                    "ref": "catalog#s3",
                    "scope": "local",
                    "slug": "catalog",
                    "stage": "s3",
                    "found": True,
                    "section_found": True,
                    "status": "active",
                    "satisfied": False,
                }
            ],
        },
    ]


def test_section_dependency_reports_an_anchor_absent_from_its_target() -> None:
    prerequisite = _plan("catalog")
    prerequisite["gates"] = [_section_gate("parser"), _section_gate("writer")]
    consumer = _plan("consumer", depends_on=["catalog#deployment"])
    consumer["gates"] = [_section_gate("deployment")]

    result = build_roadmap("sample", [prerequisite, consumer], [])
    finding = next(
        item
        for item in result["wiring_findings"]
        if item["code"] == "missing-dependency-section"
    )

    assert finding["severity"] == "error"
    assert finding["slug"] == "consumer"
    assert finding["extra"] == {
        "ref": "catalog#deployment",
        "section": "deployment",
        "target": "catalog",
    }
    assert (
        next(item for item in result["pending_work"] if item["slug"] == "consumer")[
            "ready"
        ]
        is True
    )


def test_unstaged_dependency_remains_plan_wide() -> None:
    prerequisite = _plan("catalog")
    consumer = _plan("consumer", depends_on=["catalog"])

    result = build_roadmap("sample", [prerequisite, consumer], [])
    row = next(item for item in result["pending_work"] if item["slug"] == "consumer")

    assert row["ready"] is False
    assert row["effective_status"] == "blocked"
    assert row["depends_on"] == [
        {
            "ref": "catalog",
            "scope": "local",
            "slug": "catalog",
            "found": True,
            "status": "active",
            "satisfied": False,
        }
    ]
    assert "section_readiness" not in row


def test_open_decision_blocks_its_plan_with_a_distinct_blocker_kind() -> None:
    deciding = _plan("deciding")
    deciding["decisions"] = [
        {
            "key": "transport",
            "title": "Which transport should carry the payload?",
            "choice": "",
            "rationale": "",
        }
    ]
    endpoint = _plan("endpoint", depends_on=["deciding"])
    result = build_roadmap("sample", [deciding, endpoint], [])
    rows = {row["slug"]: row for row in result["pending_work"]}

    blocker = {
        "kind": "decision",
        "plan": "deciding",
        "id": "transport",
        "question": "Which transport should carry the payload?",
        "status": "open",
        "choice": "",
        "rationale": "",
    }
    assert rows["deciding"]["decision_blockers"] == [blocker]
    assert rows["deciding"]["depends_on"] == []
    assert rows["deciding"]["explicit_blockers"] == []
    assert rows["deciding"]["gate_blockers"] == []
    assert rows["deciding"]["readiness"] == "blocked"
    assert result["decision_blockers"] == [blocker]
    assert result["decision_readiness"] == {
        "ready": False,
        "open": 1,
        "deferred": 0,
    }
    assert "deciding" not in {row["slug"] for row in result["ready_now"]}

    deciding["decisions"][0]["choice"] = "socket"
    released = build_roadmap("sample", [deciding, endpoint], [])

    assert released["decision_blockers"] == []
    assert released["decision_readiness"] == {
        "ready": True,
        "open": 0,
        "deferred": 0,
    }
    assert "deciding" in {row["slug"] for row in released["ready_now"]}


def test_locked_or_deferred_decision_releases_work_and_stays_reported() -> None:
    locked = _plan("locked")
    locked["decisions"] = [
        {
            "key": "transport",
            "title": "Which transport?",
            "choice": "socket",
            "rationale": "Selected for bounded delivery.",
        }
    ]
    deferred = _plan("deferred")
    deferred["decisions"] = [
        {
            "key": "retention",
            "title": "How long should records remain?",
            "choice": "",
            "rationale": "Deferred until retention policy is published.",
        }
    ]

    result = build_roadmap("sample", [locked, deferred], [])
    rows = {row["slug"]: row for row in result["pending_work"]}

    assert rows["locked"]["readiness"] == "ready"
    assert rows["locked"]["decision_blockers"] == []
    assert rows["locked"]["decisions"][0]["status"] == "locked"
    assert rows["deferred"]["readiness"] == "ready"
    assert rows["deferred"]["decision_blockers"] == []
    assert rows["deferred"]["deferred_decisions"][0] == {
        "kind": "decision",
        "plan": "deferred",
        "id": "retention",
        "question": "How long should records remain?",
        "status": "deferred",
        "choice": "",
        "rationale": "Deferred until retention policy is published.",
    }
    assert result["decision_blockers"] == []
    assert result["deferred_decisions"] == rows["deferred"]["deferred_decisions"]
    assert result["decision_readiness"] == {
        "ready": True,
        "open": 0,
        "deferred": 1,
    }
    assert {row["slug"] for row in result["ready_now"]} == {"locked", "deferred"}


def test_plans_without_decisions_keep_existing_readiness() -> None:
    result = build_roadmap("sample", [_plan("unaffected")], [])
    row = result["pending_work"][0]

    assert row["readiness"] == "ready"
    assert row["decision_blockers"] == []
    assert row["deferred_decisions"] == []
    assert row["decisions"] == []
    assert result["decision_blockers"] == []
    assert result["deferred_decisions"] == []
    assert result["decision_readiness"] == {
        "ready": True,
        "open": 0,
        "deferred": 0,
    }


def test_schedule_readiness_uses_the_configured_open_sprint_window() -> None:
    inventory = [
        _plan("earliest", sprint="first"),
        _plan("inside-window", sprint="second"),
        _plan("queued", sprint="third"),
        _plan("finished", status="shipped", impl=1.0, sprint="closed"),
    ]
    sprints = [
        {"id": "closed", "status": "done", "items": ["finished"]},
        {"id": "first", "status": "active", "items": ["earliest"]},
        {"id": "second", "status": "planned", "items": ["inside-window"]},
        {"id": "third", "status": "planned", "items": ["queued"]},
    ]

    result = build_roadmap(
        "sample",
        inventory,
        sprints,
        project_manifest={"schedule_horizon_sprints": 2},
    )
    rows = {row["slug"]: row for row in result["pending_work"]}
    ordered = {row["slug"]: row for row in result["immediate_roadmap"]}

    assert result["schedule"] == {
        "configured": True,
        "configuration_key": "schedule_horizon_sprints",
        "window_sprints": 2,
        "horizon_depth": 3,
        "open_sprints": ["first", "second", "third"],
        "earliest_open_sprint": "first",
        "ready_sprints": ["first", "second"],
        "ready": 2,
        "deferred": 1,
    }
    assert rows["inside-window"]["dependency_ready"] is True
    assert rows["inside-window"]["schedule_ready"] is True
    assert rows["queued"]["dependency_ready"] is True
    assert rows["queued"]["dependency_readiness"] == "ready"
    assert rows["queued"]["schedule_readiness"] == "deferred"
    assert rows["queued"]["schedule_behind_sprint"] == "second"
    assert "second" in rows["queued"]["schedule_deferred_reason"]
    assert [row["slug"] for row in result["schedule_deferred"]] == ["queued"]
    assert "queued" in {row["slug"] for row in result["ready_now"]}
    assert ordered["queued"]["schedule_readiness"] == "deferred"

    wider = build_roadmap(
        "sample",
        inventory,
        sprints,
        project_manifest={"schedule_horizon_sprints": 3},
    )
    assert wider["schedule"]["window_sprints"] == 3
    assert wider["schedule"]["deferred"] == 0
    assert wider["schedule_deferred"] == []


def test_north_star_rollup_reports_alignment_completion_and_remaining_effort() -> None:
    inventory = [
        _plan("finished", status="shipped", impl=1.0, north_star="reliability"),
        _plan("partial", impl=0.25, effort="L", north_star="reliability"),
        _plan("usable", effort="S", north_star="usability"),
    ]

    result = build_roadmap(
        "sample",
        inventory,
        [],
        project_manifest=_project_with_north_stars(
            "reliability", "usability", "unclaimed"
        ),
    )

    assert result["north_stars"] == [
        {
            "id": "reliability",
            "name": "Reliability",
            "statement": "reliability remains the durable direction.",
            "plans": 2,
            "completed": 1,
            "lifecycle_completion_pct": 50.0,
            "effort_unit": "worker-hours",
            "remaining_effort_hours": 3.0,
            "uncalibrated_plans": ["partial"],
            "uncalibrated_count": 1,
        },
        {
            "id": "usability",
            "name": "Usability",
            "statement": "usability remains the durable direction.",
            "plans": 1,
            "completed": 0,
            "lifecycle_completion_pct": 0.0,
            "effort_unit": "worker-hours",
            "remaining_effort_hours": 1.0,
            "uncalibrated_plans": ["usable"],
            "uncalibrated_count": 1,
        },
        {
            "id": "unclaimed",
            "name": "Unclaimed",
            "statement": "unclaimed remains the durable direction.",
            "plans": 0,
            "completed": 0,
            "lifecycle_completion_pct": 0.0,
            "effort_unit": "worker-hours",
            "remaining_effort_hours": 0,
            "uncalibrated_plans": [],
            "uncalibrated_count": 0,
        },
    ]


def test_explicit_hours_win_over_legacy_letter_in_remaining_arithmetic() -> None:
    result = build_roadmap(
        "sample",
        [
            _plan(
                "estimated",
                impl=0.25,
                effort="XL",
                effort_hours=5.0,
                effort_calibrated=True,
            )
        ],
        [],
    )

    row = result["pending_work"][0]
    assert row["effort_hours"] == 5.0
    assert row["remaining_effort_hours"] == 3.75
    assert row["effort_calibrated"] is True


def test_legacy_letter_maps_to_hours_and_is_reported_uncalibrated() -> None:
    result = build_roadmap("sample", [_plan("legacy", effort="L")], [])

    row = result["pending_work"][0]
    assert row["effort_hours"] == 4.0
    assert row["remaining_effort_hours"] == 4.0
    assert row["effort_calibrated"] is False
    assert result["effort"]["uncalibrated_plans"] == ["legacy"]
    assert result["effort"]["uncalibrated_count"] == 1


def test_open_path_length_equals_its_plans_remaining_hours() -> None:
    inventory = [
        _plan(
            "foundation",
            impl=0.5,
            effort_hours=2.5,
            effort_calibrated=True,
        ),
        _plan("consumer", depends_on=["foundation"], effort="L", impl=0.25),
    ]

    result = build_roadmap("sample", inventory, [])
    rows = {row["slug"]: row for row in result["pending_work"]}
    path = result["open_paths"][0]
    summed_hours = sum(rows[slug]["remaining_effort_hours"] for slug in path["plans"])

    assert path["effort_unit"] == "worker-hours"
    assert path["length_hours"] == pytest.approx(summed_hours, abs=0.25)
    assert path["uncalibrated_plans"] == ["consumer"]
    assert path["uncalibrated_count"] == 1


def test_effort_summary_names_unit_and_reports_remaining_hours() -> None:
    inventory = [
        _plan(
            "calibrated",
            impl=0.5,
            effort_hours=3.0,
            effort_calibrated=True,
        ),
        _plan("legacy", effort="S"),
        _plan("finished", status="shipped", impl=1.0, effort="XL"),
    ]

    result = build_roadmap("sample", inventory, [])

    assert result["effort"] == {
        "unit": "worker-hours",
        "remaining_hours": 2.5,
        "remaining_wall_hours": 2.5,
        "wall_clock_unit": "elapsed-hours",
        "uncalibrated_plans": ["legacy"],
        "uncalibrated_count": 1,
    }


def test_live_actionable_plan_without_north_star_is_informational() -> None:
    inventory = [
        _plan("actionable"),
        _plan("unauthorised", status="draft"),
        _plan("complete", status="shipped", impl=1.0),
    ]

    result = build_roadmap(
        "sample",
        inventory,
        [],
        project_manifest=_project_with_north_stars("reliability"),
    )

    findings = [
        item for item in result["wiring_findings"] if item["code"] == "unoriented-plan"
    ]
    # A draft is live actionable work, so it is held to the same orientation
    # expectation as any other plan.
    assert sorted((item["slug"], item["severity"]) for item in findings) == [
        ("actionable", "info"),
        ("unauthorised", "info"),
    ]


def test_plan_naming_undeclared_north_star_is_a_wiring_error() -> None:
    result = build_roadmap(
        "sample",
        [_plan("misdirected", north_star="missing")],
        [],
        project_manifest=_project_with_north_stars("reliability"),
    )

    finding = next(
        item
        for item in result["wiring_findings"]
        if item["code"] == "undeclared-north-star"
    )
    assert finding["severity"] == "error"
    assert finding["slug"] == "misdirected"
    assert finding["extra"] == {"north_star": "missing"}


def test_project_without_north_stars_has_no_orientation_findings() -> None:
    result = build_roadmap(
        "sample",
        [_plan("unlabelled"), _plan("labelled", north_star="not-declared")],
        [],
        project_manifest={},
    )

    assert result["north_stars"] == []
    assert not {item["code"] for item in result["wiring_findings"]} & {
        "unoriented-plan",
        "undeclared-north-star",
    }


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
            _plan("drafting", status="draft"),
            without_gate,
            without_followup,
        ],
        [],
    )

    rows = {row["slug"]: row for row in result["pending_work"]}
    assert rows["authorised"]["dispatchable"] is True
    assert rows["authorised"]["authorised"] is True
    assert rows["authorised"]["ready"] is True
    # Drafting is how a plan gets written, not a permission tier: a draft is
    # implementable the moment it exists and must never queue as unauthorised.
    assert rows["drafting"]["dispatchable"] is True
    assert rows["drafting"]["authorised"] is True
    assert rows["drafting"]["ready"] is True
    assert result["authorisation"]["authored_but_unauthorised"] == []
    # Dispatchability remains its own axis: a plan can be authorised and still
    # be undispatchable because it is missing the parts a worker needs.
    assert rows["without-gate"]["dispatchable"] is True
    assert rows["without-gate"]["missing_dispatchability"] == []
    assert rows["without-gate"]["ready"] is True
    assert rows["without-followup"]["missing_dispatchability"] == ["open_followup"]
    assert result["blocked"] == []
    # Only the undispatchable plan defers now; drafting no longer parks work.
    assert {item["slug"] for item in result["deferred"]} == {"without-followup"}


def test_wall_clock_and_worker_hours_are_predicted_separately() -> None:
    """Labour and elapsed time are different quantities and must not collapse.

    A plan that fans out costs the same worker-hours but blocks the schedule
    for less time. Path length is elapsed time, because plans on a path run one
    after another while each plan internally parallelises.
    """
    fanned = _plan("fanned", effort_hours=12.0)
    fanned["wall_clock_hours"] = 3.0
    serial = _plan("serial", effort_hours=4.0)
    serial["depends_on"] = ["fanned"]

    result = build_roadmap("sample", [fanned, serial], [])
    rows = {row["slug"]: row for row in result["pending_work"]}

    assert rows["fanned"]["effort_hours"] == 12.0
    assert rows["fanned"]["wall_clock_hours"] == 3.0
    # Undeclared wall-clock falls back to the serial assumption, never a guess.
    assert rows["serial"]["wall_clock_hours"] == 4.0

    assert result["effort"]["remaining_hours"] == 16.0
    assert result["effort"]["remaining_wall_hours"] == 7.0

    critical = result["critical_path"]
    assert critical["plans"] == ["fanned", "serial"]
    assert critical["length_hours"] == 7.0
    assert critical["length_unit"] == "elapsed-hours"
    assert critical["worker_hours"] == 16.0


def test_declared_wall_clock_cannot_exceed_the_serial_total() -> None:
    """Parallelism can only shorten a plan; a larger figure is clamped."""
    impossible = _plan("impossible", effort_hours=4.0)
    impossible["wall_clock_hours"] = 9.0

    result = build_roadmap("sample", [impossible], [])
    row = result["pending_work"][0]

    assert row["effort_hours"] == 4.0
    assert row["wall_clock_hours"] == 4.0


def test_drafts_never_queue_as_unauthorised_however_old() -> None:
    """Age must not turn a draft into work that reads as needing permission.

    Drafting is how a plan gets written. The authorisation report exists for
    states that genuinely must not run; a plan sitting in draft for four months
    is simply an old plan, and parking it behind a permission tier hides
    implementable work behind a label nobody has to clear.
    """
    today = date.today()
    inventory = []
    for slug, age_days in (("recent-draft", 12), ("ancient-draft", 120)):
        plan = _plan(slug, status="draft")
        plan["modified"] = (today - timedelta(days=age_days)).isoformat()
        inventory.append(plan)

    result = build_roadmap("sample", inventory, [])
    report = result["authorisation"]

    assert report["authored_but_unauthorised"] == []
    assert report["count"] == 0
    assert report["stale_count"] == 0
    rows = {row["slug"]: row for row in result["pending_work"]}
    assert rows["ancient-draft"]["authorised"] is True
    assert rows["ancient-draft"]["ready"] is True
    assert {item["slug"] for item in result["ready_now"]} == {
        "recent-draft",
        "ancient-draft",
    }


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
