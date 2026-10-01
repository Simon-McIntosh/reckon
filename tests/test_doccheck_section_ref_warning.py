"""Tests for ``plan-ref-could-be-section-ref``: a section wait wired whole-plan.

A plan-level ``depends_on`` holds its carrier from the first implementable
section onward, because the entry itself names no section. When one of the
plan's own sections declares the same target at section level — in the typed
section record's ``links`` or in the ``plan-section-depends-on`` mapping — the
wait is section-shaped and the plan-level entry re-holds every sibling the
section ref leaves dispatchable. The check reports that entry, and stays silent
on a wait the plan's first implementable section really does begin.

Every fixture is a pair of plans: the consumer under audit and the target its
wire names, both written into a synthetic docs tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import doccheck as doccheck_module
from reckon.doccheck import audit_html

CODE = "plan-ref-could-be-section-ref"


def _attr_json(value: object) -> str:
    """A JSON value escaped for a double-quoted HTML attribute."""

    return json.dumps(value).replace('"', "&quot;")


def _section_record(section_id: str, links: str) -> str:
    return (
        f'<h2 id="{section_id}" data-reckon="section" data-effort-hours="2"'
        ' data-capability-version="1.0" data-capability-class="general"'
        ' data-capability-reasoning="standard"'
        ' data-capability-verification="standard" data-capability-risk="low"'
        f' data-links="{links}" data-attempts="0" data-status="implementable">'
        f"{section_id}</h2>"
    )


def _document(
    slug: str,
    *,
    status: str = "active",
    depends_on: str = "",
    sections: list[tuple[str, str]] = (),
    bare: tuple[str, ...] = (),
    mapping: dict[str, list[str]] | None = None,
    declared: bool = True,
) -> str:
    declarations = {section_id: "implementable" for section_id, _ in sections}
    metas = [
        ("plan-slug", slug),
        ("plan-status", status),
        ("plan-modified", "2026-10-01"),
    ]
    if sections:
        metas.append(("plan-section-declarations", _attr_json(declarations)))
    if declared:
        metas.append(("reckon-type", "plan"))
    if depends_on:
        metas.append(("plan-depends-on", depends_on))
    if mapping is not None:
        metas.append(("plan-section-depends-on", _attr_json(mapping)))
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    body = "".join(
        f'<h2 id="{section_id}">{section_id}</h2>'
        if section_id in bare
        else _section_record(section_id, links)
        for section_id, links in sections
    )
    return (
        f'<!doctype html><html lang="en"><head>{head}<title>{slug}</title></head>'
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )


def _pair(
    tmp_path: Path,
    *,
    sections: list[tuple[str, str]] = (("s1", ""), ("s3", "")),
    **consumer: object,
) -> str:
    """Write the consumer and its declared target, and return the consumer's text.

    The target is a real document in the same plans directory, so the wire the
    finding reads names a plan that exists rather than a slug in prose.
    """

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    consumer_html = _document(
        "consumer", depends_on="provider", sections=list(sections), **consumer
    )
    (plans / "provider.html").write_text(
        _document("provider", status="active"), encoding="utf-8"
    )
    (plans / "consumer.html").write_text(consumer_html, encoding="utf-8")
    return consumer_html


def _of_code(findings, code: str):
    return [f for f in findings if f.code == code]


def _others(findings):
    """Every finding that is not the one under test, in audit order."""

    return [(f.severity, f.code) for f in findings if f.code != CODE]


# ── the pair that should use a section ref ──────────────────────────────────


def test_the_declared_code_is_the_one_the_module_raises():
    assert doccheck_module._PLAN_REF_COULD_BE_SECTION_REF == CODE


def test_a_section_wait_wired_whole_plan_is_reported(tmp_path: Path):
    html = _pair(tmp_path, sections=[("s1", ""), ("s3", "provider")])

    (finding,) = _of_code(audit_html(html), CODE)

    assert finding.severity == "warn"
    assert "'consumer'" in finding.message
    assert "'provider'" in finding.message
    assert "'s3'" in finding.message
    assert "'s1'" in finding.message


def test_the_section_scoped_mapping_is_read_beside_the_section_records(
    tmp_path: Path,
):
    html = _pair(
        tmp_path, sections=[("s1", ""), ("s3", "")], mapping={"s3": ["provider"]}
    )

    (finding,) = _of_code(audit_html(html), CODE)

    assert finding.severity == "warn"
    assert "'s3'" in finding.message


def test_every_declaring_section_is_named(tmp_path: Path):
    html = _pair(
        tmp_path, sections=[("s1", ""), ("s3", "provider"), ("s4", "provider")]
    )

    (finding,) = _of_code(audit_html(html), CODE)

    assert "'s3'" in finding.message
    assert "'s4'" in finding.message


# ── the pair that should not ────────────────────────────────────────────────


def test_a_wait_the_first_implementable_section_declares_stays_plan_level(
    tmp_path: Path,
):
    """A wait the first section begins IS a whole-plan prerequisite."""

    html = _pair(tmp_path, sections=[("s1", "provider"), ("s3", "")])

    assert _of_code(audit_html(html), CODE) == []


def test_a_wait_no_section_declares_stays_plan_level(tmp_path: Path):
    html = _pair(tmp_path, sections=[("s1", ""), ("s3", "")])

    assert _of_code(audit_html(html), CODE) == []


def test_a_section_ref_to_another_target_leaves_the_wire_alone(tmp_path: Path):
    html = _pair(tmp_path, sections=[("s1", ""), ("s3", "elsewhere")])

    assert _of_code(audit_html(html), CODE) == []


def test_a_terminal_plan_is_not_reported(tmp_path: Path):
    html = _pair(tmp_path, status="shipped", sections=[("s1", ""), ("s3", "provider")])

    assert _of_code(audit_html(html), CODE) == []


def test_an_undeclared_document_is_not_held_to_the_plan_rule(tmp_path: Path):
    html = _pair(tmp_path, declared=False, sections=[("s1", ""), ("s3", "provider")])

    assert _of_code(audit_html(html), CODE) == []


# ── isolation: no other finding moves when the check fires ──────────────────


def test_no_other_audit_finding_changes_for_the_fixtures(tmp_path: Path):
    """The two arms differ only in the section ref the new finding reads.

    Both carry an implementable section with no typed record, so the
    comparison holds against a plan that produces other findings rather than
    against an empty list.
    """

    positive = _pair(
        tmp_path, sections=[("s1", ""), ("s3", "provider"), ("s5", "")], bare=("s5",)
    )
    negative = _pair(
        tmp_path, sections=[("s1", ""), ("s3", ""), ("s5", "")], bare=("s5",)
    )

    findings = audit_html(positive)

    assert _of_code(findings, "section-without-contract")
    assert _of_code(findings, CODE)
    assert _others(findings) == _others(audit_html(negative))


def test_the_finding_is_beside_the_wiring_findings_not_instead_of_them(
    tmp_path: Path,
):
    html = _pair(tmp_path, sections=[("s1", ""), ("s3", "provider")])

    findings = audit_html(html)

    assert [f.code for f in findings].count(CODE) == 1
    assert _of_code(findings, "unwired-plan") == []


@pytest.mark.parametrize("bad_mapping", ["{not json}", "[]", ""])
def test_a_malformed_section_mapping_is_not_read_as_a_section_ref(
    tmp_path: Path, bad_mapping: str
):
    """A mapping no reader can use declares no section, so the wire is untouched."""

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    html = _document(
        "consumer",
        depends_on="provider",
        sections=[("s1", ""), ("s3", "")],
    ).replace(
        "</head>",
        f'<meta name="plan-section-depends-on" content="{bad_mapping}"></head>',
    )

    assert _of_code(audit_html(html), CODE) == []
