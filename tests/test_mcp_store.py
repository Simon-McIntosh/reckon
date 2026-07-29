"""Tests for reckon._store — semantic-HTML-backed plan store.

All plan operations (non-index/project slugs) use semantic HTML state files.
index/project slugs remain JSON-backed.

Uses a tempdir docs root with plan .html files containing embedded
<script id="reckon-owned sections ins.  RECKON_MOUNTS_PATH and
RECKON_STATE_ROOT point to temp paths so no live files are touched.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as _store_module


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Set up a temp docs dir + mounts + state root.

    Returns (docs_dir, state_root, project_name).
    """
    project = "test-proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    # Write mounts.json so _docs_dir_for_project works
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    # Also patch serve.py module-level _MOUNTS_FILE / _STATE_ROOT so that
    # load_mounts() inside _resolve_plan_file sees our temp mounts.
    import reckon.serve as serve_mod
    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    importlib.reload(_store_module)

    return docs_dir, state_root, project


def _make_plan_html(docs_dir: Path, slug: str, state: dict) -> Path:
    """Write a minimal plan HTML with an embedded semantic state."""
    from reckon._plan_html import write_state
    bare = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="test-proj">'
        f'<title>{slug}</title></head>\n'
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    html_with_island = write_state(bare, state)
    path = docs_dir / f"{slug}.html"
    path.write_text(html_with_island, encoding="utf-8")
    return path


# ── read_plan ──────────────────────────────────────────────────────────────

def test_read_plan_absent_returns_empty(setup):
    """read_plan on a missing HTML file returns ({}, 0)."""
    _, _, project = setup
    data, version = _store_module.read_plan(project, "nonexistent")
    assert data == {}
    assert version == 0


def test_read_plan_html_island(setup):
    """read_plan reads the semantic HTML state directly."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "my-plan", {
        "slug": "my-plan",
        "title": "My Plan",
        "status": "active",
        "version": 3,
    })
    data, version = _store_module.read_plan(project, "my-plan")
    assert version == 3
    assert data["status"] == "active"
    assert data["title"] == "My Plan"


# ── write_plan round-trip ──────────────────────────────────────────────────

def test_write_plan_roundtrip(setup):
    """write_plan rewrites the semantic HTML state; read_plan recovers the data."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"slug": "plan-a", "status": "draft", "version": 0})

    initial_data = {"slug": "plan-a", "status": "active", "impl": 0.5}
    new_version = _store_module.write_plan(project, "plan-a", initial_data, expected_version=0)
    assert new_version == 1

    data, version = _store_module.read_plan(project, "plan-a")
    assert version == 1
    assert data["status"] == "active"
    assert data["impl"] == pytest.approx(0.5)
    assert data["version"] == 1
    # No _version in the semantic HTML state
    assert "_version" not in data


def test_write_plan_modified_date_set(setup):
    """write_plan sets 'modified' to today's date (YYYY-MM-DD)."""
    from datetime import date
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-b", {"version": 0})
    _store_module.write_plan(project, "plan-b", {"status": "active"}, expected_version=0)
    data, _ = _store_module.read_plan(project, "plan-b")
    assert data["modified"] == date.today().isoformat()


def test_write_plan_atomic_tmp_rename(setup):
    """write_plan uses .html.tmp then renames — no partial write visible."""
    docs_dir, _, project = setup
    plan_file = _make_plan_html(docs_dir, "plan-c", {"version": 0})
    tmp_file = plan_file.with_suffix(".html.tmp")
    # tmp should not exist before write
    assert not tmp_file.exists()
    _store_module.write_plan(project, "plan-c", {"status": "active"}, expected_version=0)
    # tmp should be cleaned up after write
    assert not tmp_file.exists()
    assert plan_file.exists()


# ── VersionConflict ────────────────────────────────────────────────────────

