"""Progressive MCP response views and legacy compatibility."""

from __future__ import annotations

import importlib
import inspect
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon.doccheck import ACTIVE_PLAN_STALE_AFTER_DAYS
from reckon.mcp_views import (
    ResourceSelector,
    audit_view,
    compact_size,
    discovery_view,
    error_response,
    in_flight_by_plan,
    resource_view,
    storage_schema_for,
)
from reckon.roadmap import build_roadmap


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts_file
    serve_module._STATE_ROOT = state_root
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    return docs_dir, project


def _plan(
    docs_dir: Path,
    slug: str,
    *,
    relative: str | None = None,
    summary: str = "A readable plan.",
    resource_type: str = "plan",
    project: str = "proj",
    modified: str | None = None,
) -> Path:
    from reckon._plan_html import write_state

    state = {
        "slug": slug,
        "title": "Readable Plan",
        "summary": summary,
        "version": 3,
        "type": resource_type,
    }
    if resource_type == "plan":
        state.update(
            {
                "status": "active",
                "impl": 0.4,
                "decisions": {
                    "open-choice": {
                        "title": "Choose a route",
                        "choices": ["a", "b"],
                        "choice": "",
                    },
                    "locked-choice": {
                        "title": "Locked",
                        "choices": ["x", "y"],
                        "choice": "x",
                    },
                },
                "followups": [
                    {
                        "id": "next",
                        "status": "open",
                        "title": "Continue",
                        "body": "<p>Do the next thing.</p>",
                        "prompt": "Project: proj\nDone-when\n  1. shipped",
                        "written_by": "test",
                        "written_at": "2026-01-01",
                    },
                    {
                        "id": "done",
                        "status": "resolved",
                        "title": "Finished",
                        "body": "<p>Done.</p>",
                        "prompt": "historical prompt",
                        "outcome": "landed",
                        "written_by": "test",
                        "written_at": "2026-01-01",
                    },
                ],
            }
        )
        if modified is not None:
            state["modified"] = modified
    elif resource_type == "research":
        state.update({"source": "local", "source_quality": "verified"})
    elif resource_type == "evidence":
        state.update({"verdict": "pass", "evidence_for": ["typed-plan"]})
    else:
        raise ValueError(f"unsupported fixture resource type {resource_type!r}")
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        "<title>Readable</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs_dir / (relative or f"{slug}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def _live_pointer(
    plan: str,
    *,
    project: str = "proj",
    run_id: str = "run-live",
    member: str = "worker-one",
    section: str = "delivery",
    created_at: str = "2026-08-12T18:00:00Z",
) -> dict:
    return {
        "run_id": run_id,
        "project": project,
        "member": member,
        "created_at": created_at,
        "node": {"plan": plan, "section": section},
    }


def test_legacy_read_shape_is_exactly_unchanged(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")

    result = mcp_module._read_plan(project, "readable")

    assert set(result) == {"project", "slug", "version", "data"}
    assert result["project"] == project
    assert result["slug"] == "readable"
    assert result["version"] == 3
    assert "resource" not in result
    assert "view" not in result


def test_typed_plan_defaults_to_small_human_summary(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    raw = mcp_module._read_plan(project, "readable")

    result = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "readable"}
    )

    assert result["resource"] == {
        "project": project,
        "type": "plan",
        "id": "readable",
        "archived": False,
    }
    assert result["view"] == "summary"
    assert result["state"]["status"] == "active"
    assert result["state"]["effective_status"] == "active"
    assert result["state"]["progress"] == 0.4
    assert result["open_decisions"] == [
        {
            "key": "open-choice",
            "question": "Choose a route",
            "options": ["a", "b"],
        }
    ]
    assert result["next"]["id"] == "next"
    assert "prompt" not in result["next"]
    assert compact_size(result) <= 2048
    assert compact_size(result) <= compact_size(raw) * 0.60


