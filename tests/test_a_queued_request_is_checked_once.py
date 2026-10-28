"""A queued request is checked for review once, against its own contract."""

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import (
    plan_review,
    queue_admission,
    recovery,
    review,
    routing,
    runs,
)
from reckon.crew.node import TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

SECTIONS = ("s2", "s3")
ENFORCING = {"plan_review_gate": "enforce"}


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
        '<h2 id="s3">Three</h2><p>Build three.</p>',
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"s2": "implementable", "s3": "implementable"}
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
        for name in SECTIONS
    ]
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": "Reuse the owner.",
        }
    }
    state["gates"] = [
        {"id": "closure", "measure": "the suite is green", "passed": False}
    ]
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


def _store_review(path, *, rubric: str, run_id: str, items, status=None) -> dict:
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
    if status is not None:
        record["status"] = status
    plan_review.store_plan_review(record)
    return record


def _store_content(path, run_id: str, *, status=None) -> dict:
    return _store_review(
        path,
        rubric="content",
        run_id=run_id,
        items=review.PLAN_REVIEW_ITEMS,
        status=status,
    )


def _node(section: str) -> TaskNode:
    return TaskNode(
        id=f"build-{section}",
        goal=f"Build {section}",
        plan="fixture",
        section=section,
        role="implement",
    )


def _pointer(repo, section: str = "s2") -> dict:
    """A queued request record carrying the contract it was queued against."""
    node = _node(section)
    return {
        "run_id": f"r-queued-{section}",
        "project": "sample",
        "plan": "fixture",
        "section": section,
        "repo": str(repo),
        "session": "coordinator",
        "node": node.as_dict(),
        "phase": queue_admission.QUEUED_PHASE,
        "dispatch_options": {},
        "review_contract": queue_admission.review_contract(node, repo_root=repo),
    }


def _owed(pointer) -> str | None:
    return queue_admission._plan_review_owed(
        pointer, config_for=lambda _pointer: ENFORCING
    )


def _fresh_dispatch(repo, section: str = "s2") -> str | None:
    """The dispatch gate's verdict for a fresh dispatch of the same section."""
    from reckon.crew.node import PlanReviewMissingError

    try:
        routing.require_plan_reviewed(
            node=_node(section),
            project="sample",
            repo=repo,
            authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
            enforce=True,
        )
    except PlanReviewMissingError as exc:
        return str(exc)
    return None


def _queued_and_fresh_agree(repo, pointer, section: str = "s2") -> None:
    """The queued request and a fresh dispatch reach the same verdict."""
    queued = _owed(pointer) is not None
    fresh = _fresh_dispatch(repo, section) is not None
    assert queued is fresh


def test_another_sections_edit_leaves_the_request_admissible(two_section_plan):
    """An edit to a section the request does not read never holds it."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content")
    pointer = _pointer(repo, "s2")
    path.write_text(path.read_text().replace("Build three.", "Extend three."))
    assert _owed(pointer) is None
    _queued_and_fresh_agree(repo, pointer)


def test_its_own_sections_wait_change_leaves_the_request_admissible(
    two_section_plan,
):
    """An ordering edit is no request's contract, so it holds nothing."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content")
    pointer = _pointer(repo, "s2")
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_depends_on"] = {"s2": ["fixture#s3"]}
    path.write_text(_plan_html.write_state(text, state))
    assert _owed(pointer) is None
    _queued_and_fresh_agree(repo, pointer)


def test_a_decision_change_holds_it_until_a_covering_review_is_stored(
    two_section_plan,
):
    """An uncovered contract edit holds the request; a review of it releases it."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content")
    pointer = _pointer(repo, "s2")
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["decisions"]["choice"]["rationale"] = "Choose a different owner."
    path.write_text(_plan_html.write_state(text, state))
    detail = _owed(pointer)
    assert detail is not None
    assert plan_review.DOCUMENT_UNIT in detail
    _queued_and_fresh_agree(repo, pointer)
    # A review of the edited plan releases the same request, with no dispatch.
    _store_content(path, "review-content-two")
    assert _owed(pointer) is None


def test_its_own_section_text_change_holds_it_until_a_covering_review(
    two_section_plan,
):
    """Its own section moving uncovered holds the request, and only until review."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content")
    pointer = _pointer(repo, "s2")
    path.write_text(
        path.read_text().replace(
            "Build two.",
            "Build two, and then build two more, at length, in a different way.",
        )
    )
    detail = _owed(pointer)
    assert detail is not None
    assert "s2" in detail
    _queued_and_fresh_agree(repo, pointer)
    _store_content(path, "review-content-two")
    assert _owed(pointer) is None


