"""Tests for the collapsed edit_plan write tool + read_plan enrichment.

edit_plan(project, slug, ops, expected_version, create=False) is the single
write surface that folds the 15 mutators into one verb-dispatched tool. It
applies ops IN ORDER to a working dict, schema-validates, then persists via the
existing version-checked atomic write. read_plan gains additive with_schema and
discovery (slug=None) modes.

Hermetic fixture mirrors tests/test_mcp_tools.py (tmp docs + mounts via
RECKON_MOUNTS_PATH + RECKON_STATE_ROOT, reload _store + mcp).
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
    """Hermetic temp docs dir + mounts + state root. Returns (docs, state, proj)."""
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


def _make_plan_html(
    docs_dir: Path,
    slug: str,
    state: dict,
    *,
    artifact_type: str = "plan",
    relative: str | None = None,
) -> Path:
    from reckon._plan_html import write_state

    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("status", "active")
    base.setdefault("type", artifact_type)
    bare = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    html = write_state(bare, base)
    path = docs_dir / (relative or f"{slug}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _seed_index(state_root: Path, project: str, data: dict) -> None:
    proj_dir = state_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-01-01T00:00:00",
        "project": project,
        "doc": "index",
        "data": {"_version": 0, **data},
    }
    (proj_dir / "index.json").write_text(json.dumps(envelope, indent=2))


# ── set verb ────────────────────────────────────────────────────────────────


def test_set_scalar(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "draft"})
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "status", "value": "shipped"}], 0
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["status"] == "shipped"


def test_edit_plan_selects_duplicate_leaf_by_doc_type(setup):
    docs_dir, _, project = setup
    plan_path = _make_plan_html(
        docs_dir,
        "shared",
        {"version": 3, "summary": "plan"},
        relative="plans/shared.html",
    )
    research_path = _make_plan_html(
        docs_dir,
        "shared",
        {"version": 7, "summary": "research", "status": ""},
        artifact_type="research",
        relative="research/shared.html",
    )

    result = mcp_module._edit_plan(
        project,
        "shared",
        [{"op": "set", "path": "summary", "value": "selected research"}],
        expected_version=7,
        doc_type="research",
    )

    assert result["ok"] is True
    assert result["path"] == str(research_path)
    assert (
        _store_module.read_plan(project, "shared", artifact_type="research")[0][
            "summary"
        ]
        == "selected research"
    )
    assert (
        _store_module.read_plan(project, "shared", artifact_type="plan")[0]["summary"]
        == "plan"
    )
    assert plan_path.is_file()


def test_edit_plan_rejects_ambiguous_untyped_duplicate_leaf(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "shared", {"version": 3}, relative="plans/shared.html")
    _make_plan_html(
        docs_dir,
        "shared",
        {"version": 7, "status": ""},
        artifact_type="research",
        relative="research/shared.html",
    )

    result = mcp_module._edit_plan(
        project,
        "shared",
        [{"op": "set", "path": "summary", "value": "unsafe"}],
        expected_version=3,
    )

    assert result["ok"] is False
    assert result["error"] == "ambiguous_resource"
    assert "supply doc_type" in result["detail"]


def test_edit_plan_typed_version_and_state_type_are_paired(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "shared", {"version": 3}, relative="plans/shared.html")
    research = _make_plan_html(
        docs_dir,
        "shared",
        {"version": 7, "status": ""},
        artifact_type="research",
        relative="research/shared.html",
    )
    before = research.read_bytes()

    conflict = mcp_module._edit_plan(
        project,
        "shared",
        [{"op": "set", "path": "summary", "value": "wrong version"}],
        expected_version=3,
        doc_type="research",
    )
    assert conflict["error"] == "version_conflict"
    assert conflict["current_version"] == 7

    mismatch = mcp_module._edit_plan(
        project,
        "shared",
        [{"op": "set", "path": "type", "value": "evidence"}],
        expected_version=7,
        doc_type="research",
    )
    assert mismatch["error"] == "schema_validation"
    assert "does not match selected doc_type" in mismatch["details"][0]
    assert research.read_bytes() == before


def test_edit_plan_rejects_typed_non_plan_creation(setup):
    docs_dir, _, project = setup
    result = mcp_module._edit_plan(
        project,
        "new-study",
        [{"op": "set", "path": "title", "value": "Study"}],
        expected_version=0,
        create=True,
        doc_type="research",
    )
    assert result["ok"] is False
    assert "typed creation is not supported" in result["error"]
    assert not (docs_dir / "research" / "new-study.html").exists()


def test_set_impl_clamps(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0})
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "impl", "value": 2.5}], 0
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["impl"] == 1.0
    r2 = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "impl", "value": -3}], data["version"]
    )
    assert r2["ok"] is True
    data2, _ = _store_module.read_plan(project, "plan-a")
    assert data2["impl"] == 0.0


def test_set_decision_field(setup):
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir,
        "plan-a",
        {
            "version": 0,
            "decisions": {
                "transport": {"title": "T?", "choices": ["a", "b"], "choice": ""}
            },
        },
    )
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "decisions.transport.choice", "value": "a"}],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["decisions"]["transport"]["choice"] == "a"
    assert data["decisions"]["transport"]["title"] == "T?"  # preserved


def test_set_sprint_status_active_side_effect(setup):
    _, state_root, project = setup
    _seed_index(
        state_root,
        project,
        {
            "active_sprint_id": None,
            "sprints": [{"id": "S1", "status": "planned", "items": []}],
        },
    )
    r = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "set", "path": "sprints.S1.status", "value": "active"}],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["active_sprint_id"] == "S1"
    assert data["sprints"][0]["status"] == "active"


def test_set_sprint_status_double_active_warns(setup):
    _, state_root, project = setup
    _seed_index(
        state_root,
        project,
        {
            "active_sprint_id": "S1",
            "sprints": [
                {"id": "S1", "status": "active", "items": []},
                {"id": "S2", "status": "planned", "items": []},
            ],
        },
    )
    r = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "set", "path": "sprints.S2.status", "value": "active"}],
        0,
    )
    assert r["ok"] is True
    assert "warnings" in r and any("already active" in w for w in r["warnings"])


def test_set_sprint_status_done_clears_active(setup):
    _, state_root, project = setup
    _seed_index(
        state_root,
        project,
        {
            "active_sprint_id": "S1",
            "sprints": [{"id": "S1", "status": "active", "items": []}],
        },
    )
    r = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "set", "path": "sprints.S1.status", "value": "done"}],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["active_sprint_id"] is None


def test_set_active_sprint_id_index(setup):
    _, state_root, project = setup
    _seed_index(
        state_root,
        project,
        {"active_sprint_id": None, "sprints": [{"id": "S1", "items": []}]},
    )
    r = mcp_module._edit_plan(
        project, "index", [{"op": "set", "path": "active_sprint_id", "value": "S1"}], 0
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["active_sprint_id"] == "S1"


def test_set_inventory_accepted_as_noop(setup):
    """inventory[] is synthesised live; a set on it is accepted (folds
    update_inventory_item) but is a durable no-op — the contract requires it
    stay accepted, not rejected."""
    _, state_root, project = setup
    _seed_index(
        state_root,
        project,
        {"inventory": [{"slug": "plan-x", "status": "active"}]},
    )
    r = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "set", "path": "inventory.plan-x.status", "value": "shipped"}],
        0,
    )
    assert r["ok"] is True  # accepted, not an op_error


# ── append verb ───────────────────────────────────────────────────────────


def test_append_followup(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "followups": []})
    fu = {
        "id": "f1",
        "written_by": "smc",
        "written_at": "2026-01-01",
        "title": "next",
        "body": "do it",
        "prompt": "Project: proj\nDone-when: x",
    }
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "append", "target": "followups", "item": fu}], 0
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["followups"][0]["id"] == "f1"


def test_append_followup_empty_prompt_rejected(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "followups": []})
    fu = {
        "id": "f1",
        "written_by": "smc",
        "written_at": "2026-01-01",
        "title": "next",
        "body": "do it",
        "prompt": "   ",
    }
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "append", "target": "followups", "item": fu}], 0
    )
    assert r["ok"] is False
    assert r["error"] == "op_error"
    # nothing written
    data, ver = _store_module.read_plan(project, "plan-a")
    assert data["followups"] == []
    assert ver == 0


def test_append_followup_autogen_id(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "followups": []})
    fu = {
        "written_by": "smc",
        "written_at": "2026-01-01",
        "title": "next",
        "body": "do it",
        "prompt": "Done-when: x",
    }
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "append", "target": "followups", "item": fu}], 0
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["followups"][0]["id"].startswith("f-")


def test_append_comment_section_and_autogen_id(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "comments": {}})
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "append",
                "target": "comments",
                "section": "s1",
                "item": {"who": "agent", "when": "2026-01-01", "body": "hi"},
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["comments"]["s1"][0]["body"] == "hi"
    assert data["comments"]["s1"][0]["id"].startswith("c-")


def test_append_decision_with_key(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "decisions": {}})
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "append",
                "target": "decisions",
                "key": "transport",
                "item": {"title": "Which?", "choices": ["a", "b"], "choice": ""},
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["decisions"]["transport"]["title"] == "Which?"


def test_append_sprint_new_index(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"active_sprint_id": None, "sprints": []})
    r = mcp_module._edit_plan(
        project,
        "index",
        [
            {
                "op": "append",
                "target": "sprints",
                "item": {"id": "S1", "theme": "alpha", "status": "active"},
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["sprints"][0]["id"] == "S1"
    assert data["active_sprint_id"] == "S1"


def test_append_sprint_duplicate_rejected(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "items": []}]})
    r = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "append", "target": "sprints", "item": {"id": "S1"}}],
        0,
    )
    assert r["ok"] is False
    assert r["error"] == "op_error"


def test_append_sprint_item_and_dup_reject(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"sprints": [{"id": "S1", "items": []}]})
    r = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "append", "target": "sprints.S1.items", "item": "plan-x"}],
        0,
    )
    assert r["ok"] is True
    data, v = _store_module.read_plan(project, "index")
    assert "plan-x" in data["sprints"][0]["items"]
    r2 = mcp_module._edit_plan(
        project,
        "index",
        [{"op": "append", "target": "sprints.S1.items", "item": "plan-x"}],
        v,
    )
    assert r2["ok"] is False
    assert r2["error"] == "op_error"


def test_append_milestone_index(setup):
    _, state_root, project = setup
    _seed_index(state_root, project, {"milestones": []})
    r = mcp_module._edit_plan(
        project,
        "index",
        [
            {
                "op": "append",
                "target": "milestones",
                "item": {"id": "M1", "name": "launch"},
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    assert data["milestones"][0]["id"] == "M1"


# ── resolve verb ────────────────────────────────────────────────────────────


def test_resolve_followup(setup):
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir,
        "plan-a",
        {
            "version": 0,
            "followups": [
                {
                    "id": "f1",
                    "title": "x",
                    "body": "y",
                    "written_by": "smc",
                    "written_at": "2026-01-01",
                    "prompt": "Done-when: z",
                }
            ],
        },
    )
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "smc",
                "outcome": "landed",
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    fu = data["followups"][0]
    assert fu["outcome"] == "landed"
    assert fu["resolved_by"] == "smc"
    assert fu["resolved_at"]


def test_resolve_question(setup):
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir,
        "plan-a",
        {
            "version": 0,
            "questions": [
                {
                    "id": "q1",
                    "section": "§2",
                    "body": "How?",
                    "opened_by": "smc",
                    "opened_at": "2026-01-01",
                }
            ],
        },
    )
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "resolve",
                "target": "questions",
                "id": "q1",
                "by": "smc",
                "resolution": "use X",
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    q = data["questions"][0]
    assert q["resolution"] == "use X"
    assert q["resolved_at"]


def test_resolve_missing_id_rejected(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "followups": []})
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "resolve", "target": "followups", "id": "nope", "by": "x"}],
        0,
    )
    assert r["ok"] is False
    assert r["error"] == "op_error"


# ── lock verb ───────────────────────────────────────────────────────────────


def test_lock_preserves_authored_fields(setup):
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir,
        "plan-a",
        {
            "version": 0,
            "decisions": {
                "transport": {
                    "title": "Which transport?",
                    "context": "ctx",
                    "choices": ["stdio", "http"],
                    "choice": "",
                }
            },
        },
    )
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "lock",
                "key": "transport",
                "choice": "stdio",
                "rationale": "default",
                "by": "simon",
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    dec = data["decisions"]["transport"]
    assert dec["title"] == "Which transport?"
    assert dec["context"] == "ctx"
    assert dec["choices"] == ["stdio", "http"]
    assert dec["choice"] == "stdio"
    assert dec["rationale"] == "default"
    assert dec["by"] == "simon"
    assert dec["when"]


# ── move verb ───────────────────────────────────────────────────────────────


def test_move_sprint_item(setup):
    _, state_root, project = setup
    item = {"slug": "plan-a", "why_now": "priority", "tier": "opus"}
    _seed_index(
        state_root,
        project,
        {
            "sprints": [
                {"id": "S1", "items": [item]},
                {"id": "S2", "items": []},
            ]
        },
    )
    r = mcp_module._edit_plan(
        project,
        "index",
        [
            {
                "op": "move",
                "target": "sprint_item",
                "slug": "plan-a",
                "from": "S1",
                "to": "S2",
            }
        ],
        0,
    )
    assert r["ok"] is True
    data, _ = _store_module.read_plan(project, "index")
    s1 = next(s for s in data["sprints"] if s["id"] == "S1")
    s2 = next(s for s in data["sprints"] if s["id"] == "S2")
    assert s1["items"] == []
    assert s2["items"][0]["why_now"] == "priority"  # metadata preserved


def test_move_not_in_from_rejected(setup):
    _, state_root, project = setup
    _seed_index(
        state_root,
        project,
        {
            "sprints": [
                {"id": "S1", "items": []},
                {"id": "S2", "items": []},
            ]
        },
    )
    r = mcp_module._edit_plan(
        project,
        "index",
        [
            {
                "op": "move",
                "target": "sprint_item",
                "slug": "x",
                "from": "S1",
                "to": "S2",
            }
        ],
        0,
    )
    assert r["ok"] is False
    assert r["error"] == "op_error"


# ── create ──────────────────────────────────────────────────────────────────


def test_create_new_plan(setup):
    docs_dir, _, project = setup
    r = mcp_module._edit_plan(
        project,
        "brand-new",
        [
            {"op": "set", "path": "title", "value": "Brand New"},
            {"op": "set", "path": "status", "value": "active"},
        ],
        expected_version=0,
        create=True,
    )
    assert r["ok"] is True
    assert r.get("created") is True
    assert (docs_dir / "plans" / "brand-new.html").exists()
    assert r["path"] == str((docs_dir / "plans" / "brand-new.html").resolve())
    data, _ = _store_module.read_plan(project, "brand-new")
    assert data["title"] == "Brand New"
    assert data["status"] == "active"
    assert data["slug"] == "brand-new"


def test_create_existing_rejected(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0})
    r = mcp_module._edit_plan(project, "plan-a", [], expected_version=0, create=True)
    assert r["ok"] is False
    assert "already exists" in r["error"]


def test_edit_missing_plan_rejected(setup):
    _, _, project = setup
    r = mcp_module._edit_plan(
        project, "ghost", [{"op": "set", "path": "status", "value": "active"}], 0
    )
    assert r["ok"] is False
    assert "not found" in r["error"]


# ── schema REJECT (write nothing) ─────────────────────────────────────────


def test_reject_invalid_status_enum(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "active"})
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "status", "value": "bogus"}], 0
    )
    assert r["ok"] is False
    assert r["error"] == "schema_validation"
    assert any("status" in d for d in r["details"])
    # nothing written: status unchanged, version unchanged
    data, ver = _store_module.read_plan(project, "plan-a")
    assert data["status"] == "active"
    assert ver == 0


def test_reject_blank_required_title(setup):
    """Setting a required-on-write scalar (title) to empty is rejected by
    validate_for_write — nothing is written."""
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir, "plan-a", {"version": 0, "title": "T", "status": "active"}
    )
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "title", "value": ""}], 0
    )
    assert r["ok"] is False
    assert r["error"] == "schema_validation"
    assert any("title" in d for d in r["details"])
    data, ver = _store_module.read_plan(project, "plan-a")
    assert data["title"] == "T"
    assert ver == 0


def test_reject_preexisting_empty_followup_prompt_via_validate(setup):
    """A plan carrying a followup with an empty §05 prompt becomes un-editable via
    edit_plan: even an UNRELATED edit re-validates the whole dict and is rejected
    at the validate_for_write boundary (the reject half of reject-write-warn).
    This is the documented sharp edge — the granular tools are the escape hatch."""
    docs_dir, _, project = setup
    # Seed a plan whose existing followup already has an empty prompt (a violation
    # that the lenient read path tolerates).
    _make_plan_html(
        docs_dir,
        "plan-a",
        {
            "version": 0,
            "title": "Plan A",
            "status": "active",
            "followups": [
                {
                    "id": "f1",
                    "title": "x",
                    "body": "y",
                    "written_by": "smc",
                    "written_at": "2026-01-01",
                    "prompt": "",  # pre-existing violation
                }
            ],
        },
    )
    # An unrelated edit (set status) must still be rejected — the whole dict is
    # validated, and the empty prompt trips validate_for_write.
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "status", "value": "shipped"}], 0
    )
    assert r["ok"] is False
    assert r["error"] == "schema_validation"
    assert any("prompt" in d.lower() for d in r["details"])
    data, ver = _store_module.read_plan(project, "plan-a")
    assert data["status"] == "active"  # unchanged
    assert ver == 0


def test_create_missing_required_rejected_and_no_orphan(setup):
    """A create that fails schema validation must leave NO stub file behind
    (contract: on failure → no write) and must not block a later retry."""
    docs_dir, _, project = setup
    r = mcp_module._edit_plan(
        project,
        "new-plan",
        [{"op": "set", "path": "slug", "value": ""}],
        expected_version=0,
        create=True,
    )
    assert r["ok"] is False
    assert r["error"] == "schema_validation"
    assert any("slug" in d for d in r["details"])
    # No orphan stub: the failed create wrote nothing durable.
    assert not (docs_dir / "plans" / "new-plan.html").exists()
    data, ver = _store_module.read_plan(project, "new-plan")
    assert data == {}
    assert ver == 0
    # And a clean retry is unblocked (not "already exists").
    r2 = mcp_module._edit_plan(
        project,
        "new-plan",
        [{"op": "set", "path": "title", "value": "New Plan"}],
        expected_version=0,
        create=True,
    )
    assert r2["ok"] is True
    assert (docs_dir / "plans" / "new-plan.html").exists()


def test_create_op_error_no_orphan(setup):
    """A create whose ops fail at the op layer also leaves no stub behind."""
    docs_dir, _, project = setup
    r = mcp_module._edit_plan(
        project,
        "new-plan",
        [{"op": "frobnicate"}],
        expected_version=0,
        create=True,
    )
    assert r["ok"] is False
    assert r["error"] == "op_error"
    assert not (docs_dir / "plans" / "new-plan.html").exists()


# ── version conflict ──────────────────────────────────────────────────────


def test_version_conflict(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "active"})
    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "status", "value": "shipped"}], 99
    )
    assert r["ok"] is False
    assert r["error"] == "version_conflict"


# ── multi-op ordering ─────────────────────────────────────────────────────


def test_multiple_ops_applied_in_order(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "decisions": {}})
    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "append",
                "target": "decisions",
                "key": "d1",
                "item": {"title": "Q?", "choices": ["a", "b"], "choice": ""},
            },
            {"op": "lock", "key": "d1", "choice": "a", "rationale": "r", "by": "smc"},
            {"op": "set", "path": "status", "value": "shipped"},
        ],
        0,
    )
    assert r["ok"] is True
    data, ver = _store_module.read_plan(project, "plan-a")
    assert data["decisions"]["d1"]["choice"] == "a"
    assert data["status"] == "shipped"
    assert ver == 1  # one version bump for the whole batch


# ── unknown verb ───────────────────────────────────────────────────────────


def test_unknown_verb_rejected(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0})
    r = mcp_module._edit_plan(project, "plan-a", [{"op": "frobnicate"}], 0)
    assert r["ok"] is False
    assert r["error"] == "op_error"


# ── read_plan enrichment ──────────────────────────────────────────────────


def test_read_plan_unchanged_shape(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "active"})
    r = mcp_module._read_plan(project, "plan-a")
    assert set(r.keys()) == {"project", "slug", "version", "data"}
    assert r["data"]["status"] == "active"


def test_read_plan_with_schema(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0})
    r = mcp_module._read_plan(project, "plan-a", with_schema=True)
    assert "schema" in r
    assert r["schema"]["title"] == "reckon PlanState"
    assert "op_vocab" in r and "set" in r["op_vocab"]
    assert "dos_donts" in r


def test_read_plan_discovery(setup):
    docs_dir, state_root, project = setup
    _make_plan_html(
        docs_dir,
        "alpha",
        {
            "version": 0,
            "status": "active",
            "followups": [
                {
                    "id": "f1",
                    "title": "t",
                    "body": "b",
                    "written_by": "x",
                    "written_at": "2026-01-01",
                    "prompt": "p",
                }
            ],
        },
    )
    _seed_index(
        state_root,
        project,
        {"sprints": [{"id": "S1", "items": []}], "active_sprint_id": "S1"},
    )
    r = mcp_module._read_plan(project)  # slug omitted
    slugs = {p["slug"] for p in r["plans"]}
    assert "alpha" in slugs
    assert r["active_sprint_id"] == "S1"
    assert any(f["id"] == "f1" for f in r["followups"])


def test_read_plan_discovery_filters_and_summary(setup):
    docs_dir, state_root, project = setup
    _make_plan_html(
        docs_dir,
        "alpha",
        {
            "slug": "alpha",
            "title": "Alpha Plan",
            "summary": "Architecture baseline",
            "status": "active",
            "impl": 0.4,
            "roi": "high",
            "effort": "L",
            "tier": "opus",
            "owner": "smc",
            "sprint": "S1",
            "milestone": "M1",
            "depends_on": ["beta"],
            "blocks": ["gamma"],
            "informs": ["delta"],
            "followups": [
                {
                    "id": "f1",
                    "title": "next",
                    "body": "b",
                    "written_by": "x",
                    "written_at": "2026-01-01",
                    "prompt": "p",
                }
            ],
            "version": 2,
        },
    )
    _make_plan_html(
        docs_dir,
        "beta",
        {
            "slug": "beta",
            "title": "Beta Plan",
            "summary": "Operational doc",
            "status": "pending",
            "impl": 0.1,
            "roi": "mid",
            "effort": "M",
            "tier": "sonnet",
            "owner": "other",
            "sprint": "S2",
            "milestone": "M2",
            "version": 1,
        },
    )
    _seed_index(
        state_root,
        project,
        {
            "sprints": [{"id": "S1", "items": [{"slug": "alpha"}]}],
            "active_sprint_id": "S1",
        },
    )
    r = mcp_module._read_plan(
        project,
        status="active",
        owner="smc",
        search="architecture",
        include_followups=False,
        include_questions=False,
    )
    assert [p["slug"] for p in r["plans"]] == ["alpha"]
    plan = r["plans"][0]
    assert plan["type"] == "plan"
    assert plan["tier"] == "opus"
    assert plan["owner"] == "smc"
    assert plan["href"] == "alpha"
    assert plan["version"] == 2
    assert plan["depends_on"] == ["beta"]
    assert plan["blocks"] == ["gamma"]
    assert plan["informs"] == ["delta"]
    assert r["followups"] == []
    assert r["questions"] == []
    assert r["summary"]["plans"] == 1
    assert r["summary"]["open_followups"] == 1
    assert r["summary"]["by_status"] == {"blocked": 1}
    assert r["summary"]["by_sprint"] == {"S1": 1}
    assert r["summary"]["by_milestone"] == {"M1": 1}


def test_read_plan_projects_list(setup):
    _, _, project = setup
    r = mcp_module._read_plan()  # no project
    names = {p["name"] for p in r["projects"]}
    assert project in names
    r2 = mcp_module._read_plan("*")
    names2 = {p["name"] for p in r2["projects"]}
    assert project in names2


# ── checkout_path / multi-worktree routing ─────────────────────────────────
#
# A stdio MCP server resolves every project to the single FIXED docs dir in
# mounts.json (the MAIN checkout) — it has NO access to a caller's cwd. A
# sub-agent running in a git worktree (a separate checkout of the same repo)
# therefore had its writes land in the MAIN checkout, not its worktree. The
# `checkout_path` param (root in the store layer) redirects both the plan-HTML
# path AND the index/project JSON state path into the named checkout.


@pytest.fixture()
def worktree(tmp_path):
    """A second checkout NOT registered in mounts.json — the 'agent worktree'.

    Returns its repo root (the dir containing docs/), simulating
    .claude/worktrees/agent-XXX. Its docs/ tree is independent of the mounts-
    registered MAIN checkout created by the `setup` fixture.
    """
    root = tmp_path / "worktree"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "state").mkdir()
    return root


def test_edit_plan_checkout_path_writes_to_worktree_html(setup, worktree):
    """A plan HTML edit with checkout_path lands in the worktree, NOT in the
    mounts-registered main checkout."""
    docs_dir, _, project = setup
    # Same slug exists in BOTH checkouts; the worktree copy is what we target.
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "draft"})
    _make_plan_html(worktree / "docs", "plan-a", {"version": 0, "status": "draft"})

    r = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "status", "value": "shipped"}],
        0,
        checkout_path=str(worktree),
    )
    assert r["ok"] is True
    # The returned path is the worktree file (reconciliation aid).
    assert r["path"] == str((worktree / "docs" / "plan-a.html").resolve())

    # Worktree copy changed; MAIN copy is UNTOUCHED.
    wt_data, _ = _store_module.read_plan(project, "plan-a", str(worktree))
    assert wt_data["status"] == "shipped"
    main_data, main_ver = _store_module.read_plan(project, "plan-a")
    assert main_data["status"] == "draft"
    assert main_ver == 0


def test_edit_plan_checkout_path_writes_index_to_worktree(setup, worktree):
    """THE reported bug: an index (sprint) edit with checkout_path must write
    <worktree>/docs/state/<project>/index.json — NOT the config-home state root
    (symlinked to the main checkout)."""
    _, state_root, project = setup
    # Seed BOTH the main state root and the worktree state dir with the index.
    _seed_index(
        state_root,
        project,
        {"active_sprint_id": None, "sprints": []},
    )
    wt_state = worktree / "docs" / "state" / project
    wt_state.mkdir(parents=True)
    (wt_state / "index.json").write_text(
        json.dumps(
            {
                "updated": "2026-01-01T00:00:00",
                "project": project,
                "doc": "index",
                "data": {"_version": 0, "active_sprint_id": None, "sprints": []},
            },
            indent=2,
        )
    )

    r = mcp_module._edit_plan(
        project,
        "index",
        [
            {
                "op": "append",
                "target": "sprints",
                "item": {"id": "S6", "theme": "worktree sprint", "status": "planned"},
            }
        ],
        0,
        checkout_path=str(worktree),
    )
    assert r["ok"] is True
    assert r["path"] == str((wt_state / "index.json").resolve())

    # The appended sprint is isolated to the worktree index.
    wt_data, _ = _store_module.read_plan(project, "index", str(worktree))
    assert any(s["id"] == "S6" for s in wt_data["sprints"])

    # MAIN checkout's index is UNTOUCHED — no duplicate left behind.
    main_data, main_ver = _store_module.read_plan(project, "index")
    assert main_data["sprints"] == []
    assert main_ver == 0


def test_edit_plan_no_checkout_path_uses_mounts(setup, worktree):
    """Backward-compat: omitting checkout_path writes the mounts-registered MAIN
    checkout, leaving the worktree untouched (existing single-checkout behaviour
    is preserved)."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "draft"})
    _make_plan_html(worktree / "docs", "plan-a", {"version": 0, "status": "draft"})

    r = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "status", "value": "shipped"}], 0
    )
    assert r["ok"] is True
    assert r["path"] == str((docs_dir / "plan-a.html").resolve())

    main_data, _ = _store_module.read_plan(project, "plan-a")
    assert main_data["status"] == "shipped"
    wt_data, _ = _store_module.read_plan(project, "plan-a", str(worktree))
    assert wt_data["status"] == "draft"  # worktree untouched


