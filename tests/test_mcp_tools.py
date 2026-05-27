"""Tests for reckon MCP tools — semantic-HTML-backed plan store.

Sprint/milestone/inventory tools still use index.json (JSON-backed).
Per-plan tools (followups, questions, research, comments, lock_decision, etc.)
use semantic HTML state files via the new _store.py backing.

Test setup:
  - A tempdir docs root with plan .html files (semantic HTML state backing).
  - RECKON_MOUNTS_PATH → a temp mounts.json mapping project → docs dir.
  - RECKON_STATE_ROOT  → a temp state dir (for index/project JSON slugs).
  - serve.py module-level paths patched to match.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Provide a hermetic temp docs dir + mounts + state root.

    Returns (docs_dir, state_root, project).
    """
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod
    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    importlib.reload(_store_module)
    importlib.reload(mcp_module)

    return docs_dir, state_root, project


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_plan_html(docs_dir: Path, slug: str, state: dict) -> Path:
    """Write a minimal plan HTML with an embedded semantic state."""
    from reckon._plan_html import write_state
    bare = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f'<title>{slug}</title></head>\n'
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    html_with_island = write_state(bare, state)
    path = docs_dir / f"{slug}.html"
    path.write_text(html_with_island, encoding="utf-8")
    return path


def _seed_index(state_root: Path, project: str, data: dict) -> None:
    """Write a bare index.json envelope to the state root (JSON-backed)."""
    proj_dir = state_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": "index",
        "data": {"_version": 0, **data},
    }
    (proj_dir / "index.json").write_text(json.dumps(envelope, indent=2))


# ── _list_sprints ──────────────────────────────────────────────────────────

def test_list_sprints_returns_sprints(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {
        "active_sprint_id": "S1",
        "sprints": [{"id": "S1", "theme": "alpha", "status": "active", "items": []}],
        "milestones": [{"id": "M1", "name": "launch"}],
    })
    result = mcp_module._list_sprints(project)
    assert result["active_sprint_id"] == "S1"
    assert len(result["sprints"]) == 1
    assert result["sprints"][0]["theme"] == "alpha"
    assert result["milestones"][0]["id"] == "M1"


def test_list_sprints_empty_project(setup):
    _, _, project = setup
    result = mcp_module._list_sprints("noproject")
    assert result["sprints"] == []
    assert result["milestones"] == []


# ── _update_sprint ─────────────────────────────────────────────────────────

def test_update_sprint_status(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {
        "active_sprint_id": None,
        "sprints": [{"id": "S1", "status": "planned", "items": []}],
    })
    result = mcp_module._update_sprint(project, "S1", {"status": "active"}, expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["active_sprint_id"] == "S1"
    assert data["sprints"][0]["status"] == "active"


def test_update_sprint_done_clears_active_id(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {
        "active_sprint_id": "S1",
        "sprints": [{"id": "S1", "status": "active", "items": []}],
    })
    result = mcp_module._update_sprint(project, "S1", {"status": "done"}, expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["active_sprint_id"] is None


def test_update_sprint_warns_on_double_active(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {
        "active_sprint_id": "S1",
        "sprints": [
            {"id": "S1", "status": "active", "items": []},
            {"id": "S2", "status": "planned", "items": []},
        ],
    })
    result = mcp_module._update_sprint(project, "S2", {"status": "active"}, expected_version=0)
    assert result["ok"] is True
    assert "warning" in result


def test_update_sprint_rejects_items_key(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "items": []}]})
    result = mcp_module._update_sprint(project, "S1", {"items": []}, expected_version=0)
    assert result["ok"] is False
    assert "items" in result["error"]


def test_update_sprint_not_found(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": []})
    result = mcp_module._update_sprint(project, "S99", {"status": "done"}, expected_version=0)
    assert result["ok"] is False


def test_update_sprint_version_conflict(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "status": "planned", "items": []}]})
    result = mcp_module._update_sprint(project, "S1", {"status": "active"}, expected_version=99)
    assert result["ok"] is False
    assert result["error"] == "version_conflict"


# ── _add_sprint_item ───────────────────────────────────────────────────────

def test_add_sprint_item_string(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "items": []}]})
    result = mcp_module._add_sprint_item(project, "S1", "my-plan", expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert "my-plan" in data["sprints"][0]["items"]


def test_add_sprint_item_object(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "items": []}]})
    item = {"slug": "plan-x", "why_now": "gates M1", "tier": "sonnet", "done_when": "tests green"}
    result = mcp_module._add_sprint_item(project, "S1", item, expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["sprints"][0]["items"][0]["why_now"] == "gates M1"


def test_add_sprint_item_duplicate_rejected(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "items": ["plan-x"]}]})
    result = mcp_module._add_sprint_item(project, "S1", "plan-x", expected_version=0)
    assert result["ok"] is False
    assert "already in sprint" in result["error"]