def test_the_request_records_its_digests_at_queue_time(two_section_plan):
    """Queueing a request stamps the section and contract digests it reads."""
    _home, repo, path = two_section_plan
    node = _node("s2")
    config = {"default_backend": "worker", "local_backend": "worker"}
    stored = queue_admission.queue_requests(
        [node], project="sample", session="coordinator", repo_root=repo, config=config
    )
    run_id = next(iter(stored))
    pointer = runs.read_pointer(run_id)
    contract = pointer["review_contract"]
    digests = plan_review._section_digests(Path(path))
    assert contract["section"] == "s2"
    assert contract["section_digest"] == digests["s2"]
    assert contract["document_digest"] == routing._contract_digest(
        _plan_html.read_state_file(Path(path))
    )


def test_a_summary_edit_holds_a_node(two_section_plan):
    """The plan summary is contract text, so editing it holds a building node."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content")
    pointer = _pointer(repo, "s2")
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["summary"] = "A different summary of the same plan."
    path.write_text(_plan_html.write_state(text, state))
    assert _owed(pointer) is not None


def test_a_gate_edit_holds_a_node(two_section_plan):
    """A gate's measure is contract text, so editing it holds a building node."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content")
    pointer = _pointer(repo, "s2")
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["gates"][0]["measure"] = "the suite is green and the figure reads"
    path.write_text(_plan_html.write_state(text, state))
    assert _owed(pointer) is not None


def test_no_candidate_review_holds_the_node(two_section_plan):
    """A store with no document-unit review cannot show what was read."""
    _, _repo, path = two_section_plan
    _store_content(path, "review-content")
    assert (
        routing._document_change_is_contract("sample", "fixture", plan=path, records=[])
        is True
    )


def test_a_review_with_no_run_id_holds_the_node(two_section_plan):
    """A record naming no review run cannot show the snapshot it read."""
    _, _repo, path = two_section_plan
    record = _store_content(path, "review-content")
    without_run = {
        key: value for key, value in record.items() if key != "review_run_id"
    }
    assert (
        routing._document_change_is_contract(
            "sample", "fixture", plan=path, records=[without_run]
        )
        is True
    )


def test_a_missing_snapshot_holds_the_node(two_section_plan):
    """A review whose snapshot is gone reads as changed."""
    _, _repo, path = two_section_plan
    record = _store_content(path, "review-content")
    snapshot = (
        plan_review.review_report_directory("sample", "fixture", "review-content")
        / "plan.html"
    )
    snapshot.unlink()
    assert (
        routing._document_change_is_contract(
            "sample", "fixture", plan=path, records=[record]
        )
        is True
    )


def test_an_unreadable_snapshot_holds_the_node(two_section_plan):
    """A snapshot that does not parse reads as changed."""
    _, _repo, path = two_section_plan
    record = _store_content(path, "review-content")
    snapshot = (
        plan_review.review_report_directory("sample", "fixture", "review-content")
        / "plan.html"
    )
    snapshot.write_text("not a plan document", encoding="utf-8")
    assert (
        routing._document_change_is_contract(
            "sample", "fixture", plan=path, records=[record]
        )
        is True
    )


def test_a_plan_with_no_stored_review_holds_as_the_gate_does(two_section_plan):
    """With no reviewed text to compare against, the gate's own verdict stands."""
    _, repo, _path = two_section_plan
    pointer = _pointer(repo, "s2")
    detail = _owed(pointer)
    assert detail is not None
    assert _fresh_dispatch(repo, "s2") is not None
    _queued_and_fresh_agree(repo, pointer)


def test_a_plan_whose_only_review_is_incomplete_holds_as_the_gate_does(
    two_section_plan,
):
    """An incomplete review covers nothing, so the request is owed the gate's hold."""
    _, repo, path = two_section_plan
    _store_content(path, "review-content", status=plan_review.INCOMPLETE_STATUS)
    pointer = _pointer(repo, "s2")
    detail = _owed(pointer)
    assert detail is not None
    assert _fresh_dispatch(repo, "s2") is not None
    _queued_and_fresh_agree(repo, pointer)


def test_the_units_a_request_reads_are_defined_once():
    """Admission and the dispatch gate read one definition of the units."""
    assert queue_admission.plan_review_units is routing.plan_review_units
