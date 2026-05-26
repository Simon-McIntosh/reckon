"""Tests for the new and refactored reckon MCP tools.

Covers:
  - _list_sprints, _update_sprint, _add_sprint_item, _move_sprint_item
  - _update_inventory_item
  - _list_followups, _list_questions, _list_projects
  - _resolve_question, _add_research
  - note-ID uniqueness fix in _append_comment
  - _list_plans discovery fallback
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path))
    return tmp_path


import reckon._store as _store_module
import reckon.mcp as mcp_module


def get_store(state_root):  # noqa: ARG001
    importlib.reload(_store_module)
    return _store_module


def get_mcp(state_root):  # noqa: ARG001
    importlib.reload(_store_module)
    importlib.reload(mcp_module)
    return mcp_module


# ── helpers ────────────────────────────────────────────────────────────────

def _seed_index(state_root: Path, project: str, data: dict) -> None:
    """Write a bare index.json envelope to the state root."""
    proj_dir = state_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": "index",
        "data": {"_version": 0, **data},
    }
    (proj_dir / "index.json").write_text(json.dumps(envelope, indent=2))


def _seed_plan(state_root: Path, project: str, slug: str, data: dict) -> None:
    proj_dir = state_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": slug,
        "data": {"_version": 0, **data},
    }
    (proj_dir / f"{slug}.json").write_text(json.dumps(envelope, indent=2))


# ── _list_sprints ──────────────────────────────────────────────────────────

def test_list_sprints_returns_sprints(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {
        "active_sprint_id": "S1",
        "sprints": [{"id": "S1", "theme": "alpha", "status": "active", "items": []}],
        "milestones": [{"id": "M1", "name": "launch"}],
    })
    result = mcp._list_sprints("proj")
    assert result["active_sprint_id"] == "S1"
    assert len(result["sprints"]) == 1
    assert result["sprints"][0]["theme"] == "alpha"
    assert result["milestones"][0]["id"] == "M1"


def test_list_sprints_empty_project(state_root):
    mcp = get_mcp(state_root)
    result = mcp._list_sprints("noproject")
    assert result["sprints"] == []
    assert result["milestones"] == []


# ── _update_sprint ─────────────────────────────────────────────────────────

def test_update_sprint_status(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {
        "active_sprint_id": None,
        "sprints": [{"id": "S1", "status": "planned", "items": []}],
    })
    result = mcp._update_sprint("proj", "S1", {"status": "active"}, expected_version=0)
    assert result["ok"] is True
    # Verify active_sprint_id was updated
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    assert data["active_sprint_id"] == "S1"
    assert data["sprints"][0]["status"] == "active"


def test_update_sprint_done_clears_active_id(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {
        "active_sprint_id": "S1",
        "sprints": [{"id": "S1", "status": "active", "items": []}],
    })
    result = mcp._update_sprint("proj", "S1", {"status": "done"}, expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    assert data["active_sprint_id"] is None


def test_update_sprint_warns_on_double_active(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {
        "active_sprint_id": "S1",
        "sprints": [
            {"id": "S1", "status": "active", "items": []},
            {"id": "S2", "status": "planned", "items": []},
        ],
    })
    result = mcp._update_sprint("proj", "S2", {"status": "active"}, expected_version=0)
    assert result["ok"] is True
    assert "warning" in result


def test_update_sprint_rejects_items_key(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [{"id": "S1", "items": []}]})
    result = mcp._update_sprint("proj", "S1", {"items": []}, expected_version=0)
    assert result["ok"] is False
    assert "items" in result["error"]


def test_update_sprint_not_found(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": []})
    result = mcp._update_sprint("proj", "S99", {"status": "done"}, expected_version=0)
    assert result["ok"] is False


def test_update_sprint_version_conflict(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [{"id": "S1", "status": "planned", "items": []}]})
    result = mcp._update_sprint("proj", "S1", {"status": "active"}, expected_version=99)
    assert result["ok"] is False
    assert result["error"] == "version_conflict"


# ── _add_sprint_item ───────────────────────────────────────────────────────

def test_add_sprint_item_string(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [{"id": "S1", "items": []}]})
    result = mcp._add_sprint_item("proj", "S1", "my-plan", expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    assert "my-plan" in data["sprints"][0]["items"]


def test_add_sprint_item_object(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [{"id": "S1", "items": []}]})
    item = {"slug": "plan-x", "why_now": "gates M1", "tier": "sonnet", "done_when": "tests green"}
    result = mcp._add_sprint_item("proj", "S1", item, expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    assert data["sprints"][0]["items"][0]["why_now"] == "gates M1"


def test_add_sprint_item_duplicate_rejected(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [{"id": "S1", "items": ["plan-x"]}]})
    result = mcp._add_sprint_item("proj", "S1", "plan-x", expected_version=0)
    assert result["ok"] is False
    assert "already in sprint" in result["error"]


def test_add_sprint_item_sprint_not_found(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": []})
    result = mcp._add_sprint_item("proj", "S99", "plan-x", expected_version=0)
    assert result["ok"] is False


# ── _move_sprint_item ──────────────────────────────────────────────────────

def test_move_sprint_item_basic(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [
        {"id": "S1", "items": ["plan-a", "plan-b"]},
        {"id": "S2", "items": []},
    ]})
    result = mcp._move_sprint_item("proj", "plan-a", "S1", "S2", expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    s1 = next(s for s in data["sprints"] if s["id"] == "S1")
    s2 = next(s for s in data["sprints"] if s["id"] == "S2")
    assert "plan-a" not in s1["items"]
    assert "plan-a" in s2["items"]
    assert "plan-b" in s1["items"]


def test_move_sprint_item_preserves_object_fields(state_root):
    mcp = get_mcp(state_root)
    item = {"slug": "plan-a", "why_now": "priority", "tier": "opus"}
    _seed_index(state_root, "proj", {"sprints": [
        {"id": "S1", "items": [item]},
        {"id": "S2", "items": []},
    ]})
    mcp._move_sprint_item("proj", "plan-a", "S1", "S2", expected_version=0)
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    moved = data["sprints"][1]["items"][0]
    assert moved["why_now"] == "priority"


def test_move_sprint_item_not_found(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"sprints": [
        {"id": "S1", "items": []},
        {"id": "S2", "items": []},
    ]})
    result = mcp._move_sprint_item("proj", "plan-x", "S1", "S2", expected_version=0)
    assert result["ok"] is False


# ── _update_inventory_item ─────────────────────────────────────────────────

def test_update_inventory_item(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"inventory": [
        {"slug": "plan-x", "status": "active", "impl": 0.0},
    ]})
    result = mcp._update_inventory_item("proj", "plan-x", {"impl": 0.75, "status": "shipped"}, expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "index")
    item = data["inventory"][0]
    assert item["impl"] == 0.75
    assert item["status"] == "shipped"


def test_update_inventory_item_not_found(state_root):
    mcp = get_mcp(state_root)
    _seed_index(state_root, "proj", {"inventory": []})
    result = mcp._update_inventory_item("proj", "missing", {"status": "shipped"}, expected_version=0)
    assert result["ok"] is False
    assert "not found in inventory" in result["error"]


# ── _list_followups ────────────────────────────────────────────────────────

def test_list_followups_across_plans(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {
        "followups": [
            {"id": "f1", "title": "next step", "body": "x", "written_by": "smc", "written_at": "2026-01-01", "prompt": "p"},
            {"id": "f2", "title": "done", "body": "y", "written_by": "smc", "written_at": "2026-01-01", "prompt": "p",
             "resolved_at": "2026-01-02", "outcome": "shipped"},
        ]
    })
    _seed_plan(state_root, "proj", "plan-b", {
        "followups": [
            {"id": "f3", "title": "another", "body": "z", "written_by": "smc", "written_at": "2026-01-01", "prompt": "p"},
        ]
    })
    result = mcp._list_followups("proj", unresolved_only=True)
    assert result["count"] == 2
    slugs = {f["plan_slug"] for f in result["followups"]}
    assert "plan-a" in slugs
    assert "plan-b" in slugs
    ids = {f["id"] for f in result["followups"]}
    assert "f1" in ids
    assert "f3" in ids
    assert "f2" not in ids  # resolved


def test_list_followups_include_resolved(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {
        "followups": [
            {"id": "f1", "title": "a", "body": "b", "written_by": "x", "written_at": "2026-01-01", "prompt": "p",
             "resolved_at": "2026-01-02"},
        ]
    })
    result = mcp._list_followups("proj", unresolved_only=False)
    assert result["count"] == 1


# ── _list_questions ────────────────────────────────────────────────────────

def test_list_questions_across_plans(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {
        "questions": [
            {"id": "q1", "section": "§2", "body": "Which approach?", "opened_by": "smc", "opened_at": "2026-01-01"},
            {"id": "q2", "section": "§3", "body": "Done?", "opened_by": "smc", "opened_at": "2026-01-01",
             "resolved_at": "2026-01-02"},
        ]
    })
    result = mcp._list_questions("proj", unresolved_only=True)
    assert result["count"] == 1
    assert result["questions"][0]["id"] == "q1"


# ── _list_projects ─────────────────────────────────────────────────────────

def test_list_projects_from_mounts(tmp_path, monkeypatch):
    mounts = {"proj-a": "/some/path/a", "proj-b": "/some/path/b"}
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps(mounts))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    importlib.reload(_store_module)
    importlib.reload(mcp_module)
    result = mcp_module._list_projects()
    names = {p["name"] for p in result["projects"]}
    assert "proj-a" in names
    assert "proj-b" in names


def test_list_projects_no_mounts(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "nonexistent.json"))
    importlib.reload(_store_module)
    importlib.reload(mcp_module)
    result = mcp_module._list_projects()
    assert result["projects"] == []


# ── _resolve_question ──────────────────────────────────────────────────────

def test_resolve_question(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {
        "questions": [
            {"id": "q1", "section": "§2", "body": "How?", "opened_by": "smc", "opened_at": "2026-01-01"},
        ]
    })
    result = mcp._resolve_question("proj", "plan-a", "q1", "Use approach X", "smc", expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "plan-a")
    q = data["questions"][0]
    assert q["resolved_at"] is not None
    assert q["resolution"] == "Use approach X"
    assert q["resolved_by"] == "smc"


def test_resolve_question_not_found(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {"questions": []})
    result = mcp._resolve_question("proj", "plan-a", "q99", "x", "smc", expected_version=0)
    assert result["ok"] is False


# ── _add_research ──────────────────────────────────────────────────────────

def test_add_research(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {"research": []})
    item = {
        "id": "r1", "type": "paper", "title": "Study", "source": "arxiv",
        "added_by": "smc", "when": "2026-01-01",
    }
    result = mcp._add_research("proj", "plan-a", item, expected_version=0)
    assert result["ok"] is True
    store = get_store(state_root)
    data, _ = store.read_plan("proj", "plan-a")
    assert len(data["research"]) == 1
    assert data["research"][0]["title"] == "Study"


def test_add_research_missing_fields(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {"research": []})
    result = mcp._add_research("proj", "plan-a", {"id": "r1"}, expected_version=0)
    assert result["ok"] is False
    assert "missing fields" in result["error"]


# ── note ID uniqueness fix ─────────────────────────────────────────────────

def test_append_comment_unique_ids(state_root):
    mcp = get_mcp(state_root)
    _seed_plan(state_root, "proj", "plan-a", {"notes": []})
    # Append two comments in succession
    r1 = mcp._append_comment("proj", "plan-a", "§1", "first", "agent", expected_version=0)
    assert r1["ok"] is True
    r2 = mcp._append_comment("proj", "plan-a", "§1", "second", "agent", expected_version=1)
    assert r2["ok"] is True
    assert r1["note_id"] != r2["note_id"]
    # IDs should be timestamp-based, not n1/n2
    assert not r1["note_id"].startswith("n1")
    assert r1["note_id"].startswith("n-")