def test_version_conflict_raises(setup):
    """write_plan raises VersionConflict when expected_version is stale."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-d", {"version": 0})
    _store_module.write_plan(project, "plan-d", {"status": "pending"}, expected_version=0)
    # version is now 1; passing 0 again should conflict
    with pytest.raises(_store_module.VersionConflict) as exc_info:
        _store_module.write_plan(project, "plan-d", {"status": "active"}, expected_version=0)
    exc = exc_info.value
    assert exc.expected == 0
    assert exc.current == 1


def test_version_conflict_carries_current_data(setup):
    """VersionConflict carries the current state data."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-e", {"version": 0, "status": "draft"})
    _store_module.write_plan(project, "plan-e", {"status": "active"}, expected_version=0)
    try:
        _store_module.write_plan(project, "plan-e", {"status": "blocked"}, expected_version=0)
        assert False, "should have raised"
    except _store_module.VersionConflict as exc:
        assert exc.current_data["status"] == "active"


# ── lock_decision preserves authored fields ────────────────────────────────

def test_lock_decision_preserves_title_context_choices(setup):
    """set_nested merges into existing decision entry — title/context/choices survive."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-f", {
        "version": 0,
        "decisions": {
            "transport": {
                "title": "Which transport?",
                "context": "stdio vs HTTP",
                "choices": ["stdio", "http"],
                "choice": "",
            }
        }
    })
    _store_module.set_nested(
        project, "plan-f", "decisions", "transport",
        {"choice": "stdio", "rationale": "default for MCP", "when": "2026-05-26", "by": "simon"},
        expected_version=0,
    )
    data, _ = _store_module.read_plan(project, "plan-f")
    dec = data["decisions"]["transport"]
    # Authored fields preserved
    assert dec["title"] == "Which transport?"
    assert dec["context"] == "stdio vs HTTP"
    assert dec["choices"] == ["stdio", "http"]
    # Lock fields written
    assert dec["choice"] == "stdio"
    assert dec["rationale"] == "default for MCP"
    assert dec["by"] == "simon"


def test_lock_decision_creates_new_entry(setup):
    """set_nested creates a new decision entry when key is absent."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-g", {"version": 0, "decisions": {}})
    _store_module.set_nested(
        project, "plan-g", "decisions", "new-decision",
        {"choice": "yes", "rationale": "obvious", "when": "2026-05-26", "by": "simon"},
        expected_version=0,
    )
    data, _ = _store_module.read_plan(project, "plan-g")
    assert data["decisions"]["new-decision"]["choice"] == "yes"


# ── append_followup ────────────────────────────────────────────────────────

def test_append_followup(setup):
    """append_to_list adds followups to the semantic HTML state."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-h", {"version": 0, "followups": []})

    followup = {
        "id": "f1", "title": "Next step", "body": "Do something",
        "written_by": "smc", "written_at": "2026-05-26", "prompt": "...",
        "status": "open",
    }
    new_version = _store_module.append_to_list(project, "plan-h", "followups", followup, expected_version=0)
    assert new_version == 1

    data, _ = _store_module.read_plan(project, "plan-h")
    assert len(data["followups"]) == 1
    assert data["followups"][0]["id"] == "f1"

    # Append a second
    followup2 = {"id": "f2", "title": "More", "body": "x", "written_by": "smc",
                 "written_at": "2026-05-26", "prompt": "y", "status": "open"}
    _store_module.append_to_list(project, "plan-h", "followups", followup2, expected_version=1)
    data, _ = _store_module.read_plan(project, "plan-h")
    assert len(data["followups"]) == 2


# ── resolve_in_list ────────────────────────────────────────────────────────

def test_resolve_in_list(setup):
    """resolve_in_list updates matching item by id."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-i", {
        "version": 0,
        "followups": [
            {"id": "f1", "title": "Open followup", "status": "open"},
            {"id": "f2", "title": "Other followup", "status": "open"},
        ]
    })
    new_version = _store_module.resolve_in_list(
        project, "plan-i", "followups", "f1",
        {"resolved_at": "2026-05-26T12:00:00", "outcome": "done"},
        expected_version=0,
    )
    assert new_version == 1

    data, _ = _store_module.read_plan(project, "plan-i")
    resolved = next(f for f in data["followups"] if f["id"] == "f1")
    assert resolved["outcome"] == "done"
    assert resolved["title"] == "Open followup"  # preserved

    other = next(f for f in data["followups"] if f["id"] == "f2")
    assert "outcome" not in other


