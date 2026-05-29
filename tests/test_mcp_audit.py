"""Tests for the MCP audit tool — plan-schema conformance audit.

This is the WARN half of the locked reject-write-warn-doctor decision: it
validates every plan in a project against the PlanState schema (non-raising),
recomputes index rollups in the response, and NEVER mutates a plan or
index.json. Distinct from the CLI `reckon doctor` (infra/skills/mounts checker
tested in tests/test_doctor.py — NOT touched here).

Hermetic fixture mirrors tests/test_mcp_tools.py.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module


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

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    importlib.reload(_store_module)
    importlib.reload(mcp_module)

    return docs_dir, state_root, project


def _make_plan_html(
    docs_dir: Path, slug: str, state: dict, *, with_project: bool = True
) -> Path:
    from reckon._plan_html import write_state

    proj_meta = '<meta name="docs-project" content="proj">' if with_project else ""
    bare = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f"{proj_meta}"
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    html = write_state(bare, state)
    path = docs_dir / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _valid_state(slug: str) -> dict:
    return {"slug": slug, "title": slug.title(), "status": "active", "version": 0}


# ── all conformant ───────────────────────────────────────────────────────


def test_audit_all_conformant(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))
    _make_plan_html(docs_dir, "beta", _valid_state("beta"))
    r = mcp_module._audit(project)
    assert r["checked"] == 2
    assert r["conformant"] == 2
    assert r["violations"] == []
    assert r["reindexed"] is True


# ── seeded violation: invalid status enum ─────────────────────────────────


def test_audit_reports_invalid_status(setup):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))
    _make_plan_html(
        docs_dir,
        "bad",
        {"slug": "bad", "title": "Bad", "status": "not-a-status", "version": 0},
    )
    r = mcp_module._audit(project)
    assert r["checked"] == 2
    assert r["conformant"] == 1
    bad = next(v for v in r["violations"] if v["slug"] == "bad")
    assert any("status" in e for e in bad["errors"])


# ── seeded violation: empty §05 followup prompt ───────────────────────────


def test_audit_reports_empty_followup_prompt(setup):
    docs_dir, _, project = setup
    _make_plan_html(
        docs_dir,
        "withfu",
        {
            "slug": "withfu",
            "title": "With FU",
            "status": "active",
            "version": 0,
            "followups": [
                {
                    "id": "f1",
                    "title": "x",
                    "body": "y",
                    "written_by": "smc",
                    "written_at": "2026-01-01",
                    "prompt": "",
                }
            ],
        },
    )
    r = mcp_module._audit(project)
    bad = next(v for v in r["violations"] if v["slug"] == "withfu")
    assert any("prompt" in e.lower() for e in bad["errors"])


# ── doctor is read-only — does not mutate plans ───────────────────────────


def test_audit_does_not_mutate(setup):
    docs_dir, _, project = setup
    p = _make_plan_html(
        docs_dir,
        "bad",
        {"slug": "bad", "title": "Bad", "status": "not-a-status", "version": 0},
    )
    before = p.read_text(encoding="utf-8")
    _, ver_before = _store_module.read_plan(project, "bad")
    mcp_module._audit(project)
    after = p.read_text(encoding="utf-8")
    _, ver_after = _store_module.read_plan(project, "bad")
    assert before == after
    assert ver_before == ver_after == 0


# ── rollups recomputed in response ────────────────────────────────────────


def test_audit_returns_rollups(setup):
    docs_dir, state_root, project = setup
    _make_plan_html(docs_dir, "alpha", {**_valid_state("alpha"), "sprint": "S1"})
    r = mcp_module._audit(project)
    assert "rollups" in r
    assert r["rollups"]["plans"] == 1
    # S1 referenced by a plan is synthesised into the sprint rollup
    assert any(s.get("id") == "S1" for s in r["rollups"]["sprints"])


# ── unknown project ───────────────────────────────────────────────────────


def test_audit_unknown_project(setup):
    _, _, _ = setup
    r = mcp_module._audit("nope")
    assert r["ok"] is False