def test_live_runs_are_grouped_by_target_plan_and_project():
    pointers = [
        _live_pointer("alpha", run_id="run-b"),
        _live_pointer("alpha", run_id="run-a", section="verification"),
        _live_pointer("beta", run_id="run-c"),
        _live_pointer("alpha", project="elsewhere", run_id="run-d"),
        {"run_id": "run-e", "project": "proj", "node": {}},
    ]

    grouped = in_flight_by_plan("proj", pointers)

    assert list(grouped) == ["alpha", "beta"]
    assert [run["run_id"] for run in grouped["alpha"]] == ["run-a", "run-b"]
    assert grouped["alpha"][0] == {
        "run_id": "run-a",
        "member": "worker-one",
        "section": "verification",
        "started_at": "2026-08-12T18:00:00Z",
    }


def test_typed_plan_summary_reports_matching_live_run(setup, monkeypatch):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    pointer = _live_pointer("readable")
    monkeypatch.setattr("reckon.crew.list_live", lambda: [pointer])

    result = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "readable"}
    )

    assert result["in_flight"] == [
        {
            "run_id": "run-live",
            "member": "worker-one",
            "section": "delivery",
            "started_at": "2026-08-12T18:00:00Z",
        }
    ]


def test_plan_data_views_keep_the_same_live_run(monkeypatch):
    pointer = _live_pointer("readable")
    monkeypatch.setattr("reckon.crew.list_live", lambda: [pointer])
    selector = ResourceSelector("proj", "plan", "readable")
    data = {
        "title": "Readable",
        "status": "active",
        "followups": [{"id": "continue", "status": "open"}],
    }

    results = [
        resource_view(selector, 3, data, view=view)
        for view in ("summary", "detail", "history", "raw")
    ]

    assert all(result["in_flight"] == results[0]["in_flight"] for result in results)


def test_unmatched_and_non_plan_resources_have_no_live_key(monkeypatch):
    monkeypatch.setattr(
        "reckon.crew.list_live", lambda: [_live_pointer("another-plan")]
    )
    unmatched = resource_view(
        ResourceSelector("proj", "plan", "readable"),
        3,
        {"title": "Readable", "status": "active"},
        view="summary",
    )
    sprint = resource_view(
        ResourceSelector("proj", "sprint", "current"),
        2,
        {"theme": "Current", "status": "active", "items": []},
        view="summary",
    )
    roadmap = build_roadmap(
        "proj",
        [
            {
                "slug": "readable",
                "type": "plan",
                "status": "active",
                "gates": [{"id": "evidence", "verdict": "passed"}],
                "followups": [{"id": "continue", "status": "open"}],
            }
        ],
        [],
    )

    assert "in_flight" not in unmatched
    assert "in_flight" not in sprint
    assert "in_flight" not in roadmap["pending_work"][0]


def test_archived_plan_does_not_claim_live_run_for_same_slug(monkeypatch):
    monkeypatch.setattr("reckon.crew.list_live", lambda: [_live_pointer("readable")])

    result = resource_view(
        ResourceSelector("proj", "plan", "readable", archived=True),
        3,
        {"title": "Archived", "status": "done"},
        view="summary",
    )

    assert "in_flight" not in result


def test_plan_read_and_roadmap_share_the_live_run_projection(monkeypatch):
    pointer = _live_pointer("readable")
    monkeypatch.setattr("reckon.crew.list_live", lambda: [pointer])
    selector = ResourceSelector("proj", "plan", "readable")
    data = {
        "slug": "readable",
        "title": "Readable",
        "type": "plan",
        "status": "active",
        "impl": 0.0,
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "continue", "status": "open"}],
    }

    plan_result = resource_view(selector, 3, data, view="summary")
    roadmap_result = build_roadmap("proj", [data], [])
    row = roadmap_result["pending_work"][0]

    assert row["in_flight"] == plan_result["in_flight"]


def test_plan_summary_surfaces_age_at_the_audit_staleness_boundary(setup):
    docs_dir, project = setup
    modified = date.today() - timedelta(days=ACTIVE_PLAN_STALE_AFTER_DAYS)
    _plan(docs_dir, "readable", modified=modified.isoformat())

    result = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "readable"}
    )

    assert result["state"]["age_days"] == ACTIVE_PLAN_STALE_AFTER_DAYS
    assert result["state"]["staleness"] == "current"


