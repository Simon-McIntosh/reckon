"""A section edge reports what it holds, and a malformed mapping is named."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

import pytest

import reckon._schema as schema_module
import reckon.mcp as mcp_module
import reckon.roadmap as roadmap_module
from tests.mcp_family_reload import reload_mcp_family


@pytest.fixture()
def docs_tree(tmp_path, monkeypatch):
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
    reload_mcp_family()
    return docs_dir, project


def _plan(
    docs_dir: Path,
    slug: str,
    *,
    status: str = "active",
    section_depends_on: dict | None = None,
    raw_mapping: str | None = None,
) -> Path:
    """Write one plan, optionally declaring a section-scoped edge in its head."""

    from reckon._plan_html import write_state

    state = {
        "slug": slug,
        "title": slug,
        "summary": "A plan whose sections can wait on their own.",
        "version": 3,
        "type": "plan",
        "status": status,
        "impl": 0.4,
    }
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    html = write_state(bare, state)
    content = None
    if raw_mapping is not None:
        content = escape(raw_mapping, quote=True)
    elif section_depends_on is not None:
        content = escape(json.dumps(section_depends_on), quote=True)
    if content is not None:
        html = html.replace(
            "<title>",
            f'<meta name="plan-section-depends-on" content="{content}">' + "<title>",
            1,
        )
    path = docs_dir / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _summary(project: str, slug: str) -> dict:
    return mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": slug}
    )


def test_a_single_section_wait_reports_the_section_held_not_the_plan(docs_tree):
    """The plan's one wait is section-scoped, so the plan itself stays unblocked."""

    docs_dir, project = docs_tree
    _plan(docs_dir, "plan-b")
    _plan(docs_dir, "plan-a", section_depends_on={"s5": ["plan-b#s3"]})

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


@pytest.mark.parametrize(
    ("raw_mapping", "malformation"),
    [
        ("{not json", "not valid JSON"),
        ("[1, 2, 3]", "got list"),
    ],
)
def test_a_malformed_mapping_is_named_in_a_finding(
    docs_tree, raw_mapping, malformation
):
    """Invalid JSON and non-object JSON each yield a finding, never silence."""

    docs_dir, project = docs_tree
    _plan(docs_dir, "plan-m", raw_mapping=raw_mapping)

    text = (docs_dir / "plan-m.html").read_text(encoding="utf-8")
    mapping = schema_module.section_depends_on(text)

    assert mapping is not None, "a malformed value must not read as no declaration"
    assert not any(schema_module.is_section_identity(key) for key in mapping)

    plan = {"slug": "plan-m"}
    resolved = roadmap_module._plan_section_deps(plan, docs_dir, project, "plan-m")
    rows, findings = roadmap_module._section_scoped_edges(
        project, plan, "plan-m", resolved or {}, {}, docs_dir
    )

    assert rows == []
    (finding,) = findings
    assert finding["slug"] == "plan-m"
    assert finding["code"] == "invalid-section-dependency"
    assert malformation in finding["message"]
    assert raw_mapping in finding["message"]


def test_a_malformed_entry_does_not_drop_the_valid_edge_beside_it(docs_tree):
    """The valid entry still waits while the malformed one is refused by name."""

    docs_dir, project = docs_tree
    _plan(docs_dir, "plan-b")
    _plan(
        docs_dir,
        "plan-mix",
        raw_mapping='{"s5": ["plan-b#s3"], "not a section!": ["plan-b#s3"]}',
    )

    text = (docs_dir / "plan-mix.html").read_text(encoding="utf-8")
    mapping = schema_module.section_depends_on(text)
    assert mapping is not None

    plan = {"slug": "plan-mix"}
    resolved = roadmap_module._plan_section_deps(plan, docs_dir, project, "plan-mix")
    rows, findings = roadmap_module._section_scoped_edges(
        project, plan, "plan-mix", resolved or {}, {}, docs_dir
    )

    assert [row["source_section"] for row in rows] == ["s5"]
    assert [row["ref"] for row in rows] == ["plan-b#s3"]
    assert rows[0]["satisfied"] is False
    assert [finding["code"] for finding in findings] == [
        "invalid-section-dependency",
        "dangling-hard-dependency",
    ]
    assert findings[0]["extra"]["section"] == "not a section!"
    assert findings[1]["extra"]["section"] == "s5"
