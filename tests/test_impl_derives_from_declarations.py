"""A plan derives its impl from the section declarations when records fall short.

Three sources answer in order. Records covering every declaration derive the
effort-weighted figure (``computed``). Records that do not cover the
declarations fall back to the declaration count (``declarations``), so a plan
with an implementable declaration never reads 1.0 while its records are
partial. A plan with no declaration carries no derived figure, so the authored
``plan-impl`` meta stands (``authored``). The audit reports the file's meta
against the derived figure, so a reader of the file and a reader of the server
agree once the next write lands.
"""

from __future__ import annotations

import html as html_module
import json
from copy import deepcopy
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import _plan_html, doccheck
from reckon._plan_html import parse_meta, parse_plan, read_state
from reckon._schema import IMPL_SOURCE_ENUM, PlanState

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}

#: A section declared done with nothing recorded is the case the fallback
#: answers: the declaration says the section is finished, so done over done
#: plus implementable is one of two.
PARTIAL_DECLARATIONS = {"s1": "done", "s2": "implementable"}

RECORD = (
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="{hours}"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard" data-capability-verification="strict"'
    ' data-capability-risk="low" data-attempts="0" data-status="{status}"'
    ' data-links=""></section>'
)


def _plan_text(
    *,
    declarations: dict | None,
    records: list[tuple[str, float, str]],
    impl: str | None,
) -> str:
    body: list[str] = []
    for sid, hours, status in records:
        body.append(f'<h2 id="{sid}">The {sid} work</h2>')
        body.append(RECORD.format(sid=sid, hours=hours, status=status))
    head = [
        '<!doctype html>\n<html lang="en"><head>\n',
        '<meta charset="utf-8">\n',
        '<meta name="docs-project" content="sample">\n',
        '<meta name="reckon-type" content="plan">\n',
        '<meta name="plan-slug" content="derived">\n',
        '<meta name="plan-title" content="Derived impl">\n',
        '<meta name="plan-status" content="active">\n',
    ]
    if impl is not None:
        head.append(f'<meta name="plan-impl" content="{impl}">\n')
    if declarations is not None:
        escaped = html_module.escape(json.dumps(declarations), quote=True)
        head.append(f'<meta name="plan-section-declarations" content="{escaped}">\n')
    return (
        "".join(head)
        + "</head><body><main>\n"
        + "\n".join(body)
        + "\n</main></body></html>\n"
    )


def _write(tmp_path: Path, slug: str, text: str) -> Path:
    path = tmp_path / "repo" / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── the three sources ───────────────────────────────────────────────────────


def test_records_covering_every_declaration_derive_the_effort_weighted_figure(
    tmp_path: Path,
) -> None:
    text = _plan_text(
        declarations={"s1": "done", "s2": "implementable"},
        records=[("s1", 4, "done"), ("s2", 6, "implementable")],
        impl="0.9",
    )
    path = _write(tmp_path, "covered", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(0.4)
    assert state["impl_source"] == "computed"
    assert parse_plan(path)["impl"] == pytest.approx(0.4)
    assert parse_meta(path)["impl_source"] == "computed"


def test_partial_records_derive_the_impl_by_count_from_the_declarations(
    tmp_path: Path,
) -> None:
    text = _plan_text(
        declarations=PARTIAL_DECLARATIONS,
        records=[("s1", 4, "done")],
        impl="1.0",
    )
    path = _write(tmp_path, "partial", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(0.5)
    assert state["impl_source"] == "declarations"
    assert parse_plan(path)["impl"] == pytest.approx(0.5)
    assert parse_meta(path)["impl"] == pytest.approx(0.5)
    assert parse_meta(path)["impl_source"] == "declarations"
    # The authored meta on disk is untouched by the parse — only the served
    # figure moves.
    assert 'name="plan-impl" content="1.0"' in text


def test_a_plan_without_declarations_keeps_its_authored_figure(tmp_path: Path) -> None:
    text = _plan_text(declarations=None, records=[], impl="0.3")
    path = _write(tmp_path, "authored", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(0.3)
    assert state["impl_source"] == "authored"
    assert parse_plan(path)["impl"] == pytest.approx(0.3)


# ── the audit reads the file's meta against the derived figure ──────────────


def test_audit_doc_reports_impl_meta_stale_when_the_two_disagree(
    tmp_path: Path,
) -> None:
    text = _plan_text(
        declarations=PARTIAL_DECLARATIONS,
        records=[("s1", 4, "done")],
        impl="1.0",
    )
    path = _write(tmp_path, "partial", text)

    codes = {finding.code for finding in doccheck.audit_file(path)}
    assert "impl-meta-stale" in codes


def test_audit_doc_stays_quiet_when_the_meta_and_the_figure_agree(
    tmp_path: Path,
) -> None:
    text = _plan_text(
        declarations={"s1": "done", "s2": "implementable"},
        records=[("s1", 4, "done"), ("s2", 6, "implementable")],
        impl="0.4",
    )
    path = _write(tmp_path, "covered", text)

    codes = {finding.code for finding in doccheck.audit_file(path)}
    assert "impl-meta-stale" not in codes


# ── the schema carries the third value ──────────────────────────────────────


def test_schema_names_declarations_in_the_impl_source_enum() -> None:
    assert "declarations" in IMPL_SOURCE_ENUM
    published = PlanState.model_json_schema()["properties"]["impl_source"]
    assert "declarations" in published["enum"]


def test_schema_accepts_a_state_carrying_each_source() -> None:
    for value in ("computed", "declarations", "authored"):
        assert PlanState.model_validate({"impl_source": value}).impl_source == value


# ── the store still accepts an authored impl on a partial plan ──────────────


def _state(slug: str, sections: list[dict]) -> dict:
    return {
        "project": "sample",
        "type": "plan",
        "slug": slug,
        "title": "Derived impl",
        "status": "active",
        "modified": "2026-10-10",
        "version": 0,
        "impl": 1.0,
        "section_declarations": deepcopy(PARTIAL_DECLARATIONS),
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
            {"id": "next-action", "status": "open", "prompt": "/reckon-build derived"},
        ],
    }


def _record(section_id: str, status: str) -> dict:
    return {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "attempts": 0,
        "status": status,
        "links": [],
    }


def test_an_impl_write_on_the_partial_plan_is_accepted(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    slug = "partial"
    path = checkout / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<title>Derived impl</title></head><body><main class="plan-doc">'
        '<h2 id="s1">First section</h2><p>First body.</p>'
        '<h2 id="s2">Second section</h2><p>Second body.</p>'
        "</main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(authored, _state(slug, [_record("s1", "done")])),
        encoding="utf-8",
    )
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert state["impl_source"] == "declarations", state

    result = mcp_module._edit_plan_tool(
        "sample",
        slug,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[{"op": "set", "path": "impl", "value": 0.25}],
    )

    assert result["ok"] is True, result
    written = _plan_html.read_state(path.read_text(encoding="utf-8"))
    # The authored write is accepted (it is refused only once the records cover
    # the declarations), but the served figure stays the declaration count, so
    # the file's meta and the server disagree and the audit reports it.
    assert written["impl"] == pytest.approx(0.5)
    assert written["impl_source"] == "declarations"
    codes = {finding.code for finding in doccheck.audit_file(path)}
    assert "impl-meta-stale" in codes
