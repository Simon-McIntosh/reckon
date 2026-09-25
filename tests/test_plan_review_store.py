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

The boundary is also held against the real corpus rather than a fixture, because
a fixture cannot fail the way a legacy plan does: the parser derives a
calibration flag and a diagnostics list while reading the metadata, so writing
the named effort field to a plan that carried only the legacy effort letter
moves state the exclusion set must normalise away. And the parsed state omits
section prose, so a fingerprint over state alone would never notice a plan's
prose being rewritten. Both are checked against `docs/plans`.
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


# ── The boundary, held against the real corpus ──────────────────────────────
PLANS_DIR = Path(__file__).resolve().parents[1] / "docs" / "plans"
# A plan written before the named effort field existed, so it carries the legacy
# letter and the parser must derive an uncalibrated effort from it.
LEGACY_PLAN = PLANS_DIR / "reckon-mcp-plan.html"
CURRENT_PLAN = PLANS_DIR / "a-plan-is-reviewed-before-it-is-built.html"

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
