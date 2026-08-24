from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from reckon.mcp_views import compact_size, roadmap_view
from reckon.roadmap import build_roadmap


@pytest.fixture()
def mounted_project(tmp_path, monkeypatch):
    project = "sample"
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
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    return project, docs


def _write_plan(
    docs: Path,
    slug: str,
    *,
    status: str = "active",
    depends_on: list[str] | None = None,
    decisions: dict[str, dict] | None = None,
    body: str = "",
) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            bare,
            {
                "slug": slug,
                "title": slug.title(),
                "status": status,
                "depends_on": depends_on or [],
                "decisions": decisions or {},
                "gates": [
                    {
                        "id": "verified-input",
                        "measure": "Required evidence is present",
                        "verdict": "passed",
                    }
                ],
                "followups": [
                    {
                        "id": "next-action",
                        "prompt": f"/reckon-ship {slug}",
                    }
                ],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    return path


def test_mcp_roadmap_scans_mounted_project(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "foundation")
    _write_plan(docs, "consumer", depends_on=["foundation"])

    result = mcp_module._roadmap(project)

    assert result["project"] == project
    assert [item["slug"] for item in result["ready_now"]] == ["foundation"]
    foundation = next(
        item for item in result["pending_work"] if item["slug"] == "foundation"
    )
    assert foundation["dispatchable"] is True
    assert "open_followup" not in foundation["missing_dispatchability"]
    assert result["critical_path"]["plans"] == ["foundation", "consumer"]


def test_mcp_roadmap_preserves_named_decision_readiness(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(
        docs,
        "deciding",
        decisions={
            "transport": {
                "title": "Which transport should carry the payload?",
                "choice": "",
                "rationale": "",
            },
            "retention": {
                "title": "How long should records remain?",
                "choice": "",
                "rationale": "Deferred until retention policy is published.",
            },
        },
    )
    _write_plan(docs, "endpoint", depends_on=["deciding"])

    blocked = mcp_module._roadmap(project)
    rows = {row["slug"]: row for row in blocked["pending_work"]}

    assert [row["slug"] for row in blocked["pending_work"]] == [
        "deciding",
        "endpoint",
    ]
    assert rows["deciding"]["decision_blockers"] == [
        {
            "kind": "decision",
            "plan": "deciding",
            "id": "transport",
            "question": "Which transport should carry the payload?",
            "status": "open",
            "choice": "",
            "rationale": "",
        }
    ]
    assert rows["deciding"]["depends_on"] == []
    assert rows["deciding"]["explicit_blockers"] == []
    assert rows["deciding"]["gate_blockers"] == []
    assert rows["deciding"]["readiness"] == "blocked"
    assert rows["deciding"]["deferred_decisions"] == [
        {
            "kind": "decision",
            "plan": "deciding",
            "id": "retention",
            "question": "How long should records remain?",
            "status": "deferred",
            "choice": "",
            "rationale": "Deferred until retention policy is published.",
        }
    ]
    assert rows["endpoint"]["depends_on"][0]["ref"] == "deciding"
    assert rows["deciding"]["decision_blockers"][0]["kind"] == "decision"
    assert blocked["decision_readiness"] == {
        "ready": False,
        "open": 1,
        "deferred": 1,
    }

    lock = mcp_module._edit_plan_tool(
        project,
        "deciding",
        expected_version=0,
        mode="state",
        ops=[
            {
                "op": "lock",
                "key": "transport",
                "choice": "socket",
                "rationale": "Selected for bounded delivery.",
                "by": "reviewer",
            }
        ],
    )
    assert lock["ok"] is True

    released = mcp_module._roadmap(project)
    released_rows = {row["slug"]: row for row in released["pending_work"]}

    assert released_rows["deciding"]["readiness"] == "ready"
    assert released_rows["deciding"]["decision_blockers"] == []
    assert released_rows["deciding"]["decisions"][0]["status"] == "locked"
    assert released_rows["deciding"]["deferred_decisions"][0]["id"] == "retention"
    assert released["decision_readiness"] == {
        "ready": True,
        "open": 0,
        "deferred": 1,
    }


def test_fastmcp_registers_one_edit_tool_and_roadmap() -> None:
    names = {item.name for item in mcp_module.mcp._tool_manager.list_tools()}

    assert {"_roadmap", "_edit_plan"} <= names
    assert "_edit_plan_text" not in names


def test_mcp_roadmap_portfolio_rolls_up_mounted_projects(mounted_project) -> None:
    _project, docs = mounted_project
    _write_plan(docs, "work")

    result = mcp_module._roadmap("*")

    assert result["view"] == "summary"
    assert result["portfolio"]["projects"] == 1
    assert result["portfolio"]["plans"] == 1
    assert result["portfolio"]["ready"] == 1
    assert result["portfolio"]["blocked"] == 0
    assert result["portfolio"]["deferred"] == 0
    assert result["projects"][0]["completion"]["plans"] == 1
    assert result["projects"][0]["ready"] == 1
    assert result["projects"][0]["blocked"] == 0
    assert result["projects"][0]["finding_counts"]["total"] >= 0


def test_portfolio_summary_is_bounded_against_measured_baseline(
    mounted_project,
) -> None:
    _project, docs = mounted_project
    for index in range(40):
        _write_plan(docs, f"work-{index}", depends_on=[f"missing-{index}"])

    result = mcp_module._roadmap("*")

    assert compact_size(result) < 32 * 1024
    assert compact_size(result) < 176 * 1024


def test_single_project_summary_reports_counts(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "ready")
    _write_plan(docs, "blocked", depends_on=["missing"])

    result = mcp_module._roadmap(project, view="summary")

    assert result["view"] == "summary"
    assert result["completion"]["plans"] == 2
    assert result["ready"] == 1
    assert result["blocked"] == 1
    assert result["finding_counts"]["total"] >= 1


def test_summary_keeps_dependency_and_schedule_readiness_separate() -> None:
    inventory = [
        {
            "slug": slug,
            "title": slug.title(),
            "type": "plan",
            "status": "active",
            "sprint": sprint,
            "blocking": [],
            "gates": [{"id": "evidence", "verdict": "passed"}],
            "followups": [{"id": "next", "status": "open"}],
        }
        for slug, sprint in (("current", "first"), ("queued", "second"))
    ]
    raw = build_roadmap(
        "sample",
        inventory,
        [
            {"id": "first", "status": "active", "items": ["current"]},
            {"id": "second", "status": "planned", "items": ["queued"]},
        ],
        project_manifest={"schedule_horizon_sprints": 1},
    )

    summary = roadmap_view(raw, view="summary", cursor=None, limit=None)

    assert summary["dependency_readiness"] == {
        "ready": 2,
        "blocked": 0,
        "deferred": 0,
    }
    assert summary["schedule_readiness"] == {
        "configured": True,
        "configuration_key": "schedule_horizon_sprints",
        "window_sprints": 1,
        "horizon_depth": 2,
        "open_sprints": ["first", "second"],
        "earliest_open_sprint": "first",
        "ready_sprints": ["first"],
        "ready": 1,
        "deferred": 1,
    }


def test_single_project_detail_paginates_findings(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "first", depends_on=["missing-first"])
    _write_plan(docs, "second", depends_on=["missing-second"])

    first = mcp_module._roadmap(project, view="detail", limit=1)
    second = mcp_module._roadmap(
        project,
        view="detail",
        limit=1,
        cursor=first["pagination"]["next_cursor"],
    )

    assert first["view"] == "detail"
    assert first["pagination"]["count"] == 1
    assert first["pagination"]["total"] >= 2
    assert first["pagination"]["next_cursor"]
    assert second["wiring_findings"] != first["wiring_findings"]


def test_single_project_raw_preserves_lossless_report(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "work")

    legacy = mcp_module._roadmap(project)
    raw = mcp_module._roadmap(project, view="raw")

    assert raw == {"project": project, "view": "raw", "data": legacy}


def test_roadmap_rejects_unsupported_progressive_view(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "work")

    result = mcp_module._roadmap(project, view="history")

    assert result["ok"] is False
    assert result["error"] == "invalid_view"


def test_edit_plan_in_text_mode_replaces_exact_prose_and_advances_version(
    mounted_project,
) -> None:
    project, docs = mounted_project
    path = _write_plan(docs, "editable", body='<p id="intro">Old prose.</p>')

    result = mcp_module._edit_plan_tool(
        project,
        "editable",
        expected_version=0,
        doc_type="plan",
        mode="text",
        old_html='<p id="intro">Old prose.</p>',
        new_html='<p id="intro">Revised prose with <strong>evidence</strong>.</p>',
    )

    assert result["ok"] is True
    assert result["new_version"] == 1
    assert result["path"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert "Revised prose with <strong>evidence</strong>." in text
    assert 'name="plan-version" content="1"' in text


def test_edit_plan_in_text_mode_rejects_structured_state_changes(
    mounted_project,
) -> None:
    project, docs = mounted_project
    _write_plan(docs, "editable")

    result = mcp_module._edit_plan_tool(
        project,
        "editable",
        expected_version=0,
        doc_type="plan",
        mode="text",
        old_html='name="plan-status" content="active"',
        new_html='name="plan-status" content="shipped"',
    )

    assert result["ok"] is False
    assert result["error"] == "text_edit_error"
    assert "structured plan state" in result["detail"]


def test_edit_plan_in_text_mode_requires_one_exact_match(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "editable", body="<p>same</p><p>same</p>")

    result = mcp_module._edit_plan_tool(
        project,
        "editable",
        expected_version=0,
        doc_type="plan",
        mode="text",
        old_html="<p>same</p>",
        new_html="<p>changed</p>",
    )

    assert result["ok"] is False
    assert "found 2 occurrences" in result["detail"]


def test_edit_plan_modes_reject_mixed_payloads(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "editable", body="<p>prose</p>")

    text_with_ops = mcp_module._edit_plan_tool(
        project,
        "editable",
        expected_version=0,
        mode="text",
        ops=[{"op": "set", "path": "summary", "value": "mixed"}],
        old_html="<p>prose</p>",
        new_html="<p>changed</p>",
    )
    state_with_text = mcp_module._edit_plan_tool(
        project,
        "editable",
        expected_version=0,
        mode="state",
        ops=[],
        old_html="<p>prose</p>",
        new_html="<p>changed</p>",
    )

    assert text_with_ops["error"] == "invalid_edit_request"
    assert state_with_text["error"] == "invalid_edit_request"
