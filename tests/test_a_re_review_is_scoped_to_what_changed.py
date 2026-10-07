"""A later review of a plan reads what changed, not the whole plan again.

A plan's first review under a rubric reads everything. A later one is told
which units the earlier reviews no longer cover and which findings were already
answered, so a small edit to a large plan does not buy a fresh read of every
section and a fresh set of findings about sections nobody touched.
"""

from __future__ import annotations

from reckon.crew import plan_review, recovery
from tests.test_review_is_earned_by_significant_change import (
    PLAIN_B,
    _review,
    _subject,
)
from tests.test_review_is_earned_by_significant_change import (
    project as project,  # noqa: PLC0414
)
from tests.test_review_is_earned_by_significant_change import (
    sectioned as sectioned,  # noqa: PLC0414
)

ANSWERED = {
    "id": "wiring-1",
    "type": "wiring",
    "anchor": "#b",
    "text": "beta names no dependency on alpha",
}


def _brief(path, *, rubric="design", run_id="r-later-review") -> str:
    subject = {**_subject(path, run_id=run_id), "rubric": rubric}
    return recovery._review_dispatch_fields(subject, write=False)["brief_text"]


def _answered_review(path) -> None:
    _review(path, findings=[ANSWERED])
    record = plan_review.read_plan_review("sample", "fixture", plan=path)
    plan_review.record_response(
        record, "wiring-1", action="declined", reason="alpha lands first by order"
    )


def test_a_first_review_reads_the_whole_plan(sectioned):
    _, _, path = sectioned

    assert "Scope:" not in _brief(path)


def test_a_later_review_reads_the_uncovered_units_and_the_answers(sectioned):
    _, _, path = sectioned
    _answered_review(path)
    rewritten = " ".join(f"new{index:02d}" for index in range(25))
    path.write_text(path.read_text().replace(PLAIN_B, rewritten))

    brief = _brief(path)

    scope = brief[brief.index("Scope:") :]
    assert "b (" in scope
    assert "a (" not in scope and "c (" not in scope
    assert "wiring-1 [#b] beta names no dependency on alpha -> declined" in scope
    assert "alpha lands first by order" in scope


def test_another_rubrics_history_does_not_scope_the_first_review(sectioned):
    _, _, path = sectioned
    _answered_review(path)
    path.write_text(path.read_text().replace(PLAIN_B, "entirely new beta text"))

    assert "Scope:" not in _brief(path, rubric="content")


def test_a_review_asked_for_when_nothing_is_uncovered_reads_everything(sectioned):
    _, _, path = sectioned
    _answered_review(path)

    assert "Scope:" not in _brief(path)
