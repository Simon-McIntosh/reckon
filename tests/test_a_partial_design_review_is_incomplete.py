"""A design review that leaves out one of its brief's items covers nothing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import plan_review, recovery, review, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_a_section_reviewed_as_authored_stays_reviewed import (
    section_plan as section_plan,  # noqa: PLC0414
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414

_DESIGN_ITEMS = review.PLAN_DESIGN_REVIEW_ITEMS


def _subject(path, *, run_id="partial-design-review"):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": "design",
        "local": True,
        "run_id": run_id,
    }


def _deliver(path, report, *, run_id="partial-design-review"):
    """Compose an unstored delivered report and return its sidecar."""
    fields = recovery._review_dispatch_fields(_subject(path, run_id=run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(report, encoding="utf-8")
    return sidecar


def _rubric_lines(items):
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


def _design_record():
    records = [
        record
        for record in plan_review.list_plan_reviews(project="sample")
        if record.get("rubric") == "design"
    ]
    assert records, "a design review was stored"
    return max(records, key=lambda record: int(record.get("plan_version") or 0))


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


def test_a_partial_design_review_is_incomplete(section_plan):
    _, repo, path = section_plan
    missing = "interface_budget"
    _deliver(path, _rubric_lines(item for item in _DESIGN_ITEMS if item != missing))

    with pytest.raises(PlanReviewMissingError) as raised:
        _gate(repo)

    message = str(raised.value)
    assert "left out an item of its rubric" in message
    assert (
        plan_review.review_invocation(
            "sample", "fixture", rubric=plan_review.DESIGN_RUBRIC
        )
        in message
    )
    record = _design_record()
    assert record["status"] == plan_review.INCOMPLETE_STATUS
    assert record["absent_items"] == [missing]


def test_a_complete_design_review_admits_the_dispatch(section_plan):
    _, repo, path = section_plan
    _deliver(path, _rubric_lines(_DESIGN_ITEMS))

    assert _gate(repo) is None
    record = _design_record()
    assert record["status"] == plan_review.DEFAULT_STATUS
    assert record["absent_items"] == []


def test_a_stored_design_review_without_the_mark_still_counts(section_plan):
    _, repo, path = section_plan
    sidecar = _deliver(path, _rubric_lines(_DESIGN_ITEMS))
    plan_review.store_plan_review(
        {
            **sidecar,
            "findings": [],
            "responses": {},
            "rubric_items": {
                item: "pass" for item in _DESIGN_ITEMS if item != "interface_budget"
            },
            "absent_items": ["interface_budget"],
            "review_run_id": "review-before-the-mark",
        }
    )

    assert _gate(repo) is None
    record = _design_record()
    assert record["status"] == plan_review.DEFAULT_STATUS
    assert record["absent_items"] == ["interface_budget"]


def test_an_incomplete_design_reviews_finding_is_still_answerable(section_plan):
    _, _, path = section_plan
    report = _rubric_lines(
        item for item in _DESIGN_ITEMS if item != "interface_budget"
    ) + (
        "FINDING reuse_search plan#b — Name the owner of the composition. "
        "— WOULD_CHANGE_THE_PLAN: yes — REASON: the core already exists\n"
    )
    _deliver(path, report)

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
            "reuse_search-1",
            "--acted",
        ],
    )
    assert result.exit_code == 0, result.output
    record = _design_record()
    assert record["status"] == plan_review.INCOMPLETE_STATUS
    assert record["responses"]["reuse_search-1"]["action"] == "acted"
