"""A plan review reads the change, and the gate holds only what changed.

Two defects are measured here, both from dogfooding one review round per plan
edit. A composed review re-reads the whole plan though the coverage predicate
already knows which units moved, so the review is given the plan file's diff
since the commit the last review read, and the full text of the units it is
asked about and of no other section. And the dispatch gate was plan-wide, so
one uncovered section refused the dispatch of every other section, each of
which stayed reviewed.
"""

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review, recovery, review, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

SECTION_TEXTS = {"a": "Build alpha.", "b": "Build beta."}
EXTENDED_BETA = "Extend beta, reusing the owner."


@pytest.fixture
def section_plan(project):
    """A committed plan with two implementable sections and one decision."""
    _home, repo, path = project
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
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": "Reuse the owner.",
        }
    }
    path.write_text(_plan_html.write_state(text, state))
    _commit(repo, "docs(plan): split the fixture into two sections")
    return repo, path


def _commit(repo: Path, subject: str) -> str:
    subprocess.run(["git", "add", "docs/plans/fixture.html"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", subject, "-m", "Give a review a change to read."],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return _head(repo)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _complete_design_report() -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in review.PLAN_DESIGN_REVIEW_ITEMS)


def _subject(path: Path, run_id: str) -> dict:
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


def _review(path: Path, run_id: str, *, findings=(), responses=None) -> dict:
    """Compose one plan review through the real composer and store it.

    The sidecar the composer writes is the store's input, so the record a later
    reader finds is the one that path produces — the commit it read included —
    rather than one this test assembles.
    """
    fields = recovery._review_dispatch_fields(_subject(path, run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(_complete_design_report())
    plan_review.store_plan_review(
        {
            **sidecar,
            "findings": list(findings),
            "responses": dict(responses or {}),
            "review_run_id": run_id,
        }
    )
    return sidecar


def _edit_section(path: Path, name: str, text: str) -> None:
    path.write_text(path.read_text().replace(SECTION_TEXTS[name], text))


def _gate(repo: Path, path: Path, section: str):
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
        authority={"plan": {"docs": str(path.parent), "source": "repository"}},
        enforce=True,
    )


def test_stored_record_names_the_commit_it_read(section_plan):
    repo, path = section_plan
    read = _head(repo)
    _review(path, "review-the-change.py:1")

    record = plan_review.read_plan_review("sample", "fixture", plan=path)
    assert record is not None
    assert record["plan_commit"] == read


def test_composed_review_carries_the_change_and_no_other_section(section_plan):
    _repo, path = section_plan
    _review(
        path,
        "review-the-change.py:1",
        findings=[
            {
                "id": "f1",
                "anchor": "a",
                "type": "reuse_search",
                "text": "Name the owner the section reuses.",
            }
        ],
        responses={
            "f1": {"action": "acted", "reason": "The owner is named in the plan."}
        },
    )
    _edit_section(path, "b", EXTENDED_BETA)

    scope = plan_review.review_scope(
        "sample", "fixture", plan=path, rubric=plan_review.DESIGN_RUBRIC
    )

    # The plan file's diff since the commit the last review read.
    assert "```diff" in scope
    assert any(
        line.startswith("+") and EXTENDED_BETA in line for line in scope.splitlines()
    )
    # The full text of the unit this review is asked about.
    assert "Full text of b" in scope
    assert EXTENDED_BETA in scope
    # No other section's text, and not the plan's own unrelated prose.
    assert SECTION_TEXTS["a"] not in scope
    # The earlier finding and the answer it recorded.
    assert "Name the owner the section reuses." in scope
    assert "acted: The owner is named in the plan." in scope


def test_unedited_section_dispatches_while_another_is_uncovered(section_plan):
    repo, path = section_plan
    _review(path, "review-the-change.py:1")
    _edit_section(path, "b", EXTENDED_BETA)

    assert _gate(repo, path, "a") is None
    with pytest.raises(PlanReviewMissingError, match="uncovered units: b;"):
        _gate(repo, path, "b")


def test_a_review_of_the_edit_releases_it(section_plan):
    repo, path = section_plan
    _review(path, "review-the-change.py:1")
    _edit_section(path, "b", EXTENDED_BETA)
    with pytest.raises(PlanReviewMissingError):
        _gate(repo, path, "b")

    _review(path, "review-the-change.py:2")

    assert _gate(repo, path, "b") is None
