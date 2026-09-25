"""One-section MCP reads preserve authored prose and its typed context."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state


@pytest.fixture()
def mounted_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    project = "section-project"
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


def _write_plan(
    docs_dir: Path,
    project: str,
    slug: str,
    *,
    sections: list[dict] | None = None,
) -> Path:
    authored = """
    <h2 id="s2">§2 — Neighbouring section</h2>
    <p>This prose must not leak into the selected section.</p>
    <h2 id="s3">§3 — Focused transport</h2>
    <p>The copper-orchid phrase exists only in this authored body.</p>
    <ul><li>Preserve <strong>inline markup</strong>.</li></ul>
    <h2 id="s4">§4 — Later section</h2>
    <p>This prose is beyond the selected boundary.</p>
    """
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{authored}</main></body></html>'
    )
    state = {
        "slug": slug,
        "title": "Section reader",
        "summary": "A summary without the distinctive search words.",
        "version": 4,
        "type": "plan",
        "status": "active",
        "impl": 0.25,
        "section_declarations": {"s2": "done", "s3": "implementable"},
        "comments": {
            "s3": [
                {
                    "id": "focused-comment",
                    "who": "reviewer",
                    "when": "2026-09-24",
                    "body": "<p>The section-specific observation.</p>",
                }
            ],
            "s4": [
                {
                    "id": "later-comment",
                    "who": "reviewer",
                    "when": "2026-09-24",
                    "body": "<p>This belongs to a different section.</p>",
                }
            ],
        },
    }
    if sections is not None:
        state["sections"] = sections
    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def test_section_view_returns_only_the_requested_authored_section(mounted_docs):
    docs_dir, project = mounted_docs
    _write_plan(docs_dir, project, "focused")

    result = mcp_module._read_plan(
        project=project,
        slug="focused",
        view="section",
        section="s3",
    )

    assert result["view"] == "section"
    assert result["section"]["id"] == "s3"
    assert result["section"]["heading"] == "§3 — Focused transport"
    assert "copper-orchid phrase" in result["section"]["text"]
    assert "Neighbouring section" not in result["section"]["text"]
    assert "Later section" not in result["section"]["text"]
    assert '<h2 id="s3">' in result["section"]["html"]
    assert "<strong>inline markup</strong>" in result["section"]["html"]
    assert result["section"]["declaration"] == "implementable"
    assert [comment["id"] for comment in result["section"]["comments"]] == [
        "focused-comment"
    ]


def test_section_view_refuses_an_absent_identity_by_name(mounted_docs):
    docs_dir, project = mounted_docs
    _write_plan(docs_dir, project, "focused")

    result = mcp_module._read_plan(
        project=project,
        slug="focused",
        view="section",
        section="missing-section",
    )

    assert result["error"] == "section_not_found"
    assert "missing-section" in result["message"]
    assert "available sections: s2, s3, s4" in result["message"]


def test_discovery_search_matches_authored_plan_body(mounted_docs):
    docs_dir, project = mounted_docs
    _write_plan(docs_dir, project, "focused")

    result = mcp_module._read_plan(
        project=project,
        search="copper-orchid",
        include_followups=False,
        include_questions=False,
        view="summary",
    )

    assert [resource["slug"] for resource in result["resources"]] == ["focused"]


def test_section_view_uses_adjacent_typed_record_without_leaking_it(mounted_docs):
    docs_dir, project = mounted_docs
    record = {
        "id": "s3",
        "effort_hours": 1.5,
        "capability": {
            "version": "1.0",
            "class": "routine",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "attempts": 2,
        "status": "implementable",
        "links": ["inputs#ready"],
    }
    _write_plan(docs_dir, project, "recorded", sections=[record])

    result = mcp_module._read_plan(
        project=project,
        slug="recorded",
        view="section",
        section="s3",
    )

    assert result["section"]["record"] == record
    assert 'data-reckon="section"' not in result["section"]["html"]


def test_registered_read_tool_exposes_an_optional_section_selector():
    signature = inspect.signature(mcp_module._read_plan_tool)
    assert signature.parameters["section"].default is None

    tool = next(
        item
        for item in mcp_module.mcp._tool_manager.list_tools()
        if item.name == "read_plan"
    )
    assert "section" in tool.parameters["properties"]
    assert "section" not in set(tool.parameters.get("required") or [])
