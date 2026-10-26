"""A delivery the pane already drew is not drawn again once its review starts.

A producer re-sweeps a delivered run whose state has not changed, so the
delivery it writes again is a row the pane printed before. While the run's
review is running that echo would draw the run leaving its review, which reads
as the review having landed something when it has landed nothing. The pane
withholds the echo for the attempt that showed the delivery; a resumed
attempt's own delivery is new and prints.

The negative control is the second test: the same two delivery rows with no
review between them both print, so the withheld row in the first test is the
memory's work rather than a row the pane never carried.
"""

from __future__ import annotations

from reckon.crew import ticker

SESSION = "s-own"
IMPL = "r-impl-1"
IMPL_NODE = "impl-node"
REVIEW = "r-review-1"
REVIEW_NODE = f"review-of-{IMPL}-0123456789ab"

# The classification a delivered run awaiting its review carries, and the one
# that makes the re-emitted row actionable: an actionable row repeats the pane's
# word and is still printed, which is what let the echo through.
SCORING = {"recovery_classification": "scoring"}


def _row(run_id, from_state, to_state, **overrides):
    row = {
        "event": "transition",
        "run_id": run_id,
        "node": IMPL_NODE if run_id == IMPL else REVIEW_NODE,
        "session": SESSION,
        "from_state": from_state,
        "to_state": to_state,
    }
    row.update(overrides)
    return row


def _review_row(from_state, to_state, **overrides):
    """A review run's row, naming the run it reads as the producer writes it."""
    return _row(
        REVIEW,
        from_state,
        to_state,
        reviewed_run_id=IMPL,
        reviewed_node=IMPL_NODE,
        **overrides,
    )


def _delivery():
    """The delivered row the producer writes, and writes again on a re-sweep."""
    return _row(IMPL, "working", "complete", **SCORING)


def _print(sequence, *, own_session=SESSION):
    """Feed timed events through one pane and return every row it printed."""
    path = ticker.PaneRowPath(own_session=own_session)
    printed = []
    for moment, event in sequence:
        printed.extend(path.flush(now=moment))
        printed.extend(path.feed(event, now=moment))
    printed.extend(path.flush(now=1e9))
    return printed


def _words(rows):
    return [ticker._pane_word(row) for row in rows]


def test_the_reemitted_delivery_after_the_review_row_prints_nothing() -> None:
    """The delivery, then the review's start, then the echo: two rows, not three."""
    printed = _print(
        [
            (0.0, _row(IMPL, None, "dispatched")),
            (10.0, _row(IMPL, "dispatched", "working")),
            (30.0, _delivery()),
            (70.0, _review_row(None, "dispatched")),
            (80.0, _review_row("dispatched", "starting")),
            (90.0, _review_row("starting", "working")),
            # The producer re-sweeps the delivered run and writes the same row
            # again, carrying the classification that made it actionable.
            (120.0, _delivery()),
        ]
    )

    assert _words(printed) == ["working", "delivered", "in review"], _words(printed)
    assert [row.get("run_id") for row in printed] == [IMPL, IMPL, IMPL], [
        row.get("run_id") for row in printed
    ]
    in_review = printed[2]
    assert in_review.get("node") == IMPL_NODE, in_review.get("node")
    assert in_review.get("review_run_id") == REVIEW, in_review.get("review_run_id")


def test_the_same_delivery_twice_prints_twice_without_a_review() -> None:
    """The control: the echo is carried when no review row stands before it."""
    printed = _print(
        [
            (0.0, _row(IMPL, None, "dispatched")),
            (10.0, _row(IMPL, "dispatched", "working")),
            (30.0, _delivery()),
            (120.0, _delivery()),
        ]
    )

    assert _words(printed) == ["working", "delivered", "delivered"], _words(printed)


def test_a_new_attempt_after_the_review_prints_its_launch_and_its_delivery() -> None:
    """A resume is a new attempt: its launch prints, and its own delivery too.

    The review's own delivery sits between them, so the arm also shows the
    withholding is the reviewed run's and does not swallow the review run's
    delivery.
    """
    printed = _print(
        [
            (0.0, _row(IMPL, None, "dispatched")),
            (10.0, _row(IMPL, "dispatched", "working")),
            (30.0, _delivery()),
            (70.0, _review_row(None, "dispatched")),
            (80.0, _review_row("dispatched", "starting")),
            (90.0, _review_row("starting", "working")),
            (120.0, _delivery()),
            (200.0, _review_row("working", "complete")),
            # The review sends the run back to work: a halt, then the resume.
            (240.0, _row(IMPL, "complete", "interrupted")),
            (250.0, _row(IMPL, "interrupted", "working")),
            (300.0, _delivery()),
        ]
    )

    assert _words(printed) == [
        "working",
        "delivered",
        "in review",
        "delivered",
        "halted",
        "working",
        "delivered",
    ], _words(printed)
    assert [row.get("run_id") for row in printed] == [
        IMPL,
        IMPL,
        IMPL,
        REVIEW,
        IMPL,
        IMPL,
        IMPL,
    ], [row.get("run_id") for row in printed]

    launches = [row for row in printed if row.get(ticker.LAUNCH_FIELD)]
    assert len(launches) == 3, _words(printed)
    # The review's launch is the middle one, drawn on the run it reads; the
    # implementing run's own two launches open its first attempt and its resume.
    assert launches[1].get("review_run_id") == REVIEW, launches[1].get("review_run_id")
    assert [launches[0].get("from_state"), launches[2].get("from_state")] == [
        "dispatched",
        "interrupted",
    ], [launches[0].get("from_state"), launches[2].get("from_state")]
