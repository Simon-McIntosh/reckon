"""Progressive MCP response views and legacy compatibility."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon.mcp_views import (
    ResourceSelector,
    audit_view,
    compact_size,
    discovery_view,
    error_response,
    resource_view,
)


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
) -> Path:
    from reckon._plan_html import write_state

    state = {
        "slug": slug,
        "title": "Readable Plan",
        "summary": summary,
        "status": "active",
        "impl": 0.4,
        "version": 3,
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
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="proj"><title>Readable</title></head>'
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs_dir / (relative or f"{slug}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


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
    assert compact_size(result) <= compact_size(raw) / 2


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
    assert compact_size(schema) <= 24 * 1024


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

    assert unsafe["error"] == "invalid_resource"
    assert cursor["error"] == "invalid_cursor"
    assert compact_size(unsafe) <= 2048
    assert compact_size(cursor) <= 2048


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


def test_audit_progressive_views_are_small_and_legacy_is_untouched(setup):
    docs_dir, project = setup
    _plan(docs_dir, "readable")
    legacy = mcp_module._audit(project)

    summary = mcp_module._audit(project, view="summary")
    detail = mcp_module._audit(project, view="detail", limit=1)
    raw = mcp_module._audit(project, view="raw")

    assert "resource" not in legacy
    assert summary["view"] == "summary"
    assert summary["state"]["checked"] == 1
    assert compact_size(summary) <= 4 * 1024
    assert detail["pagination"]["count"] <= 1
    assert compact_size(detail) <= 16 * 1024
    assert raw["data"] == legacy

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
