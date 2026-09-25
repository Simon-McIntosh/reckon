from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon import _store as store_module
from reckon import mcp as mcp_module
from reckon.cli import main

DECLARATIONS = {"s1": "done", "s2": "implementable"}
SECTION_RECORDS = [
    {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "attempts": 0,
        "status": status,
        "links": [],
    }
    for section_id, status in DECLARATIONS.items()
]


@pytest.fixture()
def plan(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / "section-writes.html"
    path.parent.mkdir(parents=True)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<title>Section writes</title></head><body><main class="plan-doc">'
        '<h2 id="s1">First section</h2><p>First body.</p>'
        '<h2 id="s2">Second section</h2><p>Second body.</p>'
        '<p id="unrelated-prose">Unrelated prose immediately before state.</p>'
        "</main></body></html>"
    )
    state = {
        "project": "sample",
        "type": "plan",
        "slug": "section-writes",
        "title": "Section writes",
        "status": "active",
        "modified": "2026-09-25",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        "sections": deepcopy(SECTION_RECORDS),
        "gates": [
            {
                "id": "input-present",
                "status": "closed",
                "measure": "Required input is present",
                "verdict": "passed",
                "evidence": "fixture",
            }
        ],
        "followups": [
            {
                "id": "next-action",
                "status": "open",
                "prompt": "/reckon-build section-writes",
            }
        ],
    }
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return checkout, path


def _edit(checkout: Path, path: Path, op: dict) -> dict:
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        "sample",
        "section-writes",
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[op],
    )


def test_dotted_declaration_set_changes_one_key_and_preserves_records(plan) -> None:
    checkout, path = plan
    expected_records = deepcopy(SECTION_RECORDS)
    expected_records[1]["status"] = "done"

    result = _edit(
        checkout,
        path,
        {"op": "set", "path": "section_declarations.s2", "value": "done"},
    )

    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert result["ok"] is True, result
    assert state["section_declarations"] == {"s1": "done", "s2": "done"}
    assert state["sections"] == expected_records


def test_unknown_declaration_value_is_refused_with_the_accepted_values(plan) -> None:
    checkout, path = plan
    before = path.read_text(encoding="utf-8")

    result = _edit(
        checkout,
        path,
        {
            "op": "set",
            "path": "section_declarations.s2",
            "value": "unknown",
        },
    )

    assert result == {
        "ok": False,
        "error": "op_error",
        "detail": (
            "section declaration must be one of "
            "['implementable', 'deferred', 'done']; got 'unknown'"
        ),
    }
    assert path.read_text(encoding="utf-8") == before


def test_insert_section_uses_the_structured_boundary_and_passes_audit(plan) -> None:
    checkout, path = plan

    result = _edit(
        checkout,
        path,
        {
            "op": "insert_section",
            "id": "s3",
            "title": "Third section",
            "body": "<p>Inserted body.</p>",
        },
    )

    text = path.read_text(encoding="utf-8")
    state = _plan_html.read_state(text)
    assert result["ok"] is True, result
    assert text.index('id="unrelated-prose"') < text.index('<h2 id="s3">')
    assert text.index('<h2 id="s3">') < text.index('<section data-reckon="gates"')
    assert state["section_declarations"] == DECLARATIONS
    assert state["sections"] == SECTION_RECORDS
    audit = CliRunner().invoke(main, ["audit-doc", str(path)])
    assert audit.exit_code == 0, audit.output


def test_authored_text_edit_still_refuses_a_structured_overlap(plan) -> None:
    _checkout, path = plan
    text = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match=r"overlaps a section.*structured-state"):
        store_module._replace_authored_html(
            text,
            '<section data-reckon="gates"',
            '<section data-reckon="evidence"',
            selector_name="old_html",
        )
