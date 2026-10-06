"""The plan-review record: keyed by version, joined by content fingerprint.

A plan review is only useful if it can be found again for the content it read.
Version and impl bumps rewrite the plan on every store call, so a review keyed to
the version integer would be orphaned the first time an implementation fraction
moved. These cases hold the record to the fingerprint instead: two versions are
two records, a metadata-only edit keeps the fingerprint so no review is due, a
second review of one version lands beside the first rather than over it, a
declined finding without a reason is refused before anything is written, and a
finding type declined across three distinct plans surfaces while one declined
across two does not.

The boundary is also held against frozen copies of real plans under
`tests/fixtures/plan_review_store/`, not against the live plans. The parser
derives a calibration flag and a diagnostics list while reading the metadata, so
writing the named effort field to a plan that carried only the legacy effort
letter moves state the exclusion set must normalise away; and the parsed state
omits section prose, so a fingerprint over state alone would never notice a
plan's prose being rewritten. Both are held against committed copies, so an edit
to a live plan cannot move the case.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review as module

PROJECT = "fixture-project"
BLOB_A = "a" * 40
BLOB_B = "b" * 40


def _finding(finding_id: str, finding_type: str = "anchor-resolves") -> dict:
    return {"id": finding_id, "type": finding_type, "text": "an advisory finding"}


def _record(
    *,
    slug: str = "demo",
    version: int = 1,
    blob: str = BLOB_A,
    fingerprint: str = "fp",
    findings: list | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": slug,
        "plan_version": version,
        "rubric": "plan_review",
        "reviewed_blob_sha": blob,
        "plan_fingerprint": fingerprint,
        "findings": findings or [],
        "responses": {},
        "status": "ready",
        "review_run_id": "r-plan-review",
    }


def _stored(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_two_versions_of_one_plan_are_two_records(tmp_path: Path) -> None:
    first = module.store_plan_review(
        _record(version=1, blob=BLOB_A, fingerprint="fp-v1"), base_dir=tmp_path
    )
    second = module.store_plan_review(
        _record(version=2, blob=BLOB_B, fingerprint="fp-v2"), base_dir=tmp_path
    )

    assert first.name == "plan-demo.v1.json"
    assert second.name == "plan-demo.v2.json"
    assert first != second
    assert first.is_file() and second.is_file()

    listed = module.list_plan_reviews(PROJECT, base_dir=tmp_path)
    assert {record["plan_version"] for record in listed} == {1, 2}

    # The fingerprint, not the integer, selects the record when the gate asks.
    current = module.read_plan_review(
        PROJECT, "demo", base_dir=tmp_path, plan_fingerprint="fp-v2"
    )
    assert current is not None and current["plan_version"] == 2


def test_metadata_only_edit_keeps_the_fingerprint() -> None:
    base = {
        "slug": "demo",
        "title": "Demo",
        "version": 4,
        "modified": "2026-09-25",
        "impl": 0.1,
        "status": "active",
        "roi": "high",
        "effort_hours": 8.0,
        "owner": "Simon McIntosh",
        "sprint": "S22",
        "tags": ["crew"],
        "archived": "",
        "sections": [{"id": "s1", "body": "the authored prose"}],
        "decisions": [{"key": "k", "chosen": "a"}],
        "followups": [{"id": "f1"}],
        "depends_on": ["another-plan"],
    }
    edited = dict(base)
    edited.update(
        {
            "version": 5,
            "modified": "2026-09-26",
            "impl": 0.4,
            "status": "done",
            "roi": "mid",
            "effort_hours": 9.0,
            "owner": "someone else",
            "sprint": "S23",
            "tags": ["crew", "extra"],
            "archived": "1",
        }
    )
    # Every excluded scalar moved and no content key did: no review is due.
    assert module.plan_fingerprint(base) == module.plan_fingerprint(edited)

    # A content edit does move the fingerprint, so a fresh review is demanded.
    content = dict(base)
    content["decisions"] = [{"key": "k", "chosen": "b"}]
    assert module.plan_fingerprint(content) != module.plan_fingerprint(base)

    # The exclusion set is the named constant, not a scattered literal.
    assert module.PLAN_METADATA_SCALARS == (
        "version",
        "modified",
        "impl",
        "status",
        "roi",
        "effort_hours",
        "owner",
        "sprint",
        "tags",
        "archived",
    )


def test_re_reviewing_one_version_keeps_both_files(tmp_path: Path) -> None:
    first = module.store_plan_review(
        _record(version=1, blob=BLOB_A, fingerprint="fp-a"), base_dir=tmp_path
    )
    second = module.store_plan_review(
        _record(version=1, blob=BLOB_B, fingerprint="fp-b"), base_dir=tmp_path
    )

    assert first.name == "plan-demo.v1.json"
    assert second.name == "plan-demo.v1.at-bbbbbbbb.json"
    assert first.is_file() and second.is_file()

    # Re-storing the same content is idempotent on the plain path: a duplicate
    # write does not mint a sibling for a review that changed nothing.
    again = module.store_plan_review(
        _record(version=1, blob=BLOB_A, fingerprint="fp-a"), base_dir=tmp_path
    )
    assert again == first


def test_blob_keyed_path_is_byte_identical_through_the_shared_helper(
    tmp_path: Path,
) -> None:
    """The store's keying rule is unchanged by the shared ``.at-<sha>`` helper.

    ``plan_review_path`` now builds its sibling through the same helper as
    ``review.review_path``, parameterised by digest length. The plan-review
    path keeps the blob truncated to eight characters, so a record stored
    before that refactor still resolves to the identical path.
    """
    path = module.plan_review_path(
        PROJECT, "demo", 1, base_dir=tmp_path, reviewed_blob_sha=BLOB_B
    )
    assert path == tmp_path / PROJECT / "plan-demo.v1.at-bbbbbbbb.json"


def test_a_response_without_a_reason_is_refused(tmp_path: Path) -> None:
    path = module.store_plan_review(
        _record(findings=[_finding("f1")]), base_dir=tmp_path
    )
    stored = module.read_plan_review(PROJECT, "demo", base_dir=tmp_path)
    assert stored is not None

    with pytest.raises(ValueError, match="reason") as refusal:
        module.record_response(
            stored, "f1", action="declined", reason="   ", base_dir=tmp_path
        )
    assert "reason" in str(refusal.value)

    # The refusal wrote nothing: the stored record still carries no response and
    # the gate still sees the finding unanswered.
    assert _stored(path)["responses"] == {}
    assert module.unanswered_findings(stored) == ["f1"]

    # An action needs no reason, and a decline with a reason both closes it.
    acted = module.record_response(stored, "f1", action="acted", base_dir=tmp_path)
    assert module.unanswered_findings(_stored(acted)) == []

    declined_record = module.record_response(
        stored, "f1", action="declined", reason="house convention", base_dir=tmp_path
    )
    assert _stored(declined_record)["responses"]["f1"]["action"] == "declined"
    assert _stored(declined_record)["responses"]["f1"]["reason"] == "house convention"


def _decline_findings(tmp_path: Path, slug: str, findings: list) -> None:
    """Store a review of one plan and decline each of its findings."""
    module.store_plan_review(_record(slug=slug, findings=findings), base_dir=tmp_path)
    stored = module.read_plan_review(PROJECT, slug, base_dir=tmp_path)
    assert stored is not None
    for finding in findings:
        module.record_response(
            stored,
            finding["id"],
            action="declined",
            reason="the rubric item rests on a convention the reviewer cannot see",
            base_dir=tmp_path,
        )


def test_recurrence_counts_distinct_plans(tmp_path: Path) -> None:
    # p1 carries two declines of one type and must still count once.
    _decline_findings(tmp_path, "p1", [_finding("p1a", "anchor-resolves")])
    _decline_findings(tmp_path, "p2", [_finding("p2a", "anchor-resolves")])
    _decline_findings(tmp_path, "p3", [_finding("p3a", "anchor-resolves")])
    _decline_findings(tmp_path, "q1", [_finding("q1a", "naming")])
    _decline_findings(tmp_path, "q2", [_finding("q2a", "naming")])
    recurrence = module.declined_recurrence(base_dir=tmp_path)
    assert recurrence["anchor-resolves"]["plan_count"] == 3
    assert recurrence["anchor-resolves"]["surfaced"] is True
    assert recurrence["naming"]["plan_count"] == 2
    assert recurrence["naming"]["surfaced"] is False
    assert sorted(recurrence["anchor-resolves"]["plans"]) == [
        "fixture-project/p1",
        "fixture-project/p2",
        "fixture-project/p3",
    ]


# ── The boundary, held against frozen plan copies ───────────────────────────
# Verbatim copies of live plans, so editing a live plan cannot move a case.
# legacy_plan.html predates the named effort field (the parser derives its
# effort from the legacy letter and reports it uncalibrated); current_plan.html
# carries records short of its declarations, so its authored impl stands;
# derived_impl_plan.html carries records covering its declarations, so its impl
# is computed from them.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plan_review_store"
LEGACY_PLAN = FIXTURES / "legacy_plan.html"
CURRENT_PLAN = FIXTURES / "current_plan.html"
DERIVED_PLAN = FIXTURES / "derived_impl_plan.html"

# (parsed-state key, the meta that carries it, a metadata-only replacement).
# `effort_hours` leads: it is the scalar a derived key breaks on, so a control
# that folds that derived key back in fails on the assertion it names rather
# than on some later plan.
_METADATA_EDITS = (
    ("effort_hours", "plan-effort-hours", "123.0"),
    ("version", "plan-version", "99"),
    ("modified", "plan-modified", "2031-01-01"),
    ("impl", "plan-impl", "0.42"),
    ("status", "plan-status", "blocked"),
    ("roi", "plan-roi", "low"),
    ("owner", "plan-owner", "Someone Else"),
    ("sprint", "plan-sprint", "S99"),
    ("tags", "plan-tags", "x,y"),
    ("archived", "plan-archived", "1"),
)

_AUTHORED_EDIT = "</h2>\n<p>an authored edit for the fingerprint check</p>"


def _write_meta(html: str, name: str, value: str) -> str:
    """Return the document with one ``plan-*`` meta set to a new value.

    A plan that never carried the meta gains the meta: writing a metadata scalar
    to a plan is a metadata-only edit whether or not that scalar was present.
    """
    pattern = re.compile(f'<meta name="{re.escape(name)}" content="[^"]*"')
    if pattern.search(html):
        return pattern.sub(f'<meta name="{name}" content="{value}"', html, count=1)
    return html.replace(
        "</head>", f'<meta name="{name}" content="{value}">\n</head>', 1
    )


def test_real_plan_metadata_edits_keep_the_fingerprint_while_prose_moves_it() -> None:
    # The edit table is the named constant, not a hand-copied list.
    assert {key for key, _meta, _value in _METADATA_EDITS} == set(
        module.PLAN_METADATA_SCALARS
    )

    legacy_html = LEGACY_PLAN.read_text(encoding="utf-8")
    legacy_state = _plan_html.read_state(legacy_html)
    # The premise the boundary turns on: this plan has no named effort field, so
    # writing one flips the derived calibration flag.
    assert legacy_state.get("effort")
    assert legacy_state.get("effort_calibrated") is False
    assert "plan-effort-hours" not in legacy_html

    for path in (LEGACY_PLAN, CURRENT_PLAN):
        html = path.read_text(encoding="utf-8")
        base_state = _plan_html.read_state(html)
        base_fingerprint = module.plan_fingerprint(html)
        for key, meta, value in _METADATA_EDITS:
            edited_html = _write_meta(html, meta, value)
            assert edited_html != html, f"{path.name}: the {meta} edit did not apply"
            # The edit must actually have landed, or the stability check below
            # would pass vacuously.
            assert _plan_html.read_state(edited_html).get(key) != base_state.get(key), (
                f"{path.name}: the {meta} edit did not reach the {key} state"
            )
            # ... and the fingerprint must not move: no review is due.
            assert module.plan_fingerprint(edited_html) == base_fingerprint, (
                f"{path.name}: {key} is metadata, so the fingerprint must not move"
            )

        # An authored edit does move it, so a fresh review is demanded.
        authored = html.replace("</h2>", _AUTHORED_EDIT, 1)
        assert authored != html, f"{path.name}: the authored edit did not apply"
        assert module.plan_fingerprint(authored) != base_fingerprint, (
            f"{path.name}: authored prose must move the fingerprint"
        )


def test_a_metadata_edit_leaves_a_derived_impl_and_the_fingerprint_unchanged() -> None:
    html = DERIVED_PLAN.read_text(encoding="utf-8")
    base_state = _plan_html.read_state(html)
    # The premise the case turns on: the records cover the declarations, so the
    # impl derives from them rather than from the meta.
    assert base_state.get("impl_source") == "computed"
    base_fingerprint = module.plan_fingerprint(html)

    written = "0.42"
    edited = _write_meta(html, "plan-impl", written)
    # The edit reached the meta, so the stability below is not vacuous.
    assert f'<meta name="plan-impl" content="{written}"' in edited

    edited_state = _plan_html.read_state(edited)
    assert edited_state.get("impl_source") == "computed"
    assert edited_state.get("impl") == base_state.get("impl")
    assert edited_state.get("impl") != float(written)
    # `impl` is a metadata scalar, so the edit cannot demand a fresh review.
    assert module.plan_fingerprint(edited) == base_fingerprint


# ── The landing collapse, held through the production path ──────────────────
# A landing collapse replaces a section's authored body with a landed card and
# sets its declaration to done. Neither is authored design, so neither may move
# the fingerprint, or every landing beat would demand a fresh review of content
# nobody changed. The collapse path is driven through the MCP plan-editing tool
# the surface calls, against a synthesised checkout, so the case reads no live
# plan.

_COLLAPSE_PROJECT = "sample"
_COLLAPSE_PLAN = "collapse-demo"
_COLLAPSE_DECLARATIONS = {
    "s-landed": "implementable",
    "s-live": "implementable",
}
_COLLAPSE_CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}
# s-landed carries no authored body (its heading is followed straight by the
# next heading), so the collapse under test changes only the card and the
# declaration; s-live keeps an authored body the review must still catch.
_COLLAPSE_AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{_COLLAPSE_PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    "<title>Collapse plan</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="s-landed">&sect;1 &mdash; Landed section</h2>'
    '<h2 id="s-live">&sect;2 &mdash; Live section</h2>'
    "<p>The authored prose a review exists to catch.</p>"
    "</main></body></html>"
)


def _collapse_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / f"{_COLLAPSE_PLAN}.html"
    path.parent.mkdir(parents=True)
    state = {
        "project": _COLLAPSE_PROJECT,
        "type": "plan",
        "slug": _COLLAPSE_PLAN,
        "title": "Collapse plan",
        "status": "active",
        "modified": "2026-10-05",
        "version": 0,
        "section_declarations": dict(_COLLAPSE_DECLARATIONS),
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": dict(_COLLAPSE_CAPABILITY),
                "attempts": 0,
                "status": status,
                "links": [],
            }
            for section_id, status in _COLLAPSE_DECLARATIONS.items()
        ],
    }
    path.write_text(_plan_html.write_state(_COLLAPSE_AUTHORED, state), encoding="utf-8")
    return checkout, path


def _collapse_through_the_op(checkout: Path, section: str, summary: str) -> dict:
    """Collapse a section through the plan-editing tool the surface calls."""
    from reckon import mcp as mcp_module

    path = checkout / "docs" / "plans" / f"{_COLLAPSE_PLAN}.html"
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        _COLLAPSE_PROJECT,
        _COLLAPSE_PLAN,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[
            {
                "op": "collapse_section",
                "section": section,
                "summary": summary,
                "evidence_anchor": f"/{_COLLAPSE_PROJECT}/evidence/archive/x#{section}",
            }
        ],
    )


def test_a_landing_collapse_leaves_the_fingerprint_while_prose_moves_it(
    tmp_path: Path,
) -> None:
    checkout, path = _collapse_checkout(tmp_path)
    before = module.plan_fingerprint(path)

    result = _collapse_through_the_op(checkout, "s-landed", "Built the thing.")
    assert result.get("ok") is True, result
    collapsed_text = path.read_text(encoding="utf-8")
    # The collapse really landed: a card is present and the declaration is done.
    assert 'class="section-landed"' in collapsed_text
    assert (
        _plan_html.read_state(collapsed_text)["section_declarations"]["s-landed"]
        == "done"
    )
    # And it does not move the fingerprint: no review is due for a landing.
    assert module.plan_fingerprint(path) == before, (
        "a landing collapse must not move the fingerprint"
    )

    # An authored edit to the other, still-live section still moves it.
    authored = collapsed_text.replace(
        "The authored prose a review exists to catch.",
        "The authored prose a review exists to catch, now edited.",
        1,
    )
    assert authored != collapsed_text
    assert module.plan_fingerprint(authored) != before, (
        "an authored edit to a live section must move the fingerprint"
    )


def test_a_review_stored_under_the_legacy_digest_still_reads_as_current(
    tmp_path: Path,
) -> None:
    """A definition change must not orphan a review of unchanged content."""
    _checkout, path = _collapse_checkout(tmp_path)
    document = path.read_text(encoding="utf-8")

    current = module.plan_fingerprint(document)
    legacy = module._fingerprint_forms(document)[1]
    # The two definitions disagree, or the equality below would be vacuous.
    assert current != legacy

    # ``_record`` writes under the fixture project, which is where the store
    # keys the file, so the read names the same project.
    module.store_plan_review(
        _record(slug=_COLLAPSE_PLAN, fingerprint=legacy), base_dir=tmp_path
    )
    found = module.read_plan_review(
        PROJECT,
        _COLLAPSE_PLAN,
        base_dir=tmp_path,
        plan_fingerprint={current, legacy},
    )
    assert found is not None, "a legacy-digest review must read as current"
    assert found["plan_fingerprint"] == legacy
