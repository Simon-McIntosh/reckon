"""A queued review is promoted before the run it reviews, and that run waits.

On 13 November the reviews of two runs were requested at 15:02 and 15:04 and
the implementations of the same runs at 15:04; the service took up the
implementations first and refused both as terminal for having no complete
independent review stored, while the reviews that would have licensed them
were still in the same queue. A refused request stays refused, so the
coordinator had to notice and re-request work that was about to become
promotable. These tests read the drain's own order and its handling of a run
that loses the race.
"""

from __future__ import annotations

import threading

from reckon.crew import promotion_requests as requests
from reckon.crew import runs
from reckon.crew.node import CrewError

PROJECT = "review-order-fixture"
SESSION = "review-order-session"
# The run is dispatched before its review, so its id sorts first by name: plain
# name order reaches the run before the review that would licence it.
RUN_ID = "r-20261113T150400000000-run-queued-before-its-review"
REVIEW_ID = "r-20261113T150410000000-review-of-run-queued-before-its-review"
MISSING_REVIEW = (
    "run {run_id!r} is classified scoring; no complete independent review is "
    "stored. Produce it with `reckon crew dispatch --review`, or promote anyway "
    "with --waive-unreviewed-promotion REASON stating why this run may land "
    "without review"
)


def _queue(run_id: str, node_id: str) -> None:
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "complete",
        "node": {"id": node_id, "role": "implement", "write_paths": []},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    requests.request_promotion(run_id)


class _StubPromotions:
    """Stand in for the promotion work, refusing a run whose review is absent."""

    def __init__(self) -> None:
        self.promoted: list[str] = []
        self.attempts: list[str] = []

    def __call__(self, request):
        run_id = str(request["run_id"])
        self.attempts.append(run_id)
        if run_id != REVIEW_ID and REVIEW_ID not in self.promoted:
            raise CrewError(MISSING_REVIEW.format(run_id=run_id))
        if run_id not in self.promoted:
            self.promoted.append(run_id)
        return {"ok": True, "run_id": run_id, "record": {"commits": []}}


class _RacingPromotions(_StubPromotions):
    """Hold the review's promotion until the run has been refused once.

    A run promoted before its review is refused, and that refusal is what the
    drain must keep queued rather than record as terminal.
    """

    def __init__(self) -> None:
        super().__init__()
        self.review_running = threading.Event()
        self.run_refused = threading.Event()

    def __call__(self, request):
        run_id = str(request["run_id"])
        if run_id == REVIEW_ID:
            self.review_running.set()
            assert self.run_refused.wait(5), "the run's promotion never ran"
            return super().__call__(request)
        if REVIEW_ID not in self.promoted and not self.run_refused.is_set():
            try:
                return super().__call__(request)
            finally:
                self.run_refused.set()
        return super().__call__(request)


def _drain(service: requests.PromotionService, *, passes: int = 8) -> None:
    for _ in range(passes):
        launched = service.tick()
        service.settle(timeout=20)
        if not launched:
            return
    raise AssertionError("the queue never drained")


def test_a_review_is_promoted_before_the_run_it_reviews():
    _queue(RUN_ID, "delivery")
    _queue(REVIEW_ID, f"review-of-{RUN_ID}")
    promotions = _StubPromotions()
    service = requests.PromotionService(width=1, execute=promotions)

    assert service.tick() == [REVIEW_ID], "the review was not taken up first"
    service.settle(timeout=20)
    _drain(service)

    assert promotions.promoted == [REVIEW_ID, RUN_ID], promotions.attempts
    assert requests.read_promotion(REVIEW_ID)["phase"] == requests.PROMOTED
    assert requests.read_promotion(RUN_ID)["phase"] == requests.PROMOTED


def test_a_run_refused_only_for_its_queued_review_stays_queued():
    _queue(RUN_ID, "delivery")
    _queue(REVIEW_ID, f"review-of-{RUN_ID}")
    promotions = _RacingPromotions()
    service = requests.PromotionService(width=2, execute=promotions)

    _drain(service)

    assert promotions.promoted == [REVIEW_ID, RUN_ID], promotions.attempts
    assert promotions.attempts.count(RUN_ID) == 2, promotions.attempts
    receipt = requests.read_promotion(RUN_ID)
    assert receipt["phase"] == requests.PROMOTED, receipt
    assert str(receipt.get("deferred_reason", "")).startswith("run ")


def test_a_run_with_no_review_anywhere_is_refused():
    _queue(RUN_ID, "delivery")
    promotions = _StubPromotions()
    service = requests.PromotionService(width=1, execute=promotions)

    _drain(service)

    receipt = requests.read_promotion(RUN_ID)
    assert receipt["phase"] == requests.REFUSED
    assert "no complete independent review is stored" in receipt["error"]
