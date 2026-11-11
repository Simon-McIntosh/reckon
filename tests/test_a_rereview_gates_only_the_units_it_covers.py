"""A re-review gates dispatch only on the units it was composed to cover.

Coverage is section-scoped: an edit uncovers only the units it changed, and a
re-review is composed for those units. But the re-review reads the whole plan,
so it can raise a finding on a section an earlier review already covers. That
finding is real, and it is recorded, but it gates a dispatch of content the
round was never asked to judge. It is therefore listed with the review as an
observation and is not counted as unanswered by the dispatch gate, while a
finding on a unit the round did trace gates exactly as before. A first review
traces every unit, so every finding it raises still gates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import plan_review, recovery, review
from reckon.crew.node import PlanReviewMissingError
from tests.test_a_finding_blocks_its_own_section import (
    CONTENT_VERSION,
    PLAN,
    PROSE,
    _empty_design,
    _gate,
    _line_of,
    _report,
    _subject,
    many_section_plan as many_section_plan,  # noqa: PLC0414
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414

TRACED = "s4"
UNTRACED = "s21"
IN_SCOPE = "reasoning-1"
OUT_OF_SCOPE = "done_when-1"
# The edit the re-review is composed for: §4's prose, and nothing else.
EDITED = "Charge the read once and reuse the parsed result."


def _findings(path, *, traced_marker=PROSE[TRACED]) -> list[dict]:
    """One finding on the traced section and one on a covered section."""
    return [
        {
            "id": IN_SCOPE,
            "type": "reasoning",
            "anchor": f"{PLAN}:{_line_of(path, traced_marker)}",
            "text": "§4's premise names a counter the code cannot observe.",
        },
        {
            "id": OUT_OF_SCOPE,
            "type": "done_when",
            "anchor": f"{PLAN}:{_line_of(path, PROSE[UNTRACED])}",
            "text": "§21's done-when declares no negative control.",
        },
    ]


def _store(path, *, traced, findings, rubric="content", run_id="review-content"):
    """Store one review whose record names the units it traced.

    ``traced`` empty is a whole-plan review, which is every first review and
    every review composed before a round could be scoped.
    """
    items = (
        review.PLAN_DESIGN_REVIEW_ITEMS
        if rubric == "design"
        else review.PLAN_REVIEW_ITEMS
    )
    fields = recovery._review_dispatch_fields(
        _subject(path, run_id, rubric), rubric=rubric
    )
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    sidecar["plan_version"] = CONTENT_VERSION
    if traced:
        sidecar[plan_review.TRACED_UNITS_KEY] = list(traced)
    Path(sidecar["report_path"]).write_text(_report(items))
    record = {
        **sidecar,
        "findings": list(findings),
        "responses": {},
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record)
    return record


@pytest.fixture
def rereview(many_section_plan):
    """A design review, an edit to §4 alone, then a re-review tracing §4."""
    _home, _repo, path = many_section_plan
    _empty_design(path)
    path.write_text(path.read_text().replace(PROSE[TRACED], EDITED))
    return many_section_plan, _store(
        path, traced=[TRACED], findings=_findings(path, traced_marker=EDITED)
    )


def test_a_rereview_gates_on_the_finding_of_the_unit_it_traced(rereview):
    (_home, repo, _path), record = rereview

    assert plan_review.unanswered_findings_by_unit(record) == {TRACED: [IN_SCOPE]}
    with pytest.raises(PlanReviewMissingError) as raised:
        _gate(repo, TRACED)

    refusal = str(raised.value)
    assert IN_SCOPE in refusal
    assert OUT_OF_SCOPE not in refusal


def test_a_rereview_lists_its_finding_on_a_covered_unit_as_an_observation(rereview):
    (_home, repo, _path), record = rereview

    assert plan_review.observation_findings(record) == [OUT_OF_SCOPE]
    assert plan_review.unanswered_findings(record) == [IN_SCOPE, OUT_OF_SCOPE]
    assert _gate(repo, UNTRACED) is None


def test_a_first_review_gates_on_every_finding(many_section_plan):
    _home, repo, path = many_section_plan
    record = _store(path, traced=[], findings=_findings(path), rubric="design")

    assert plan_review.observation_findings(record) == []
    assert set(plan_review.unanswered_findings_by_unit(record)) == {TRACED, UNTRACED}
    with pytest.raises(PlanReviewMissingError, match=IN_SCOPE):
        _gate(repo, TRACED)
    with pytest.raises(PlanReviewMissingError, match=OUT_OF_SCOPE):
        _gate(repo, UNTRACED)