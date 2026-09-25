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
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