def test_add_sprint_item_sprint_not_found(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": []})
    result = mcp_module._add_sprint_item(project, "S99", "plan-x", expected_version=0)
    assert result["ok"] is False


# ── _move_sprint_item ──────────────────────────────────────────────────────

def test_move_sprint_item_basic(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [
        {"id": "S1", "items": ["plan-a", "plan-b"]},
        {"id": "S2", "items": []},
    ]})
    result = mcp_module._move_sprint_item(project, "plan-a", "S1", "S2", expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    s1 = next(s for s in data["sprints"] if s["id"] == "S1")
    s2 = next(s for s in data["sprints"] if s["id"] == "S2")
    assert "plan-a" not in s1["items"]
    assert "plan-a" in s2["items"]
    assert "plan-b" in s1["items"]


def test_move_sprint_item_preserves_object_fields(setup):
    _, state_root, project = setup
    item = {"slug": "plan-a", "why_now": "priority", "tier": "opus"}
    _seed_index(state_root, project, {"sprints": [
        {"id": "S1", "items": [item]},
        {"id": "S2", "items": []},
    ]})
    mcp_module._move_sprint_item(project, "plan-a", "S1", "S2", expected_version=0)
    data, _ = _store_module.read_plan(project, "index")
    moved = data["sprints"][1]["items"][0]
    assert moved["why_now"] == "priority"


def test_move_sprint_item_not_found(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [
        {"id": "S1", "items": []},
        {"id": "S2", "items": []},
    ]})
    result = mcp_module._move_sprint_item(project, "plan-x", "S1", "S2", expected_version=0)
    assert result["ok"] is False


# ── _update_inventory_item ─────────────────────────────────────────────────

def test_update_inventory_item(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"inventory": [
        {"slug": "plan-x", "status": "active", "impl": 0.0},
    ]})
    result = mcp_module._update_inventory_item(project, "plan-x", {"impl": 0.75, "status": "shipped"}, expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    item = data["inventory"][0]
    assert item["impl"] == 0.75
    assert item["status"] == "shipped"


def test_update_inventory_item_not_found(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"inventory": []})
    result = mcp_module._update_inventory_item(project, "missing", {"status": "shipped"}, expected_version=0)
    assert result["ok"] is False
    assert "not found in inventory" in result["error"]


# ── _list_followups (semantic HTML state scan) ────────────────────────────────────

def test_list_followups_across_plans(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {
        "slug": "plan-a", "title": "Plan A", "version": 0,
        "followups": [
            {"id": "f1", "title": "next step", "body": "x", "written_by": "smc",
             "written_at": "2026-01-01", "prompt": "p"},
            {"id": "f2", "title": "done", "body": "y", "written_by": "smc",
             "written_at": "2026-01-01", "prompt": "p",
             "resolved_at": "2026-01-02", "outcome": "shipped"},
        ]
    })
    _make_plan_html(docs_dir, "plan-b", {
        "slug": "plan-b", "title": "Plan B", "version": 0,
        "followups": [
            {"id": "f3", "title": "another", "body": "z", "written_by": "smc",
             "written_at": "2026-01-01", "prompt": "p"},
        ]
    })
    result = mcp_module._list_followups(project, unresolved_only=True)
    assert result["count"] == 2
    slugs = {f["plan_slug"] for f in result["followups"]}
    assert "plan-a" in slugs
    assert "plan-b" in slugs
    ids = {f["id"] for f in result["followups"]}
    assert "f1" in ids
    assert "f3" in ids
    assert "f2" not in ids  # resolved


def test_list_followups_include_resolved(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {
        "slug": "plan-a", "version": 0,
        "followups": [
            {"id": "f1", "title": "a", "body": "b", "written_by": "x",
             "written_at": "2026-01-01", "prompt": "p",
             "resolved_at": "2026-01-02"},
        ]
    })
    result = mcp_module._list_followups(project, unresolved_only=False)
    assert result["count"] == 1


# ── _list_questions (semantic HTML state scan) ────────────────────────────────────

def test_list_questions_across_plans(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {
        "slug": "plan-a", "version": 0,
        "questions": [
            {"id": "q1", "section": "§2", "body": "Which approach?",
             "opened_by": "smc", "opened_at": "2026-01-01"},
            {"id": "q2", "section": "§3", "body": "Done?",
             "opened_by": "smc", "opened_at": "2026-01-01",
             "resolved_at": "2026-01-02"},
        ]
    })
    result = mcp_module._list_questions(project, unresolved_only=True)
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


