"""An edit op accepts the input forms its examples show.

Three spellings the documented examples use were refused while a sibling
spelling of the same statement worked: a visibility flag written as a JSON
boolean (``archived: true``) while the meta string ``"1"`` is accepted; a
followup append built from the append example (id, title, body, prompt) and
refused for the ``written_by`` and ``written_at`` the example never shows, and
a sprint append refusing a plan committed to the tree.

Each case is driven through the MCP edit tool against a temporary project and
asserted on the document that was written, not on the reply alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "example-forms"
SLUG = "example-plan"


@pytest.fixture()
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    reload_mcp_family()
    return docs_dir


def _write_plan(docs: Path, slug: str) -> Path:
    state = {
        "type": "plan",
        "slug": slug,
        "project": PROJECT,
        "title": slug.title(),
        "status": "active",
        "section_declarations": {"s1": "implementable", "s2": "implementable"},
    }
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def test_a_boolean_flag_is_accepted_like_its_string_spelling(docs: Path) -> None:
    _write_plan(docs, SLUG)
    _write_plan(docs, "control-plan")

    string_result = mcp_module._edit_plan(
        PROJECT,
        "control-plan",
        [{"op": "set", "path": "archived", "value": "1"}],
        0,
    )
    assert string_result["ok"] is True, string_result

    boolean_result = mcp_module._edit_plan(
        PROJECT,
        SLUG,
        [{"op": "set", "path": "archived", "value": True}],
        0,
    )
    assert boolean_result["ok"] is True, boolean_result

    boolean_written = Path(boolean_result["path"]).read_text(encoding="utf-8")
    string_written = Path(string_result["path"]).read_text(encoding="utf-8")
    assert 'name="plan-archived" content="1"' in boolean_written
    assert 'name="plan-archived" content="1"' in string_written


def test_a_followup_append_defaults_its_author_and_date(docs: Path) -> None:
    _write_plan(docs, SLUG)

    result = mcp_module._edit_plan(
        PROJECT,
        SLUG,
        [
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": "f-example",
                    "title": "Implement the next pass",
                    "body": "The documented example carries no provenance.",
                    "prompt": f"/reckon-build {SLUG} §2",
                },
            }
        ],
        0,
    )
    assert result["ok"] is True, result

    written = Path(result["path"]).read_text(encoding="utf-8")
    assert 'data-id="f-example"' in written, written[:600]
    assert 'data-written-by="reckon-edit"' in written, written[:600]
    assert f'data-written-at="{datetime.now(tz=UTC).date().isoformat()}"' in written


def test_a_sprint_append_resolves_a_committed_plan(tmp_path: Path, docs: Path) -> None:
    from reckon import project_state

    # The project is in distributed mode and the sprint exists before the plan
    # does, so nothing run ahead of the append could have registered it.
    project_state.create_project_state(docs, PROJECT)
    sprint_path = docs / "sprints" / "S1.html"
    sprint_path.parent.mkdir(parents=True)
    sprint_path.write_text(
        project_state._render_resource(PROJECT, "sprint", "S1", {"items": []}),
        encoding="utf-8",
    )
    assert not (docs / "plans" / f"{SLUG}.html").exists()

    _write_plan(docs, SLUG)
    _, version = project_state.read_resource(docs, PROJECT, "sprint", "S1")

    result = mcp_module._edit_plan(
        PROJECT,
        "S1",
        [{"op": "append", "target": "items", "item": {"slug": SLUG}}],
        version,
        checkout_path=str(tmp_path),
        doc_type="sprint",
    )
    assert result["ok"] is True, result

    sprint, version = project_state.read_resource(docs, PROJECT, "sprint", "S1")
    assert [
        row if isinstance(row, str) else row.get("slug") for row in sprint["items"]
    ] == [SLUG]

    # A slug with no plan in the tree is still refused, so the acceptance above
    # is the plan being resolved and not the check being absent.
    refused = mcp_module._edit_plan(
        PROJECT,
        "S1",
        [{"op": "append", "target": "items", "item": {"slug": "no-such-plan"}}],
        version,
        checkout_path=str(tmp_path),
        doc_type="sprint",
    )
    assert refused["ok"] is False, refused
    assert "does not resolve to a live plan" in json.dumps(refused), refused
