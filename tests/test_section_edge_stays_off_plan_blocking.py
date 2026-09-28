"""A section-scoped wait stays with its section and never blocks the plan."""

from __future__ import annotations

import importlib
import json
from html import escape
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module


@pytest.fixture()
def project_docs(tmp_path, monkeypatch):
    """One mounted project whose plans live in the fixture's own docs tree."""

    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
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
    status: str = "active",
    depends_on: list[str] | None = None,
    section_depends_on: dict[str, list[str]] | None = None,
) -> Path:
    """Write one plan, optionally holding a whole-plan or section-scoped ref."""

    from reckon._plan_html import write_state

    state: dict = {
        "slug": slug,
        "title": slug,
        "summary": "A plan whose sections can wait on their own.",
        "version": 3,
        "type": "plan",
        "status": status,
        "impl": 0.4,
    }
    if depends_on:
        state["depends_on"] = list(depends_on)
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    html = write_state(bare, state)
    if section_depends_on:
        meta = (
            '<meta name="plan-section-depends-on" content="'
            f'{escape(json.dumps(section_depends_on), quote=True)}">'
        )
        html = html.replace("<title>", meta + "<title>", 1)
    path = docs_dir / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _summary(project: str, slug: str) -> dict:
    return mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": slug}
    )


def test_section_wait_reports_the_section_not_the_plan(project_docs):
    docs_dir, project = project_docs
    _plan(docs_dir, "plan-b")
    _plan(
        docs_dir,
        "plan-a",
        section_depends_on={"s5": ["plan-b#s3"]},
    )

    result = _summary(project, "plan-a")

    assert result["blocking"] == []
    assert result["state"]["effective_status"] == "active"
    assert result["section_blocking"] == [
        {
            "section": "s5",
            "ready": False,
            "waits_on": [
                {"ref": "plan-b#s3", "found": True, "status": "active", "stage": "s3"}
            ],
        }
    ]


def test_whole_plan_dependency_still_blocks_the_plan(project_docs):
    docs_dir, project = project_docs
    _plan(docs_dir, "plan-b")
    _plan(docs_dir, "plan-c", depends_on=["plan-b"])

    result = _summary(project, "plan-c")

    assert result["blocking"] == [{"ref": "plan-b", "found": True, "status": "active"}]
    assert result["state"]["effective_status"] == "blocked"
    assert result["section_blocking"] == []


def test_whole_plan_and_section_waits_are_reported_side_by_side(project_docs):
    docs_dir, project = project_docs
    _plan(docs_dir, "plan-b")
    _plan(
        docs_dir,
        "plan-d",
        depends_on=["plan-b"],
        section_depends_on={"s2": ["plan-b#s4"]},
    )

    result = _summary(project, "plan-d")

    assert result["blocking"] == [{"ref": "plan-b", "found": True, "status": "active"}]
    assert result["state"]["effective_status"] == "blocked"
    assert [row["section"] for row in result["section_blocking"]] == ["s2"]
    assert result["section_blocking"][0]["waits_on"] == [
        {"ref": "plan-b#s4", "found": True, "status": "active", "stage": "s4"}
    ]


def test_satisfied_section_wait_leaves_no_section_row(project_docs):
    docs_dir, project = project_docs
    _plan(docs_dir, "plan-b", status="shipped")
    _plan(docs_dir, "plan-e", section_depends_on={"s3": ["plan-b#s1"]})

    result = _summary(project, "plan-e")

    assert result["blocking"] == []
    assert result["section_blocking"] == []
    assert result["state"]["effective_status"] == "active"


def test_a_plan_without_section_edges_reports_no_section_blocking(project_docs):
    docs_dir, project = project_docs
    _plan(docs_dir, "plan-f")

    result = _summary(project, "plan-f")

    assert result["blocking"] == []
    assert result["section_blocking"] == []
