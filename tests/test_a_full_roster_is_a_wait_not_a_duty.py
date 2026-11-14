"""A queued request refused at a full placement roster is waiting, not owed.

The admission pass stores a roster refusal on the queued request and leaves it
queued, because a seat frees when a worker ends. The refusal's own text quotes
``reckon crew complete`` for an occupying run, and the classifier published
that quoted command as the request's duty, so on 14 November every coordinator
whose reviews waited at the roster saw ``queued-awaiting-session`` rows telling
them to complete runs that had never launched.
"""

from __future__ import annotations

from reckon.crew import recovery_classification

ROSTER_REFUSAL = (
    "RosterFull: 23 workers already occupy the placement reservation's roster, "
    "whose cap is 22. The cap is sized on resident memory per worker against "
    "the reservation's reserved memory, not a core count. Resolve with "
    "`reckon crew observe` and `reckon crew complete` on an occupying run, "
    "then `reckon crew dispatch`. A seat frees when one of those workers ends, "
    "so a request queued behind this cap launches then."
)


def _queued(failure: str) -> dict:
    return {
        "run_id": "r-queued-review",
        "project": "proj",
        "session": "s-1",
        "phase": "queued",
        "failure": failure,
        "failure_at": "2026-11-14T22:35:34Z",
    }


def test_a_roster_refusal_keeps_the_waiting_reading():
    assert (
        recovery_classification._queued_failure_remedy(_queued(ROSTER_REFUSAL)) is None
    )


def test_a_refusal_that_quotes_its_remedy_is_still_a_duty():
    refusal = (
        "PlanReviewMissingError: the plan carries no review; compose it with "
        "`reckon crew review --project proj --plan p --rubric content`"
    )
    remedy = recovery_classification._queued_failure_remedy(_queued(refusal))
    assert remedy is not None
    assert remedy["next_command"].startswith("reckon crew review")
