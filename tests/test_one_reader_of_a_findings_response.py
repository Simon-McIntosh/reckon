"""One reader of a finding's stored response.

A finding's answer is stored once, keyed by the finding's id in a review
record's ``responses`` map, and read by three callers: ``declined_recurrence``
and the recurrence fold's ``_acted_finding_types`` compare the response's
``action``, and the re-review ``review_scope`` shows its ``action`` and
``reason``. These cases patch the one accessor all three call and hold each
reader to the patched answer. A reader that looks the response up inline again
cannot see it, because the record's stored ``responses`` map is empty, so the
case fails.

Every fixture is synthesised: a temporary store directory and an in-memory
record. No real review record is read or written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import plan_review

PROJECT = "one-reader"
SLUG = "demo"
FINDING_ID = "f1"
FINDING_TYPE = "duplicate_owner"
SENTINEL_REASON = "SENTINEL-REASON"


def _finding(finding_id: str = FINDING_ID, finding_type: str = FINDING_TYPE) -> dict:
    return {"id": finding_id, "type": finding_type, "text": "the same owner twice"}


def _record(findings: list) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": SLUG,
        "plan_version": 1,
        "rubric": "content",
        "findings": findings,
        "responses": {},
        "status": "ready",
        "review_run_id": "r-one-reader",
    }


def _patch_accessor(monkeypatch: pytest.MonkeyPatch, action: str) -> dict[str, str]:
    """Answer every finding with a sentinel response, never the empty stored map."""
    sentinel = {"action": action, "reason": SENTINEL_REASON}
    monkeypatch.setattr(
        plan_review, "_stored_response", lambda record, finding: dict(sentinel)
    )
    return sentinel


def test_only_the_accessor_looks_a_finding_response_up_by_id() -> None:
    # The lookup lives in exactly one function; each reader calls it by name.
    source = Path(plan_review.__file__).read_text(encoding="utf-8")
    assert source.count("responses.get(") == 1
    assert "response = _stored_response(record, finding)" in source


def test_declined_recurrence_counts_through_the_accessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_review.store_plan_review(_record([_finding()]), base_dir=tmp_path)
    _patch_accessor(monkeypatch, "declined")

    recurrence = plan_review.declined_recurrence(base_dir=tmp_path, threshold=1)

    # Only reachable through the accessor: the stored responses map is empty.
    assert recurrence[FINDING_TYPE]["plan_count"] == 1
    assert recurrence[FINDING_TYPE]["surfaced"] is True
    assert recurrence[FINDING_TYPE]["plans"] == [f"{PROJECT}/{SLUG}"]


def test_the_recurrence_fold_reads_acted_through_the_accessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record([_finding()])
    _patch_accessor(monkeypatch, "acted")

    # The fold reads the same accessor, so the type counts as acted.
    assert plan_review._acted_finding_types(record) == {FINDING_TYPE}


def test_review_scope_shows_the_accessors_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record([_finding()])
    _patch_accessor(monkeypatch, "acted")
    monkeypatch.setattr(plan_review, "list_plan_reviews", lambda *a, **k: [record])
    monkeypatch.setattr(
        plan_review, "review_coverage", lambda *a, **k: ([record], {"s7"}, {})
    )

    scope = plan_review.review_scope(
        PROJECT, SLUG, plan="<html/>", rubric=plan_review.CONTENT_RUBRIC
    )

    # The reason the accessor carries is what the scope shows.
    assert SENTINEL_REASON in scope