# ── _resolve_question (semantic HTML state) ───────────────────────────────────────

def test_resolve_question(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {
        "slug": "plan-a", "version": 0,
        "questions": [
            {"id": "q1", "section": "§2", "body": "How?",
             "opened_by": "smc", "opened_at": "2026-01-01"},
        ]
    })
    result = mcp_module._resolve_question(project, "plan-a", "q1", "Use approach X", "smc", expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    q = data["questions"][0]
    assert q["resolved_at"] is not None
    assert q["resolution"] == "Use approach X"
    assert q["resolved_by"] == "smc"


def test_resolve_question_not_found(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"slug": "plan-a", "version": 0, "questions": []})
    result = mcp_module._resolve_question(project, "plan-a", "q99", "x", "smc", expected_version=0)
    assert result["ok"] is False


# ── _add_research (semantic HTML state) ────────────────────────────────────────────

def test_add_research(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"slug": "plan-a", "version": 0, "research": []})
    item = {
        "id": "r1", "type": "paper", "title": "Study", "source": "arxiv",
        "added_by": "smc", "when": "2026-01-01",
    }
    result = mcp_module._add_research(project, "plan-a", item, expected_version=0)
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert len(data["research"]) == 1
    assert data["research"][0]["title"] == "Study"


def test_add_research_missing_fields(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"slug": "plan-a", "version": 0, "research": []})
    result = mcp_module._add_research(project, "plan-a", {"id": "r1"}, expected_version=0)
    assert result["ok"] is False
    assert "missing fields" in result["error"]


# ── note ID uniqueness (semantic HTML state) ──────────────────────────────────────

def test_append_comment_unique_ids(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"slug": "plan-a", "version": 0, "comments": {}})
    r1 = mcp_module._append_comment(project, "plan-a", "s1", "first", "agent", expected_version=0)
    assert r1["ok"] is True
    r2 = mcp_module._append_comment(project, "plan-a", "s1", "second", "agent", expected_version=1)
    assert r2["ok"] is True
    # comments land in the section-keyed comments map (rendered as data-reckon="comments")
    assert r1["comment_id"] != r2["comment_id"]
    assert r1["comment_id"].startswith("c-")
    data, _ = _store_module.read_plan(project, "plan-a")
    bodies = [c["body"] for c in data["comments"]["s1"]]
    assert bodies == ["first", "second"]


# ── lock_decision preserves authored fields ────────────────────────────────

def test_lock_decision_preserves_authored_fields(setup):
    """_lock_decision MUST NOT drop title/context/choices from an existing decision entry."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {
        "slug": "plan-a", "version": 0,
        "decisions": {
            "transport": {
                "title": "Which transport to use?",
                "context": "stdio is default for MCP",
                "choices": ["stdio", "http"],
                "choice": "",
            }
        }
    })
    result = mcp_module._lock_decision(
        project, "plan-a", "transport",
        choice="stdio", rationale="default for Claude Code", by="simon",
        expected_version=0,
    )
    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    dec = data["decisions"]["transport"]
    # Authored fields must survive the lock
    assert dec["title"] == "Which transport to use?"
    assert dec["context"] == "stdio is default for MCP"
    assert dec["choices"] == ["stdio", "http"]
    # Locked fields must be present
    assert dec["choice"] == "stdio"
    assert dec["rationale"] == "default for Claude Code"
    assert dec["by"] == "simon"
    assert "when" in dec


# ── _list_plans discovery (semantic HTML state scan) ───────────────────────────────

def test_list_plans_discovery(setup):
    """_list_plans uses HTML discovery to return plan inventory."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "alpha", {
        "slug": "alpha", "title": "Alpha Plan", "status": "active",
        "version": 0,
    })
    _make_plan_html(docs_dir, "beta", {
        "slug": "beta", "title": "Beta Plan", "status": "shipped",
        "version": 0,
    })
    result = mcp_module._list_plans(project)
    slugs = {p["slug"] for p in result["plans"]}
    assert "alpha" in slugs
    assert "beta" in slugs


def test_list_plans_status_filter(setup):
    """_list_plans filters by status when provided."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "alpha", {
        "slug": "alpha", "title": "Active", "status": "active", "version": 0,
    })
    _make_plan_html(docs_dir, "beta", {
        "slug": "beta", "title": "Shipped", "status": "shipped", "version": 0,
    })
    result = mcp_module._list_plans(project, status="active")
    assert len(result["plans"]) == 1
    assert result["plans"][0]["slug"] == "alpha"
