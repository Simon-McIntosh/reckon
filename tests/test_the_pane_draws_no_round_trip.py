"""RowPolicy draws no round trip on the pane.

A run can leave the word the pane last showed for it and come back within
seconds — a requeue, a lane change, a resume that is refused. Both halves would
print and cancel each other, so the departure is held for the settle window and
dropped together with the return; a departure that does not come back inside
the window is delivered when the window passes, so a real withdrawal still
reaches the reader. The clock is a stub, so every window is measured rather
than waited out.
"""

from __future__ import annotations

from reckon.crew import ticker

RUN = "r-round-trip"


def _row(from_state, to_state, **extra):
    row = {
        "event": "transition",
        "run_id": RUN,
        "node": "one",
        "from_state": from_state,
        "to_state": to_state,
    }
    row.update(extra)
    return row


def _kinds(rows):
    return [(row.get("from_state"), row.get("to_state")) for row in rows]


def _showing_queued():
    """A pane that already shows the run as queued, as a re-arm leaves it."""
    policy = ticker.RowPolicy()
    policy.seed(RUN, "queued")
    return policy


def test_a_run_that_returns_to_its_shown_word_draws_no_row() -> None:
    """``queued -> withdrawn -> dispatched -> queued`` inside the window: no row.

    The run leaves the queued word the pane shows and returns to it, so the
    departure and its return cancel. Neither half reaches the pane, and nothing
    is delivered once the window passes either.
    """
    policy = _showing_queued()
    for now, (from_state, to_state) in [
        (1.0, ("queued", "withdrawn")),
        (2.0, ("withdrawn", "dispatched")),
        (3.0, ("dispatched", "queued")),
    ]:
        assert policy.feed(_row(from_state, to_state), now=now) == [], (now, from_state)
    assert policy.flush(now=1e9) == []


def test_a_departure_that_does_not_return_is_delivered_when_the_window_passes() -> None:
    """The same departure with no return delivers its dropped row at the window.

    It is held for the window, not suppressed: before the window closes nothing
    is delivered, and once it passes the withdrawal reaches the pane exactly as
    it would have without the round-trip rule.
    """
    policy = _showing_queued()
    departure = _row("queued", "withdrawn")
    assert policy.feed(departure, now=0.0) == []
    assert policy.flush(now=ticker.SETTLE_WINDOW - 1.0) == []
    assert policy.flush(now=ticker.SETTLE_WINDOW) == [departure]


def test_a_run_that_never_reached_the_pane_ends_unheld() -> None:
    """A run whose word the pane never showed is not a departure.

    Its end is the only notice the reader gets that it stopped, so it is
    delivered at once rather than held for a return that cannot be measured
    against any word.
    """
    policy = ticker.RowPolicy()
    end = _row("working", "discarded")
    assert policy.feed(end, now=1.0) == [end]


def test_a_duty_arriving_while_away_still_reaches_the_pane() -> None:
    """A coordinator duty while the run is away prints, and the departure with it.

    The hold suppresses the round trip's middle, not a real duty: a stop that
    asks for the coordinator is news, so it is not swallowed by a departure
    that has not returned yet.
    """
    policy = _showing_queued()
    departure = _row("queued", "withdrawn")
    assert policy.feed(departure, now=0.0) == []
    duty = _row("withdrawn", "failed", recovery_classification="failed")
    first = policy.feed(duty, now=1.0)
    # The departure prints ahead of the duty, which is a coordinator row held on
    # its own settle window.
    assert departure in first, first
    released = policy.flush(now=1.0 + ticker.SETTLE_WINDOW)
    assert any(row.get("to_state") == "failed" for row in released), released
