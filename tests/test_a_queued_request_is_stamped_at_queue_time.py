"""A queued request is stamped with its review contract when the queue writes it.

The contract is the section's text and the document text the request reads as
its own (the summary, decisions and gates), both digested from the plan as the
dispatch left it. Admission compares the plan against those digests instead of
re-running the whole dispatch gate, so an edit to another section, an ordering
or bookkeeping edit and a contract change already reviewed never hold a request
that was queued against reviewed text.
"""

from __future__ import annotations

import importlib
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
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

SECTIONS = ("s2", "s3")
ENFORCING = {"plan_review_gate": "enforce"}


def _report(items) -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


@pytest.fixture
def two_section_plan(project):
    """A plan naming two implementable sections and a decision beside them."""
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


def _store_review(path, *, rubric: str, run_id: str, items) -> dict:
    """Compose and store one review of the plan as it stands now."""
    subject = {
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
    fields = recovery._review_dispatch_fields(subject, rubric=rubric)
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(_report(items))
    record = {**sidecar, "findings": [], "responses": {}, "review_run_id": run_id}
    plan_review.store_plan_review(record)
    return record


def _store_content(path, run_id: str) -> dict:
    return _store_review(
        path, rubric="content", run_id=run_id, items=review.PLAN_REVIEW_ITEMS
    )


def _store_design(path, run_id: str) -> dict:
    return _store_review(
        path, rubric="design", run_id=run_id, items=review.PLAN_DESIGN_REVIEW_ITEMS
    )


def _reviewed(path) -> None:
    """A plan whose design and content are both reviewed as it stands."""
    _store_design(path, "review-design")
    _store_content(path, "review-content")


def _node(section: str = "s2") -> TaskNode:
    return TaskNode(
        id=f"build-{section}",
        goal=f"Build {section}",
        plan="fixture",
        section=section,
        role="implement",
    )


def _queue(repo, section: str = "s2") -> dict:
    """Queue one request through the production dispatch path, then read it."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    stored = dispatch_module.queue_requests(
        [_node(section)],
        project="sample",
        session="coordinator",
        repo_root=repo,
        config={"default_backend": "worker", "local_backend": "worker"},
    )
    run_id = next(iter(stored))
    return runs.read_pointer(run_id)


def _fresh_pointer(repo, section: str = "s2") -> dict:
    """The contract a fresh dispatch of the same section would be stamped with."""
    node = _node(section)
    return {
        "run_id": f"r-fresh-{section}",
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


def _dispatch_verdict(repo, section: str = "s2") -> str | None:
    """The dispatch gate's own verdict for a fresh dispatch of the section."""
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
    assert (_owed(pointer) is not None) is (
        _dispatch_verdict(repo, section) is not None
    )


def _edit(path: Path, *, prose: tuple[str, str] | None = None, mutate=None) -> None:
    text = path.read_text()
    if prose is not None:
        old, new = prose
        path.write_text(text.replace(old, new))
        return
    state = _plan_html.read_state(text)
    mutate(state)
    path.write_text(_plan_html.write_state(text, state))


def test_a_queued_request_records_the_digests_it_was_reviewed_against(
    two_section_plan,
):
    """The queue write stamps the section and contract digests it was queued on."""
    _, repo, path = two_section_plan
    _reviewed(path)
    pointer = _queue(repo, "s2")

    contract = pointer["review_contract"]
    digests = plan_review._section_digests(Path(path))
    assert contract["section"] == "s2"
    assert contract["section_digest"] == digests["s2"]
    assert contract["document_digest"] == routing._contract_digest(
        _plan_html.read_state_file(Path(path))
    )

    # The stamp is the queue-time text, not whatever the plan says later.
    _edit(path, prose=("Build two.", "Build two, revised at length, elsewhere."))
    assert runs.read_pointer(pointer["run_id"])["review_contract"] == contract


def _assert_narrowed(monkeypatch) -> None:
    """Fail if admission answered by re-running the whole dispatch gate.

    A request that recorded its contract is judged by comparing the plan with
    the digests it was queued on. Falling back to the dispatch gate is the
    behaviour for a request that recorded none, and it would let an unrelated
    plan edit hold the request once the gate's own narrowing changes.
    """

    def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "admission re-ran the dispatch gate for a request that recorded its "
            "own review contract"
        )

    monkeypatch.setattr(queue_admission, "_dispatch_gate_owed", _forbidden)


def test_an_edit_to_another_section_leaves_the_request_admissible(
    two_section_plan, monkeypatch
):
    """An edit to a section the request does not read never holds it."""
    _, repo, path = two_section_plan
    _reviewed(path)
    pointer = _queue(repo, "s2")

    _edit(path, prose=("Build three.", "Extend three."))
    _assert_narrowed(monkeypatch)
    assert _owed(pointer) is None
    _queued_and_fresh_agree(repo, pointer)


def test_a_change_to_its_own_sections_waits_leaves_the_request_admissible(
    two_section_plan, monkeypatch
):
    """An ordering edit is no request's contract, so it holds nothing."""
    _, repo, path = two_section_plan
    _reviewed(path)
    pointer = _queue(repo, "s2")

    _edit(
        path,
        mutate=lambda state: state.update(
            {"section_depends_on": {"s2": ["fixture#s3"]}}
        ),
    )
    _assert_narrowed(monkeypatch)
    assert _owed(pointer) is None
    _queued_and_fresh_agree(repo, pointer)


def test_a_decision_change_holds_it_until_a_covering_review_is_stored(
    two_section_plan,
):
    """An uncovered contract edit holds the request; a review of it releases it."""
    _, repo, path = two_section_plan
    _reviewed(path)
    pointer = _queue(repo, "s2")

    _edit(
        path,
        mutate=lambda state: state["decisions"]["choice"].update(
            rationale="Choose a different owner."
        ),
    )
    detail = _owed(pointer)
    assert detail is not None
    assert plan_review.DOCUMENT_UNIT in detail
    _queued_and_fresh_agree(repo, pointer)

    # The write-time review of that text releases the same request, and no
    # further dispatch is needed: the pointer is unchanged.
    _store_content(path, "review-content-two")
    assert _owed(pointer) is None
    _queued_and_fresh_agree(repo, pointer)


def test_its_own_section_text_change_holds_it_until_a_covering_review(
    two_section_plan,
):
    """Its own section moving uncovered holds the request, and only until review."""
    _, repo, path = two_section_plan
    _reviewed(path)
    pointer = _queue(repo, "s2")

    _edit(
        path,
        prose=(
            "Build two.",
            "Build two, and then build two more, at length, in a different way.",
        ),
    )
    detail = _owed(pointer)
    assert detail is not None
    assert "s2" in detail
    _queued_and_fresh_agree(repo, pointer)

    _store_content(path, "review-content-two")
    assert _owed(pointer) is None
    _queued_and_fresh_agree(repo, pointer)


def test_a_fresh_dispatch_is_stamped_like_the_queued_request(two_section_plan):
    """A request queued later carries the same contract verdict as the first
    request, so the queued and freshly dispatched verdicts cannot disagree."""
    _, repo, path = two_section_plan
    _reviewed(path)
    queued = _queue(repo, "s2")
    fresh = _fresh_pointer(repo, "s2")
    assert fresh["review_contract"]["section"] == queued["review_contract"]["section"]
    assert (
        fresh["review_contract"]["section_digest"]
        == queued["review_contract"]["section_digest"]
    )
    assert (
        fresh["review_contract"]["document_digest"]
        == queued["review_contract"]["document_digest"]
    )
    _queued_and_fresh_agree(repo, queued)
