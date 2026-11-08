"""A queued obligation reads a review answer before repeating its remedy."""

from __future__ import annotations

import pytest

from reckon.crew import plan_review, runs
from reckon.crew.node import PlanReviewMissingError
from tests import test_a_queued_run_is_never_told_to_redispatch as fixtures

mounted_home = fixtures.mounted_home


@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "answered"])
def test_a_resolved_refusal_row_states_the_current_hold(mounted_home, answered):
    review = {
        "project": fixtures.PROJECT,
        "plan_slug": "fixture",
        "plan_version": 1,
        "rubric": "plan_design_review",
        "review_run_id": "r-design-review",
        "findings": [{"id": "wiring-1", "type": "wiring", "text": "name the input"}],
        "responses": {},
    }
    plan_review.store_plan_review(review)
    command = plan_review.answer_invocation(fixtures.PROJECT, "fixture", "wiring-1")
    refusal = PlanReviewMissingError(f"finding wiring-1 is unanswered; run `{command}`")
    record = fixtures._write_queued_pointer(f"{type(refusal).__name__}: {refusal}")
    record["node"] = {"id": "queued-request", "plan": "fixture"}
    record["reason"] = "the session has no allowance for another worker"
    record["pass_hold"] = {"verdict": "held-for-allowance", "reason": record["reason"]}
    runs._write_json(runs.pointer_path(fixtures.QUEUED_RUN), record)
    if answered:
        plan_review.record_response(
            review, "wiring-1", action="declined", reason="the input is already named"
        )
    row = next(
        item
        for item in fixtures.obligations_module.obligations(
            fixtures.PROJECT, fixtures.SESSION
        )["obligations"]
        if item["run_id"] == fixtures.QUEUED_RUN
    )
    print(f"answered={answered}: {row}")
    if answered:
        assert row["kind"] == "run-queued", row
        assert "held-for-allowance" in str(row), row
        assert "--answer wiring-1" not in str(row), row
    else:
        assert row["kind"] == "queued-awaiting-session", row
        assert row["next_command"] == command, row
