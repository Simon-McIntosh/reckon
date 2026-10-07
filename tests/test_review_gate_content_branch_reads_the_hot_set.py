"""The gate counts an unanswered finding only while its content review is hot.

The dispatch gate's content branch joins the newest review of the content about
to be built and counts its unanswered findings only when that review is hot.
Deleting the branch's ``is_hot`` check leaves the design-branch tests green, so
this file pins the content branch from both sides: a content review whose
reviewed section is declared done is the archive and the gate admits the node,
while the same review left hot still refuses on its finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew import plan_review
from tests.test_review_gate_reads_the_hot_set import (
    GATE_PLAN,
    GATE_PROJECT,
    _node,
    _plan,
    _review_store_root,
)
from tests.test_review_gate_reads_the_hot_set import (
    gate_project as gate_project,  # noqa: PLC0414
)

CONTENT_FINDING = "content-orphan-finding"


def _store_content_review(
    plan_path: Path,
    config_home: Path,
    *,
    section_digests: dict[str, str],
) -> None:
    """Store a finding review of the content about to be built.

    Its whole-plan fingerprint matches the current plan, so it is the review the
    gate joins to that content. ``section_digests`` names the sections it read,
    which is what makes it landed once every one of them is declared done.
    """
    plan_review.store_plan_review(
        {
            "project": GATE_PROJECT,
            "plan_slug": GATE_PLAN,
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "b" * 40,
            "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
            "section_digests": section_digests,
            "findings": [
                {"id": CONTENT_FINDING, "type": "reuse", "text": "name the owner"}
            ],
            "responses": {},
            "review_run_id": "r-content-review",
        },
        base_dir=_review_store_root(config_home),
    )


def _store_design_review_of_the_newer_section(
    plan_path: Path, config_home: Path
) -> None:
    """Store the design review that read only the newer implementable section.

    It carries no finding, so it decides nothing on its own; its presence
    satisfies the once-owed design requirement while the section it read — the
    one the node is about to build — keeps the review off the archive.
    """
    plan_review.store_plan_review(
        {
            "project": GATE_PROJECT,
            "plan_slug": GATE_PLAN,
            "plan_version": 1,
            "rubric": plan_review.DESIGN_RUBRIC,
            "reviewed_blob_sha": "a" * 40,
            "section_digests": {"delivery": "d"},
            "findings": [],
            "responses": {},
            "review_run_id": "r-design-review",
        },
        base_dir=_review_store_root(config_home),
    )


def test_the_gate_admits_a_landed_content_review_with_an_unanswered_finding(
    gate_project: tuple[Path, Path, Path],
) -> None:
    """A content review whose reviewed section is done no longer refuses.

    The content review read only ``foundation``, now declared done, so it is
    landed and its unanswered finding is archive; the node builds the newer
    ``delivery`` section, and the gate admits it rather than refusing on a
    finding the collapsed work has already retired.
    """
    config_home, repo, plan_path = gate_project
    _store_content_review(plan_path, config_home, section_digests={"foundation": "d"})
    _store_design_review_of_the_newer_section(plan_path, config_home)

    resolution = _plan(_node(config_home), repo)

    assert resolution.validation.ok is True


def test_the_gate_refuses_an_unanswered_finding_of_a_hot_content_review(
    gate_project: tuple[Path, Path, Path],
) -> None:
    """The content review read ``delivery``, still implementable, so it is hot.

    Its unanswered finding is a live obligation and the gate refuses, which pins
    the guard from the other side: it suppresses a landed review's findings
    without suppressing a hot one's.
    """
    config_home, repo, plan_path = gate_project
    _store_content_review(plan_path, config_home, section_digests={"delivery": "d"})
    _store_design_review_of_the_newer_section(plan_path, config_home)

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo)

    refusal = str(excinfo.value)
    assert CONTENT_FINDING in refusal
    assert "unanswered" in refusal
