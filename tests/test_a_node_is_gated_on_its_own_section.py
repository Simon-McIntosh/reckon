"""The dispatch gate holds a node only on the plan units that node builds."""

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review, recovery, review, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

SECTIONS = ("s2", "s3", "s4")


def _report(items) -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


@pytest.fixture
def two_section_plan(project):
    """A plan naming two implementable sections and a decision above them."""
    home, repo, path = project
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        "<p>Shared introduction.</p>"
        '<h2 id="s2">Two</h2><p>Build two.</p>'
        '<h2 id="s3">Three</h2><p>Build three.</p>'
        '<h2 id="s4">Four</h2><p>Build four.</p>',
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = {
        "s2": "implementable",
        "s3": "implementable",
        "s4": "deferred",
    }
    state["sections"] = [
        {
            "id": name,
            "effort_hours": 1.0,
            "status": state["section_declarations"][name],
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
        for name in SECTIONS
    ]
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": "Reuse the owner.",
        }
    }
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, run_id: str):
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


def _store_review(path, *, rubric: str, run_id: str, items) -> dict:
    """Compose and store one review of the plan as it stands now."""
    fields = recovery._review_dispatch_fields(_subject(path, run_id), rubric=rubric)
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(_report(items))
    record = {
        **sidecar,
        "findings": [],
        "responses": {},
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record)
    return record


def _reviewed(path) -> None:
    """One design review and one content review of the unchanged plan."""
    _store_review(
        path,
        rubric="design",
        run_id="review-design",
        items=review.PLAN_DESIGN_REVIEW_ITEMS,
    )
    _store_review(
        path,
        rubric="content",
        run_id="review-content",
        items=review.PLAN_REVIEW_ITEMS,
    )


def _gate(repo, section: str):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id=f"build-{section}",
            goal=f"Build {section}",
            plan="fixture",
            section=section,
            role="implement",
        ),
        project="sample",
        repo=repo,
        authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        enforce=True,
    )


def test_a_covered_section_builds_while_its_neighbour_owes_a_review(
    two_section_plan,
):
    """A node building a covered, unchanged section is admitted."""
    _, repo, path = two_section_plan
    _reviewed(path)
    path.write_text(path.read_text().replace("Build three.", "Extend three."))
    assert _gate(repo, "s2") is None
    with pytest.raises(PlanReviewMissingError, match="uncovered units: s3;"):
        _gate(repo, "s3")


def test_a_refusal_names_every_uncovered_unit(two_section_plan):
    """The refusal carries the whole debt, not only the units the node reads."""
    _, repo, path = two_section_plan
    _reviewed(path)
    text = path.read_text().replace("Build three.", "Extend three.")
    state = _plan_html.read_state(text)
    # A section the review read as deferred, declared implementable afterwards:
    # it is covered by nothing, and the node reading s3 is owed its number.
    state["section_declarations"]["s4"] = "implementable"
    for record in state["sections"]:
        if record["id"] == "s4":
            record["status"] = "implementable"
    path.write_text(_plan_html.write_state(text, state))
    with pytest.raises(PlanReviewMissingError, match="uncovered units: s3, s4;"):
        _gate(repo, "s3")


def test_a_document_edit_outside_the_contract_does_not_gate(two_section_plan):
    """Remainder prose is no node's contract, so editing it holds no node."""
    _, repo, path = two_section_plan
    _reviewed(path)
    path.write_text(
        path.read_text().replace(
            "Shared introduction.", "A longer shared introduction."
        )
    )
    _records, uncovered, _changes = plan_review.review_coverage(
        "sample", "fixture", plan=path
    )
    assert uncovered == {plan_review.DOCUMENT_UNIT}
    assert _gate(repo, "s2") is None


def test_a_changed_decision_still_gates_a_covered_section(two_section_plan):
    """The document contract changed, so the section that reads it is held."""
    _, repo, path = two_section_plan
    _reviewed(path)
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["decisions"]["choice"]["rationale"] = "Choose a different owner."
    path.write_text(_plan_html.write_state(text, state))
    _records, uncovered, _changes = plan_review.review_coverage(
        "sample", "fixture", plan=path
    )
    assert uncovered == {plan_review.DOCUMENT_UNIT}
    with pytest.raises(PlanReviewMissingError, match="uncovered units: _document;"):
        _gate(repo, "s2")
