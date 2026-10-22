"""A bulleted finding is read, and a review missing a rubric item covers nothing.

A plan review's report is Markdown, so its emitted lines may carry a list
marker. An anchored line reading dropped ``- FINDING anchors_resolve …``
entirely: the finding was never stored, an answer naming it was refused as
"not on this review", and the finding reached the plan only because a
coordinator compared the report with the record by hand.

The second half is the record that defect left behind. A delivered report that
leaves out an item its own brief listed is an unfinished review, and it is
marked incomplete when it is stored; the coverage predicate skips it, so the
units it would have covered stay uncovered and the dispatch gate names them
rather than counting a partial review as cover. Records stored before the mark
existed carry no mark, so an older review that merely lists an absent item still
covers what it read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, cli
from reckon.crew import plan_review, recovery, review, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

CONTENT_ITEMS = review.PLAN_REVIEW_ITEMS
DESIGN_ITEMS = review.PLAN_DESIGN_REVIEW_ITEMS


@pytest.fixture
def section_plan(project):
    """A two-section plan, so one authored edit uncovers exactly one unit."""
    home, repo, path = project
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        '<p>Shared introduction.</p><h2 id="a">Alpha</h2><p>Build alpha.</p>'
        '<h2 id="b">Beta</h2><p>Build beta.</p>',
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"a": "implementable", "b": "implementable"}
    state["sections"] = [
        {
            "id": name,
            "effort_hours": 1.0,
            "status": "implementable",
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
            "links": [],
        }
        for name in ("a", "b")
    ]
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, *, run_id, rubric):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": rubric,
        "local": True,
        "run_id": run_id,
    }


def _deliver(path, *, run_id, rubric, report):
    """Write a delivered report and sidecar, and let the store read them."""
    fields = recovery._review_dispatch_fields(
        _subject(path, run_id=run_id, rubric=rubric)
    )
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(report)
    plan_review.store_delivered_reviews("sample", "fixture")
    return sidecar


def _rubric_lines(items):
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


def _stored(run_id):
    record = plan_review.read_plan_review("sample", "fixture")
    assert record is not None
    assert record["review_run_id"] == run_id
    return record


def _coverage(path):
    records, uncovered, _changes = plan_review.review_coverage(
        "sample", "fixture", plan=path
    )
    return records, uncovered


def _gate(repo):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id="build-beta",
            goal="Build beta",
            plan="fixture",
            section="b",
            role="implement",
        ),
        project="sample",
        repo=repo,
        authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        enforce=True,
    )


def test_a_bulleted_finding_is_stored_and_answerable(section_plan):
    _, _repo, path = section_plan
    report = _rubric_lines(CONTENT_ITEMS) + (
        "- FINDING anchors_resolve docs/plans/fixture.html:4 — the anchor names "
        "no heading — WOULD_CHANGE_THE_PLAN: yes — REASON: the done-when reads on a "
        "unit that does not exist\n"
    )
    _deliver(
        path,
        run_id="content-bulleted",
        rubric=plan_review.CONTENT_RUBRIC,
        report=report,
    )
    stored = _stored("content-bulleted")
    assert [finding["id"] for finding in stored["findings"]] == ["anchors_resolve-1"]
    assert stored["findings"][0]["would_change"] is True

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "anchors_resolve-1",
            "--acted",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["unanswered"] == []


def test_a_report_missing_a_brief_item_covers_nothing(section_plan):
    _, repo, path = section_plan
    _deliver(
        path,
        run_id="design-complete",
        rubric=plan_review.DESIGN_RUBRIC,
        report=_rubric_lines(DESIGN_ITEMS),
    )
    design = _stored("design-complete")
    assert design["status"] == "ready"
    assert _coverage(path)[1] == set()

    path.write_text(path.read_text().replace("Build beta.", "Extend beta."))
    missing = [item for item in CONTENT_ITEMS if item != "anchors_resolve"]
    _deliver(
        path,
        run_id="content-partial",
        rubric=plan_review.CONTENT_RUBRIC,
        report=_rubric_lines(missing),
    )
    partial = _stored("content-partial")
    assert partial["status"] == plan_review.INCOMPLETE_STATUS
    assert partial["absent_items"] == ["anchors_resolve"]

    records, uncovered = _coverage(path)
    assert uncovered == {"b"}
    assert all(record["review_run_id"] != "content-partial" for record in records)
    with pytest.raises(PlanReviewMissingError) as refusal:
        _gate(repo)
    assert "uncovered units: b;" in str(refusal.value)
    assert "design rubric" not in str(refusal.value)


def test_an_unmarked_older_review_keeps_its_coverage(section_plan):
    _, _repo, path = section_plan
    _deliver(
        path,
        run_id="content-partial",
        rubric=plan_review.CONTENT_RUBRIC,
        report=_rubric_lines(CONTENT_ITEMS),
    )
    # The record a store of the time would have written: every item answered but
    # one, with no mark on the status, which the store fills with ``ready``.
    record = _stored("content-partial")
    record["absent_items"] = ["anchors_resolve"]
    record.pop("status")
    plan_review.store_plan_review(record)
    stored = _stored("content-partial")
    assert stored["status"] == "ready"

    records, uncovered = _coverage(path)
    assert uncovered == set()
    assert [item["review_run_id"] for item in records] == ["content-partial"]


def test_a_complete_report_stores_as_ready(section_plan):
    _, _repo, path = section_plan
    _deliver(
        path,
        run_id="content-complete",
        rubric=plan_review.CONTENT_RUBRIC,
        report=_rubric_lines(CONTENT_ITEMS),
    )
    stored = _stored("content-complete")
    assert stored["status"] == "ready"
    assert stored["absent_items"] == []
    assert sorted(stored["rubric_items"]) == sorted(CONTENT_ITEMS)