def test_read_plan_checkout_path_reads_worktree(setup, worktree):
    """read_plan(checkout_path=...) reads the worktree copy, not the main one."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "draft"})
    _make_plan_html(worktree / "docs", "plan-a", {"version": 0, "status": "active"})

    r_main = mcp_module._read_plan(project, "plan-a")
    assert r_main["data"]["status"] == "draft"
    r_wt = mcp_module._read_plan(project, "plan-a", checkout_path=str(worktree))
    assert r_wt["data"]["status"] == "active"


def test_read_plan_discovery_checkout_path_reads_worktree_inventory(setup, worktree):
    """Discovery mode should use the checkout_path inventory, not the main mount."""
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir,
        "plan-a",
        {
            "slug": "plan-a",
            "title": "Plan A",
            "status": "draft",
            "impl": 0.1,
            "version": 0,
        },
    )
    _make_plan_html(
        worktree / "docs",
        "plan-a",
        {
            "slug": "plan-a",
            "title": "Plan A",
            "status": "active",
            "impl": 0.9,
            "version": 3,
        },
    )

    r_main = mcp_module._read_plan(project)
    r_wt = mcp_module._read_plan(project, checkout_path=str(worktree))
    main_plan = next(p for p in r_main["plans"] if p["slug"] == "plan-a")
    wt_plan = next(p for p in r_wt["plans"] if p["slug"] == "plan-a")

    assert main_plan["status"] == "draft"
    assert main_plan["impl"] == 0.1
    assert wt_plan["status"] == "active"
    assert wt_plan["impl"] == 0.9
    assert wt_plan["version"] == 3


def test_edit_plan_create_in_worktree(setup, worktree):
    """create=True with checkout_path scaffolds the new plan in the worktree."""
    docs_dir, _, project = setup
    r = mcp_module._edit_plan(
        project,
        "brand-new",
        [
            {"op": "set", "path": "title", "value": "Brand New"},
            {"op": "set", "path": "status", "value": "active"},
        ],
        expected_version=0,
        create=True,
        checkout_path=str(worktree),
    )
    assert r["ok"] is True
    assert r.get("created") is True
    # Created in the WORKTREE, not the main docs dir.
    assert (worktree / "docs" / "plans" / "brand-new.html").exists()
    assert not (docs_dir / "brand-new.html").exists()


def test_edit_plan_read_unchanged_shape_has_no_path(setup):
    """Guard the read-result contract: the single-plan read shape stays exactly
    {project, slug, version, data} — `path` is a WRITE-result field only."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "plan-a", {"version": 0, "status": "active"})
    r = mcp_module._read_plan(project, "plan-a", checkout_path=str(docs_dir.parent))
    assert set(r.keys()) == {"project", "slug", "version", "data"}