def test_stale_plan_read_remains_advisory_and_dispatchable(setup):
    docs_dir, project = setup
    modified = date.today() - timedelta(days=ACTIVE_PLAN_STALE_AFTER_DAYS + 1)
    _plan(docs_dir, "readable", modified=modified.isoformat())

    result = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "readable"}
    )

    assert result["state"]["age_days"] == ACTIVE_PLAN_STALE_AFTER_DAYS + 1
    assert result["state"]["staleness"] == "stale"
    assert result["state"]["effective_status"] == "active"
    assert result["blocking"] == []
    assert result["next"]["id"] == "next"


def test_dependency_blocking_is_derived_and_clears_when_dependency_ships(setup):
    docs_dir, project = setup
    dependency = _plan(docs_dir, "dependency")
    _plan(docs_dir, "dependent")

    from reckon._plan_html import read_state, write_state

    dependent = docs_dir / "dependent.html"
    dependent_state = read_state(dependent.read_text(encoding="utf-8"))
    dependent.write_text(
        write_state(
            dependent.read_text(encoding="utf-8"),
            {**dependent_state, "status": "active", "depends_on": ["dependency"]},
        ),
        encoding="utf-8",
    )

    blocked = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "dependent"}
    )
    assert blocked["state"]["status"] == "active"
    assert blocked["state"]["effective_status"] == "blocked"
    assert blocked["blocking"] == [
        {
            "ref": "dependency",
            "found": True,
            "status": "active",
        }
    ]

    dependency_state = read_state(dependency.read_text(encoding="utf-8"))
    dependency.write_text(
        write_state(
            dependency.read_text(encoding="utf-8"),
            {**dependency_state, "status": "shipped", "impl": 1.0},
        ),
        encoding="utf-8",
    )
    ready = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "dependent"}
    )
    assert ready["state"]["status"] == "active"
    assert ready["state"]["effective_status"] == "active"
    assert ready["blocking"] == []


def test_external_dependency_blocking_clears_without_local_file_changes(
    setup,
    tmp_path,
):
    docs_dir, project = setup
    other_docs = tmp_path / "other-docs"
    other_docs.mkdir()
    dependency = _plan(
        other_docs,
        "foundation",
        project="other",
    )
    _plan(docs_dir, "dependent")
    mounts_path = mcp_module._mounts_path()
    mounts_path.write_text(
        json.dumps(
            {
                project: str(docs_dir),
                "other": str(other_docs),
            }
        ),
        encoding="utf-8",
    )

    from reckon._plan_html import read_state, write_state

    dependent = docs_dir / "dependent.html"
    dependent_state = read_state(dependent.read_text(encoding="utf-8"))
    dependent.write_text(
        write_state(
            dependent.read_text(encoding="utf-8"),
            {
                **dependent_state,
                "status": "active",
                "depends_on": ["other:foundation"],
            },
        ),
        encoding="utf-8",
    )

    blocked = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "dependent"}
    )
    assert blocked["state"]["effective_status"] == "blocked"

    dependency_state = read_state(dependency.read_text(encoding="utf-8"))
    dependency.write_text(
        write_state(
            dependency.read_text(encoding="utf-8"),
            {**dependency_state, "status": "shipped", "impl": 1.0},
        ),
        encoding="utf-8",
    )
    ready = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "dependent"}
    )
    assert ready["state"]["effective_status"] == "active"
    assert ready["blocking"] == []


