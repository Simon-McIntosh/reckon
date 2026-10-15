"""An impl write is refused wherever the read derives impl.

A plan whose section records do not cover its declarations serves the
declaration count as its impl, sourced ``declarations``; one whose records
cover them serves the effort-weighted figure, sourced ``computed``. Either way
every read discards a stored figure, so the store refuses ``set impl`` in both
cases rather than accept a write that would never be served.

A plan also changes its impl rule silently on its first section record, so the
append or insert that writes that record carries a warning naming the change.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from reckon import _plan_html
from reckon import mcp as mcp_module

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}

DECLARATIONS = {"s1": "done", "s2": "implementable"}


def _record(section_id: str, status: str) -> dict:
    return {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "attempts": 0,
        "status": status,
        "links": [],
    }


def _state(slug: str, sections: list[dict]) -> dict:
    return {
        "project": "sample",
        "type": "plan",
        "slug": slug,
        "title": "Partial records",
        "status": "active",
        "modified": "2026-10-02",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        "sections": sections,
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
                "prompt": "/reckon-build partial-records",
            }
        ],
    }


def _write_plan(checkout: Path, slug: str, sections: list[dict]) -> Path:
    path = checkout / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<title>Partial records</title></head><body><main class="plan-doc">'
        '<h2 id="s1">First section</h2><p>First body.</p>'
        '<h2 id="s2">Second section</h2><p>Second body.</p>'
        "</main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(authored, _state(slug, sections)), encoding="utf-8"
    )
    return path


def _edit(checkout: Path, slug: str, op: dict) -> dict:
    path = checkout / "docs" / "plans" / f"{slug}.html"
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        "sample",
        slug,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[op],
    )


def test_set_impl_is_refused_while_records_are_partial(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    slug = "partial-records"
    path = _write_plan(checkout, slug, [_record("s1", "done")])

    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert state["sections"], "the plan must carry a record the store can see"
    assert (
        _plan_html.derive_impl_from_sections(
            state["sections"], state["section_declarations"]
        )
        is None
    ), "the derived figure must not answer for a plan with partial records"
    # The served figure is the declaration count: one done of one done plus one
    # implementable.
    assert state["impl"] == 0.5
    assert state["impl_source"] == "declarations"
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, slug, {"op": "set", "path": "impl", "value": 0.25})

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "counted from the section declarations" in result["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_set_impl_is_refused_when_records_cover_the_declarations(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "repo"
    slug = "covered-records"
    path = _write_plan(
        checkout, slug, [_record("s1", "done"), _record("s2", "implementable")]
    )
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert (
        _plan_html.derive_impl_from_sections(
            state["sections"], state["section_declarations"]
        )
        is not None
    ), "the derived figure must answer once the records cover the declarations"
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, slug, {"op": "set", "path": "impl", "value": 0.25})

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "computed" in result["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_first_section_record_append_warns_impl_becomes_computed(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "repo"
    slug = "first-record"
    path = _write_plan(checkout, slug, [])

    result = _edit(
        checkout,
        slug,
        {
            "op": "append",
            "target": "sections",
            "item": {
                "id": "s1",
                "effort_hours": 1.0,
                "capability": deepcopy(CAPABILITY),
                "links": [],
            },
        },
    )

    assert result["ok"] is True, result
    warnings = " ".join(result.get("warnings") or [])
    assert (
        "impl becomes computed once the records cover every declared section"
        in warnings
    ), result
    written = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert [record["id"] for record in written["sections"]] == ["s1"]


def test_first_section_record_insert_warns_impl_becomes_computed(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "repo"
    slug = "first-inserted-record"
    path = _write_plan(checkout, slug, [])

    result = _edit(
        checkout,
        slug,
        {
            "op": "insert_section",
            "id": "s3",
            "title": "Third section",
            "body": "<p>Third body.</p>",
            "effort_hours": 1.0,
            "capability": deepcopy(CAPABILITY),
            "links": [],
        },
    )

    assert result["ok"] is True, result
    warnings = " ".join(result.get("warnings") or [])
    assert (
        "impl becomes computed once the records cover every declared section"
        in warnings
    ), result
    written = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert [record["id"] for record in written["sections"]] == ["s3"]