def test_resolve_in_list_missing_id_raises(setup):
    """resolve_in_list raises KeyError for an unknown id."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-j", {"version": 0, "followups": [{"id": "f1"}]})
    with pytest.raises(KeyError):
        _store_module.resolve_in_list(project, "plan-j", "followups", "NOPE", {"outcome": "x"}, expected_version=0)


# ── list_followups_across ──────────────────────────────────────────────────

def test_list_followups_across(setup):
    """list_followups_across scans all plan HTML files in the docs dir."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-k", {
        "slug": "plan-k", "title": "Plan K", "version": 0,
        "followups": [
            {"id": "f1", "title": "open followup", "status": "open"},
            {"id": "f2", "title": "resolved", "status": "resolved", "resolved_at": "2026-05-26"},
        ]
    })
    _make_plan_html(docs_dir, "plan-l", {
        "slug": "plan-l", "title": "Plan L", "version": 0,
        "followups": [
            {"id": "f3", "title": "another open", "status": "open"},
        ]
    })
    results = _store_module.list_followups_across(project, unresolved_only=True)
    ids = {r["id"] for r in results}
    assert "f1" in ids
    assert "f3" in ids
    assert "f2" not in ids  # resolved

    # Check plan_slug and plan_title are attached
    f1_entry = next(r for r in results if r["id"] == "f1")
    assert f1_entry["plan_slug"] == "plan-k"
    assert f1_entry["plan_title"] == "Plan K"


def test_list_followups_across_include_resolved(setup):
    """list_followups_across with unresolved_only=False includes resolved."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-m", {
        "slug": "plan-m", "version": 0,
        "followups": [
            {"id": "f1", "resolved_at": "2026-05-26", "title": "done"},
        ]
    })
    results = _store_module.list_followups_across(project, unresolved_only=False)
    assert any(r["id"] == "f1" for r in results)


def test_list_followups_across_no_docs_dir(setup):
    """list_followups_across returns [] when project is not mounted."""
    _store_module.list_followups_across("unknown-project")  # no error
    assert _store_module.list_followups_across("unknown-project") == []


# ── index slug still uses JSON ─────────────────────────────────────────────

def test_index_slug_uses_json(setup):
    """The 'index' slug reads/writes JSON, not HTML."""
    _, state_root, project = setup
    # Write directly to the JSON state path
    proj_state = state_root / project
    proj_state.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": "index",
        "data": {"_version": 0, "sprints": [{"id": "S1"}], "milestones": []},
    }
    (proj_state / "index.json").write_text(json.dumps(envelope, indent=2))

    data, version = _store_module.read_plan(project, "index")
    assert version == 0
    assert data["sprints"][0]["id"] == "S1"
    assert "_version" in data  # JSON path keeps _version in data

    # Write back
    new_version = _store_module.write_plan(project, "index", data, expected_version=0)
    assert new_version == 1
    data2, v2 = _store_module.read_plan(project, "index")
    assert v2 == 1
    assert data2["_version"] == 1


def test_project_slug_uses_json(setup):
    """The 'project' slug reads/writes JSON."""
    _, state_root, project = setup
    proj_state = state_root / project
    proj_state.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": "project",
        "data": {"_version": 0, "name": "Test Project"},
    }
    (proj_state / "project.json").write_text(json.dumps(envelope, indent=2))

    data, version = _store_module.read_plan(project, "project")
    assert version == 0
    assert data["name"] == "Test Project"