def test_legacy_sprint_rollup_uses_hydrated_effective_status(setup):
    docs_dir, project = setup
    _plan(docs_dir, "dependency")
    _plan(docs_dir, "dependent")

    from reckon._plan_html import read_state, write_state

    dependent = docs_dir / "dependent.html"
    state = read_state(dependent.read_text(encoding="utf-8"))
    dependent.write_text(
        write_state(
            dependent.read_text(encoding="utf-8"),
            {**state, "status": "pending", "depends_on": ["dependency"]},
        ),
        encoding="utf-8",
    )
    state_dir = docs_dir.parent / "state" / project
    state_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(
        json.dumps(
            {
                "project": project,
                "data": {
                    "_version": 1,
                    "sprints": [
                        {
                            "id": "iteration",
                            "status": "active",
                            "items": [{"slug": "dependent"}],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = mcp_module._read_plan(
        resource={"project": project, "type": "sprint", "id": "iteration"},
        view="detail",
    )
    assert result["state"]["blocked"] == 1
    assert result["items"][0]["status"] == "pending"
    assert result["items"][0]["effective_status"] == "blocked"


def test_explicit_sprint_blocker_keeps_plan_effectively_blocked(setup):
    docs_dir, project = setup
    _plan(docs_dir, "waiting")
    state_dir = docs_dir.parent / "state" / project
    state_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(
        json.dumps(
            {
                "project": project,
                "data": {
                    "_version": 1,
                    "sprints": [
                        {
                            "id": "iteration",
                            "status": "active",
                            "items": [
                                {
                                    "slug": "waiting",
                                    "blocked_by": ["external-access"],
                                }
                            ],
                        }
                    ],
                    "blockers": [
                        {
                            "id": "external-access",
                            "summary": "Await access.",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "waiting"}
    )

    assert result["state"]["status"] == "active"
    assert result["state"]["effective_status"] == "blocked"
    assert result["blocking"] == ["external-access"]


def test_detail_prompts_are_explicitly_opt_in(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    selector = {"project": project, "type": "plan", "id": "readable"}

    without = mcp_module._read_plan(resource=selector, view="detail")
    with_prompts = mcp_module._read_plan(
        resource=selector,
        view="detail",
        include_prompts=True,
    )
    rejected = mcp_module._read_plan(
        resource=selector,
        view="summary",
        include_prompts=True,
    )

    assert "prompt" not in without["followups"][0]
    assert with_prompts["followups"][0]["prompt"].startswith("Project:")
    assert rejected["error"] == "invalid_view_option"


def test_history_is_paginated_and_omits_prompts(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    selector = {"project": project, "type": "plan", "id": "readable"}

    first = mcp_module._read_plan(
        resource=selector,
        view="history",
        limit=1,
    )
    second = mcp_module._read_plan(
        resource=selector,
        view="history",
        cursor=first["pagination"]["next_cursor"],
        limit=1,
    )
    invalid = mcp_module._read_plan(
        resource=selector,
        view="history",
        cursor="not-a-cursor",
    )

    assert first["pagination"]["total"] == 2
    assert len(first["records"]) == len(second["records"]) == 1
    assert all("prompt" not in item for item in first["records"] + second["records"])
    assert invalid["error"] == "invalid_cursor"


def test_raw_and_schema_are_explicit_layers(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    selector = {"project": project, "type": "plan", "id": "readable"}

    raw = mcp_module._read_plan(resource=selector, view="raw")
    schema = mcp_module._read_plan(resource=selector, view="schema")

    assert raw["data"]["followups"][0]["prompt"].startswith("Project:")
    assert schema["schema_version"] == 1
    assert schema["response_schema"]["type"] == "object"
    assert schema["storage_schema"]["title"] == "reckon PlanState"
    response_schemas = schema["response_schemas"]
    assert "data" in response_schemas["raw"]["required"]
    assert response_schemas["summary"]["properties"]["in_flight"]["type"] == "array"
    assert {"records", "pagination"} <= set(response_schemas["history"]["required"])
    assert {"metadata", "relations", "followups", "questions"} <= set(
        response_schemas["detail"]["required"]
    )
    assert {
        "schema_version",
        "response_schema",
        "response_schemas",
        "storage_schema",
        "op_vocab",
        "dos_donts",
    } <= set(response_schemas["schema"]["required"])
    assert compact_size(schema) <= 24 * 1024


def test_storage_schema_matches_selected_resource_type():
    sprint = storage_schema_for("sprint")
    project = storage_schema_for("project")
    timeline = storage_schema_for("timeline")

    assert sprint["title"] == "reckon SprintResource"
    assert sprint["properties"]["type"]["const"] == "sprint"
    assert {"id", "type", "version"} <= set(sprint["required"])
    assert project["title"] == "reckon ProjectResource"
    assert project["properties"]["type"]["const"] == "project"
    assert project["properties"]["scope"]["properties"]["routes"]["type"] == "array"
    assert timeline["properties"]["events"]["items"]["title"] == "TimelineEntry"


def test_schema_view_routes_to_every_selected_storage_contract(setup):
    from reckon.project_state import migrate_project_state

    docs_dir, project = setup
    _plan(docs_dir, "typed-plan", relative="plans/typed-plan.html")
    _plan(
        docs_dir,
        "typed-research",
        relative="research/typed-research.html",
        resource_type="research",
    )
    _plan(
        docs_dir,
        "typed-evidence",
        relative="evidence/typed-evidence.html",
        resource_type="evidence",
    )
    index = docs_dir / "state" / project / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps(
            {
                "updated": "2026-01-01T00:00:00",
                "project": project,
                "doc": "index",
                "data": {
                    "_version": 1,
                    "active_sprint_id": "iteration",
                    "projects": [
                        {
                            "project": project,
                            "path": "/private/checkout/docs",
                            "owner": "test",
                            "published": "example.invalid/proj",
                            "plans_count": 99,
                        }
                    ],
                    "sprints": [
                        {
                            "id": "iteration",
                            "theme": "Iteration",
                            "status": "active",
                            "items": [
                                {
                                    "slug": "typed-plan",
                                    "status": "active",
                                    "impl": 0.4,
                                }
                            ],
                        }
                    ],
                    "milestones": [
                        {"id": "target", "name": "Target", "status": "active"}
                    ],
                    "blockers": [
                        {
                            "id": "constraint",
                            "summary": "Constraint",
                            "owner": "test",
                            "next": "Resolve it",
                            "n": 0,
                        }
                    ],
                    "timeline": [
                        {"when": "2026-01-01", "who": "test", "what": "Started"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    legacy_sprint = mcp_module._read_plan(
        resource={"project": project, "type": "sprint", "id": "iteration"},
        view="raw",
        checkout_path=str(docs_dir.parent),
    )
    legacy_project = mcp_module._read_plan(
        resource={"project": project, "type": "project", "id": "project"},
        view="raw",
        checkout_path=str(docs_dir.parent),
    )
    assert legacy_sprint["data"]["theme"] == "Iteration"
    assert legacy_sprint["version"] == 1
    assert legacy_project["data"]["project"] == project
    assert (
        "legacy aggregate index" in legacy_sprint["data"]["compatibility_warnings"][0]
    )
    migrate_project_state(docs_dir, project)
    selectors = {
        "plan": "typed-plan",
        "research": "typed-research",
        "evidence": "typed-evidence",
        "sprint": "iteration",
        "milestone": "target",
        "blocker": "constraint",
        "timeline": "timeline",
        "project": "project",
    }

    schemas = {
        resource_type: mcp_module._read_plan(
            resource={
                "project": project,
                "type": resource_type,
                "id": resource_id,
            },
            view="schema",
            checkout_path=str(docs_dir.parent),
        )["storage_schema"]
        for resource_type, resource_id in selectors.items()
    }

    assert schemas["plan"]["title"] == "reckon PlanState"
    assert schemas["research"]["title"] == "reckon PlanState"
    assert schemas["evidence"]["title"] == "reckon PlanState"
    assert schemas["sprint"]["title"] == "reckon SprintResource"
    assert schemas["milestone"]["title"] == "reckon MilestoneResource"
    assert schemas["blocker"]["title"] == "reckon BlockerResource"
    assert schemas["timeline"]["title"] == "reckon TimelineResource"
    assert schemas["project"]["title"] == "reckon ProjectResource"


def test_archived_selector_does_not_read_live_duplicate(setup):
    docs_dir, project = setup
    _plan(docs_dir, "shared", relative="plans/shared.html", summary="live")
    _plan(
        docs_dir,
        "shared",
        relative="plans/archive/shared.html",
        summary="archived",
    )

    result = mcp_module._read_plan(
        resource={
            "project": project,
            "type": "plan",
            "id": "shared",
            "archived": True,
        }
    )

    assert result["summary"] == "archived"
    assert result["resource"]["archived"] is True


def test_selector_errors_are_structured_and_bounded(setup):
    _, project = setup

    invalid = mcp_module._read_plan(
        resource={"project": project, "type": "unknown", "id": "x"}
    )
    missing = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "missing"}
    )

    assert invalid["error"] == "invalid_resource"
    assert missing["error"] == "not_found"
    assert missing["resource"]["id"] == "missing"
    assert compact_size(invalid) <= 2048
    assert compact_size(missing) <= 2048


def test_malformed_selector_and_cursor_errors_stay_bounded(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")

    unsafe = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "x" * 100_000}
    )
    cursor = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": "readable"},
        view="history",
        cursor="x" * 100_000,
    )
    view = mcp_module._read_plan(project, view="x" * 100_000)

    assert unsafe["error"] == "invalid_resource"
    assert cursor["error"] == "invalid_cursor"
    assert view["error"] == "invalid_view"
    assert compact_size(unsafe) <= 2048
    assert compact_size(cursor) <= 2048
    assert compact_size(view) <= 2048


def test_unshipped_dependencies_are_visible_as_blocking():
    selector = ResourceSelector("proj", "plan", "dependent")
    deps = [
        {"ref": "ready", "found": True, "status": "shipped"},
        {"ref": "pending", "found": True, "status": "active"},
        {"ref": "missing", "found": False},
    ]

    result = resource_view(
        selector,
        2,
        {"title": "Dependent", "status": "active"},
        view="summary",
        deps=deps,
    )

    assert result["blocking"] == [
        {"ref": "pending", "found": True, "status": "active"},
        {"ref": "missing", "found": False, "status": ""},
    ]


def test_sprint_summary_uses_live_composed_items():
    selector = ResourceSelector("proj", "sprint", "S7")
    items = [
        {
            "slug": f"item-{index}",
            "title": f"Item {index}",
            "status": "shipped" if index < 6 else "pending",
            "impl": 1.0 if index < 6 else 0.0,
            "capability": {"class": "general"},
            "why_now": "x" * 100,
            "done_when": "y" * 100,
        }
        for index in range(12)
    ]
    raw = {
        "id": "S7",
        "theme": "Representative sprint",
        "summary": "A populated sprint fixture.",
        "status": "active",
        "items": items,
    }

    result = resource_view(selector, 4, raw, view="summary")

    assert result["state"]["items"] == 12
    assert result["state"]["completed"] == 6
    assert compact_size(result) <= 6 * 1024
    assert compact_size(result) <= compact_size(raw) / 2


def test_discovery_summary_is_paginated_and_small():
    raw = {
        "project": "proj",
        "plans": [
            {
                "slug": f"plan-{index}",
                "type": "plan",
                "title": f"Plan {index}",
                "status": "active",
                "impl": 0.2,
                "summary": "z" * 160,
                "depends_on": ["dependency"],
                "capability": {"class": "orchestrator"},
            }
            for index in range(40)
        ],
        "followups": [],
        "questions": [],
        "sprints": [],
        "milestones": [],
        "blockers": [],
        "timeline": [],
        "active_sprint_id": None,
        "resource_versions": {"project:project": 2},
        "summary": {
            "plans": 40,
            "artifacts": 40,
            "open_followups": 0,
            "open_questions": 0,
            "open_decisions": 0,
        },
    }

    result = discovery_view(
        "proj",
        raw,
        view="summary",
        cursor=None,
        limit=None,
        include_prompts=False,
    )

    assert result["pagination"]["count"] == 25
    assert result["pagination"]["next_cursor"]
    assert compact_size(result) <= 12 * 1024
    assert compact_size(result) <= compact_size(raw) * 0.35


def test_discovery_summary_includes_concise_sprint_cards():
    raw = {
        "project": "proj",
        "plans": [],
        "followups": [],
        "questions": [],
        "sprints": [
            {
                "id": "iteration",
                "theme": "Readable responses",
                "status": "active",
                "items": [
                    {"slug": "done", "status": "shipped"},
                    {"slug": "waiting", "status": "blocked"},
                ],
            }
        ],
        "milestones": [],
        "blockers": [],
        "timeline": [],
        "active_sprint_id": "iteration",
        "resource_versions": {},
        "summary": {"plans": 0, "artifacts": 0},
    }

    result = discovery_view(
        "proj",
        raw,
        view="summary",
        cursor=None,
        limit=None,
        include_prompts=False,
    )

    assert result["resources"] == [
        {
            "id": "iteration",
            "type": "sprint",
            "title": "Readable responses",
            "status": "active",
            "items": 2,
            "completed": 1,
            "blocked": 1,
        }
    ]
    assert result["pagination"] == {
        "count": 1,
        "total": 1,
        "next_cursor": None,
    }


def test_audit_progressive_views_are_small_and_legacy_is_untouched(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    legacy = mcp_module._audit(project)

    summary = mcp_module._audit(project, view="summary")
    detail = mcp_module._audit(project, view="detail", limit=1)
    raw = mcp_module._audit(project, view="raw")
    schema = mcp_module._audit(project, view="schema")

    assert "resource" not in legacy
    assert summary["view"] == "summary"
    assert summary["state"]["checked"] == 1
    assert compact_size(summary) <= 4 * 1024
    assert detail["pagination"]["count"] <= 1
    assert compact_size(detail) <= 16 * 1024
    assert raw["data"] == legacy
    assert {"findings", "pagination", "violations"} <= set(
        schema["response_schemas"]["detail"]["required"]
    )
    assert "history" not in schema["response_schemas"]

    representative = {
        "project": project,
        "checked": 80,
        "conformant": 0,
        "violations": [],
        "findings": [
            {
                "category": "references",
                "code": "dangling-ref",
                "severity": "warn",
                "message": f"Resource {index} has a detailed finding " + "x" * 180,
                "slug": f"resource-{index}",
                "path": f"plans/resource-{index}.html",
            }
            for index in range(80)
        ],
        "finding_counts": {
            "total": 80,
            "by_severity": {"warn": 80},
            "by_category": {"references": 80},
            "by_code": {"dangling-ref": 80},
        },
    }
    representative_summary = audit_view(
        project, representative, view="summary", cursor=None, limit=None
    )
    assert compact_size(representative_summary) <= compact_size(representative) * 0.2


def test_fastmcp_signature_keeps_new_arguments_optional():
    signature = inspect.signature(mcp_module._read_plan)

    assert signature.parameters["resource"].default is None
    assert signature.parameters["view"].default is None
    assert signature.parameters["cursor"].default is None
    assert signature.parameters["include_prompts"].default is False
    tool = next(
        item
        for item in mcp_module.mcp._tool_manager.list_tools()
        if item.name == "_read_plan"
    )
    required = set(tool.parameters.get("required") or [])
    properties = tool.parameters["properties"]
    assert {"resource", "view", "cursor", "include_prompts"} <= set(properties)
    assert not {"resource", "view", "cursor", "include_prompts"} & required


def test_fastmcp_edit_signature_discriminates_state_and_text_payloads():
    tool = next(
        item
        for item in mcp_module.mcp._tool_manager.list_tools()
        if item.name == "_edit_plan"
    )
    required = set(tool.parameters.get("required") or [])
    properties = tool.parameters["properties"]

    assert {"project", "slug", "expected_version"} <= required
    assert not {"mode", "ops", "old_html", "new_html"} & required
    assert properties["mode"]["enum"] == ["state", "text"]


def test_error_builder_common_contract():
    selector = ResourceSelector("proj", "plan", "x")
    result = error_response(
        "version_conflict",
        "Version changed.",
        selector=selector,
        operation="edit",
        expected_version=2,
        current_version=3,
        hint="Re-read and retry.",
    )

    assert result == {
        "ok": False,
        "error": "version_conflict",
        "message": "Version changed.",
        "operation": "edit",
        "resource": selector.as_dict(),
        "expected_version": 2,
        "current_version": 3,
        "hint": "Re-read and retry.",
    }


def test_edit_success_and_conflict_name_the_affected_resource(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")

    success = mcp_module._edit_plan(
        project,
        "readable",
        [{"op": "set", "path": "summary", "value": "Updated summary."}],
        expected_version=3,
    )
    conflict = mcp_module._edit_plan(
        project,
        "readable",
        [{"op": "set", "path": "summary", "value": "Stale edit."}],
        expected_version=3,
    )

    assert success["ok"] is True
    assert success["operation"] == "edit"
    assert success["resource"] == {
        "project": project,
        "type": "plan",
        "id": "readable",
        "archived": False,
        "title": "Readable Plan",
    }
    assert success["message"] == "Updated plan Readable Plan to version 4."
    assert conflict["error"] == "version_conflict"
    assert conflict["operation"] == "edit"
    assert conflict["resource"] == success["resource"]
    assert conflict["expected_version"] == 3
    assert conflict["current_version"] == 4
    assert conflict["hint"].startswith("Re-read")
