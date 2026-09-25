"""The audit's section-contract findings — specimens the test builds itself.

A section is the unit that carries a stated effort, a capability and a place in
the roadmap. Two authored shapes leave work outside it, and the audit reports
both: an implementable section with no typed record (at ``error`` once the plan
carries a record to compare against, at ``warn`` while the plan still predates
the contract) and an open followup whose body states section-sized work.

Every specimen is synthesised into a temporary plan file rather than read from a
live document: a check asserted against a live plan is a fuse, because the day
that plan is legitimately converted the test turns red and reads as an audit
regression. The clean-plan case is here for the other direction — a rule that
cannot stay silent on a plan which does carry its records would make the audit
unusable on the plans that adopted the contract first.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon._store import _section_contract_refusal
from reckon.doccheck import Finding, audit_file

_RECORD = (
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="1.25"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard" data-capability-verification="strict"'
    ' data-capability-risk="low" data-attempts="0"'
    ' data-status="{status}" data-links=""></section>'
)


def _plan(body: str, *, declarations: dict[str, str] | None = None) -> str:
    meta = ""
    if declarations is not None:
        meta = (
            '<meta name="plan-section-declarations" content=\''
            + json.dumps(declarations)
            + "'>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="section-contract">'
        '<meta name="plan-status" content="active">'
        + meta
        + "<title>section contract</title></head><body>"
        + f'<main class="plan-doc">{body}</main></body></html>'
    )


def _section(sid: str, *, title: str = "work") -> str:
    # Only the heading: a typed record is valid directly beside its h2, so a
    # paragraph wedged between them is a differently-shaped defect.
    return f'<h2 id="{sid}">§{sid[1:]} — {title}</h2>'


def _record(sid: str, status: str = "implementable") -> str:
    return _RECORD.format(sid=sid, status=status)


def _followup(fid: str, body: str, *, status: str = "open") -> str:
    return (
        '<section data-reckon="followups" class="r-followups">'
        '<h2><span class="sec">§</span> Followups</h2>'
        f'<article class="r-fu" data-id="{fid}" data-status="{status}"'
        ' data-written-by="coordinator" data-written-at="2026-09-25">'
        f'<h4 class="r-fu-title">{fid}</h4><div class="r-fu-body">{body}</div>'
        '<pre class="r-fu-prompt">/reckon-build section-contract §2</pre>'
        "</article></section>"
    )


def _audit(tmp_path: Path, doc: str) -> list[Finding]:
    path = tmp_path / "plan.html"
    path.write_text(doc, encoding="utf-8")
    return audit_file(path)


def _with_code(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


# ─ a record-carrying plan holds the contract ────────────────────────────────


def test_a_record_carrying_plan_reports_an_unrecorded_section_as_an_error(
    tmp_path: Path,
) -> None:
    doc = _plan(
        _section("s1") + _record("s1") + _section("s2"),
        declarations={"s1": "implementable", "s2": "implementable"},
    )
    found = _with_code(_audit(tmp_path, doc), "section-without-contract")
    assert [f.severity for f in found] == ["error"]
    assert len(found) == 1, "the section that carries its record is not reported"
    assert "'s2'" in found[0].message


def test_the_finding_carries_the_write_boundary_example(tmp_path: Path) -> None:
    doc = _plan(
        _section("s1") + _record("s1") + _section("s2"),
        declarations={"s1": "implementable", "s2": "implementable"},
    )
    found = _with_code(_audit(tmp_path, doc), "section-without-contract")
    _, _, carried = found[0].message.partition("Example: ")
    assert carried, "the finding carries no worked example for the author to copy"
    # Imported, not copied: the example a finding carries is byte-for-byte the
    # one the write boundary refuses with, so the two cannot drift apart.
    _, _, refused = _section_contract_refusal("x").partition("Example: ")
    assert carried == refused
    assert json.loads(carried)["target"] == "sections"


# ─ a plan that predates the contract warns rather than stops the fleet ──────


def test_a_plan_with_no_typed_record_reports_the_gap_as_a_warning(
    tmp_path: Path,
) -> None:
    doc = _plan(
        _section("s1") + _section("s2"),
        declarations={"s1": "implementable", "s2": "implementable"},
    )
    found = _with_code(_audit(tmp_path, doc), "section-without-contract")
    assert [f.severity for f in found] == ["warn", "warn"]
    assert sorted(f.message.split()[1] for f in found) == ["'s1'", "'s2'"]


def test_a_plan_declaring_no_implementable_section_is_silent(tmp_path: Path) -> None:
    doc = _plan(_section("s1"), declarations={"s1": "done"})
    assert _with_code(_audit(tmp_path, doc), "section-without-contract") == []


# ─ a followup that states section-sized work ────────────────────────────────


def test_a_followup_stating_section_sized_work_is_a_warning(tmp_path: Path) -> None:
    doc = _plan(
        _section("s1")
        + _record("s1")
        + _followup(
            "f-sc-001",
            "<p>(1) Add the audit rule to the reader. "
            "(2) Wire the same predicate into the CLI.</p>",
        ),
        declarations={"s1": "implementable"},
    )
    found = _with_code(_audit(tmp_path, doc), "followup-describes-section-work")
    assert [f.severity for f in found] == ["warn"]
    assert "'f-sc-001'" in found[0].message


def test_a_followup_with_one_deliverable_is_still_a_remainder(tmp_path: Path) -> None:
    doc = _plan(
        _section("s1")
        + _record("s1")
        + _followup("f-sc-001", "<p>(1) Repair the reader's stale threshold.</p>"),
        declarations={"s1": "implementable"},
    )
    assert _with_code(_audit(tmp_path, doc), "followup-describes-section-work") == []


def test_a_resolved_followup_is_not_reported(tmp_path: Path) -> None:
    doc = _plan(
        _section("s1")
        + _record("s1")
        + _followup(
            "f-sc-001",
            "<p>(1) Add the rule. (2) Set the threshold.</p>",
            status="resolved",
        ),
        declarations={"s1": "implementable"},
    )
    assert _with_code(_audit(tmp_path, doc), "followup-describes-section-work") == []


# ─ a plan that carries the contract is clean ────────────────────────────────


def test_a_contract_carrying_plan_is_silent(tmp_path: Path) -> None:
    doc = _plan(
        _section("s1")
        + _record("s1")
        + _section("s2")
        + _record("s2", "done")
        + _followup("f-sc-001", "<p>(1) One remainder, not a section.</p>"),
        declarations={"s1": "implementable", "s2": "done"},
    )
    findings = _audit(tmp_path, doc)
    assert _with_code(findings, "section-without-contract") == []
    assert _with_code(findings, "followup-describes-section-work") == []
    assert "empty-section" not in [f.code for f in findings], (
        "a typed section record is metadata, not an empty reckon section"
    )


# ─ a malformed record is reported, never fatal ──────────────────────────────


def test_a_partial_record_is_reported_and_the_audit_continues(tmp_path: Path) -> None:
    doc = _plan(
        _section("s1")
        + '<section data-reckon="section" data-id="s1" data-effort-hours="1.25">'
        "</section>" + '<img src="figures/x.svg">',
        declarations={"s1": "implementable"},
    )
    findings = _audit(tmp_path, doc)
    reported = _with_code(findings, "section-record-invalid")
    assert [f.severity for f in reported] == ["error"]
    # The checks after the state read ran: a relative image src is one of them,
    # and it is reachable only if the malformed record did not end the audit.
    assert _with_code(findings, "img-relative-src") != []
