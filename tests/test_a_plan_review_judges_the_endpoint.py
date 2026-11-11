"""A plan review judges the endpoint a section delivers, not a method.

A plan describes what a node must deliver and the measurement that proves it —
records and figures, their quantity, tolerance, independent reference and
negative control. It does not say how to compute any of it. A review rubric that
asks the author to cite a symbol, or that judges a stated mechanism, pulls the
author back into writing implementation, and each round of that costs a review
apiece.

So the composed briefs are held to that rule. The content brief must raise a
section that prescribes implementation as a finding, must judge reasoning on
whether the endpoint is reachable and checkable, and must judge a cited
reference only on whether it resolves. The design brief must place its reuse map
in the node brief and ask for no module, function or file to extend to be
written into the plan. Both skills say the same of the reuse map.

The negative control restores the base revision's rubric text, which makes the
content and design assertions fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew import review as review_module
from tests.test_review_plan_command import project as project  # noqa: PLC0414


def _normalised(text: str) -> str:
    """The text folded to one case with its line wrapping collapsed.

    A clause is asserted as one string, so a sentence the rubric wraps across
    two lines and one it opens with a capital read the same way here.
    """
    return " ".join(text.split()).lower()


def _brief(rubric: str, path: Path, repo: Path) -> str:
    """Compose the review brief the production composer writes for ``rubric``.

    ``write=False`` gives the text a dispatch would carry without leaving a
    report directory behind, so composing a brief for a test plan changes
    nothing on disk.
    """
    subject = {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(repo),
        "session": "coordinator",
        "rubric": rubric,
        "local": True,
        "run_id": f"review-{rubric}",
    }
    fields = recovery._review_dispatch_fields(subject, rubric=rubric, write=False)
    return fields["brief_text"]


def _skill(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return _normalised(
        (root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    )


@pytest.fixture
def briefs(project):
    _, repo, path = project
    return {
        "content": _normalised(_brief("content", path, repo)),
        "design": _normalised(_brief("design", path, repo)),
    }


def test_content_brief_raises_prescribed_implementation_as_a_finding(briefs) -> None:
    assert "a section that prescribes implementation is a finding" in briefs["content"]


def test_content_brief_judges_the_endpoint_not_a_stated_method(briefs) -> None:
    content = briefs["content"]
    assert "is the endpoint reachable and checkable" in content
    assert "does the section prescribe implementation" in content
    # The base rubric's two method checks, which hard rule 9 removes: judging
    # whether a stated mechanism produces the done-when, and requiring its
    # caller to be named. A brief carrying either pulls the author back into
    # writing the implementation it would have to justify.
    assert "does each section's mechanism produce its done-when" not in content
    assert "with the caller named" not in content


def test_content_brief_does_not_ask_the_author_to_cite_a_symbol(briefs) -> None:
    content = briefs["content"]
    assert "not asked to cite a symbol" in content
    assert "judged on resolution alone" in content
    # The base rubric opened this item by requiring claims to be cited, which is
    # what earned plans their symbol anchors round after round.
    assert "claims are cited" not in content


def test_design_brief_keeps_the_reuse_map_out_of_the_plan(briefs) -> None:
    design = briefs["design"]
    assert "no finding asks the plan to name a module" in design
    assert "belongs to the node brief" in design
    assert "never to plan text" in design
    # The base rubric made a plan that cited no searched module a finding, which
    # is the demand that wrote module names into plan text.
    assert (
        "a plan that adds a subsystem without citing what it searched is a finding"
        not in design
    )


@pytest.mark.parametrize(
    ("skill", "clause"),
    [
        ("reckon-build", "reuse map stays in the brief"),
        ("reckon-review", "never to plan text"),
    ],
)
def test_both_skills_keep_the_reuse_map_out_of_the_plan(skill, clause) -> None:
    text = _skill(skill)
    assert "reuse map" in text
    assert clause in text


def test_the_composed_briefs_are_the_prompts_the_store_reads(project) -> None:
    """The brief is the on-disk prompt plus its pointers, not a copy.

    A brief composed from a stale in-memory rubric would pass every assertion
    above while the reviewer received the text this test never read, so the
    brief must carry the loader's own words.
    """
    _, repo, path = project
    for rubric, loader in (
        ("content", review_module.load_plan_review_prompt),
        ("design", review_module.load_plan_design_review_prompt),
    ):
        brief = _normalised(_brief(rubric, path, repo))
        assert _normalised(loader()) in brief
