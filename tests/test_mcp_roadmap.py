from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state


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
    assert result["critical_path"]["plans"] == ["foundation", "consumer"]


def test_fastmcp_registers_roadmap_and_text_edit_tools() -> None:
    names = {item.name for item in mcp_module.mcp._tool_manager.list_tools()}

    assert {"_roadmap", "_edit_plan_text"} <= names


def test_mcp_roadmap_portfolio_rolls_up_mounted_projects(mounted_project) -> None:
    _project, docs = mounted_project
    _write_plan(docs, "work")

    result = mcp_module._roadmap("*")

    assert result["portfolio"]["projects"] == 1
    assert result["portfolio"]["plans"] == 1
    assert result["portfolio"]["ready"] == 1
    assert result["portfolio"]["blocked"] == 0
    assert result["portfolio"]["deferred"] == 0


def test_edit_plan_text_replaces_exact_prose_and_advances_version(
    mounted_project,
) -> None:
    project, docs = mounted_project
    path = _write_plan(docs, "editable", body='<p id="intro">Old prose.</p>')

    result = mcp_module._edit_plan_text(
        project,
        "editable",
        '<p id="intro">Old prose.</p>',
        '<p id="intro">Revised prose with <strong>evidence</strong>.</p>',
        expected_version=0,
        doc_type="plan",
    )

    assert result["ok"] is True
    assert result["new_version"] == 1
    assert result["path"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert "Revised prose with <strong>evidence</strong>." in text
    assert 'name="plan-version" content="1"' in text


def test_edit_plan_text_rejects_structured_state_changes(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "editable")

    result = mcp_module._edit_plan_text(
        project,
        "editable",
        'name="plan-status" content="active"',
        'name="plan-status" content="shipped"',
        expected_version=0,
        doc_type="plan",
    )

    assert result["ok"] is False
    assert result["error"] == "text_edit_error"
    assert "structured plan state" in result["detail"]


def test_edit_plan_text_requires_one_exact_match(mounted_project) -> None:
    project, docs = mounted_project
    _write_plan(docs, "editable", body="<p>same</p><p>same</p>")

    result = mcp_module._edit_plan_text(
        project,
        "editable",
        "<p>same</p>",
        "<p>changed</p>",
        expected_version=0,
        doc_type="plan",
    )

    assert result["ok"] is False
    assert "found 2 occurrences" in result["detail"]
