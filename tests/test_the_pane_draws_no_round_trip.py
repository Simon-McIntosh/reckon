"""RowPolicy draws no round trip, and no coordinator-made withdrawal, on the pane.

A run can leave the word the pane last showed for it and come back within
seconds — a requeue, a lane change, a resume that is refused. Neither half asks
the coordinator for anything, so neither reaches the stream: the departure and
its return are held and dropped together, and a departure that never returns
prints nothing either, because a withdrawal the coordinator made is the
coordinator's own act. The duty such an away run can still hit — a stop that
asks for the coordinator — is carried, and is not swallowed by the hold. The
clock is a stub, so every window is measured rather than waited out.
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


def test_a_departure_that_does_not_return_still_prints_no_row() -> None:
    """The same departure with no return prints nothing when the window passes.

    A withdrawal the coordinator made is the coordinator's own act, so it is
    drawn in the view and never carried: neither before the window closes nor
    after it.
    """
    policy = _showing_queued()
    departure = _row("queued", "withdrawn")
    assert policy.feed(departure, now=0.0) == []
    assert policy.flush(now=ticker.SETTLE_WINDOW - 1.0) == []
    assert policy.flush(now=ticker.SETTLE_WINDOW) == []
    assert policy.flush(now=1e9) == []
    assert ticker._pane_word(departure) == "dropped"
    assert "dropped" in ticker.Ticker().render(departure)


def test_a_run_that_never_reached_the_pane_ends_with_no_row() -> None:
    """A run whose word the pane never showed is not a departure.

    Its end is a discard the coordinator made, which the discarding call already
    reported, so it prints nothing whether or not any word was shown for it.
    """
    policy = ticker.RowPolicy()
    end = _row("working", "discarded")
    assert policy.feed(end, now=1.0) == []
    assert policy.flush(now=1e9) == []
    assert ticker._pane_word(end) == "dropped"


def test_a_terminal_echo_the_pane_withholds_stays_hidden() -> None:
    """A withheld row is not made visible by the round-trip hold.

    The terminal echo (``complete -> recorded``) asks for nothing: the pane
    already showed the completion, so the echo is inventory and the observer
    hold withholds it. A departure hold on such a row would print it when the
    window passed, which is the opposite of what the pane's contract says — so
    a row the observer hold withholds stays withheld whether or not it is a
    departure, and nothing is delivered at any later instant either.
    """
    policy = ticker.RowPolicy()
    policy.seed(RUN, "complete")
    echo = _row("complete", "recorded")
    assert policy.feed(echo, now=0.0) == []
    assert policy.flush(now=ticker.SETTLE_WINDOW) == []
    assert policy.flush(now=1e9) == []


def test_a_duty_arriving_while_away_still_reaches_the_pane() -> None:
    """A coordinator duty while the run is away prints, and the departure does not.

    A stop that asks for the coordinator is news and is carried, even behind a
    departure the pane never returned from; the departure itself is the
    coordinator's own withdrawal and prints nothing beside it.
    """
    policy = _showing_queued()
    departure = _row("queued", "withdrawn")
    assert policy.feed(departure, now=0.0) == []
    duty = _row("withdrawn", "failed", recovery_classification="failed")
    first = policy.feed(duty, now=1.0)
    # The duty is a coordinator row held on its own settle window, so it lands
    # when the window passes; the departure behind it contributes no row.
    assert departure not in first, first
    released = policy.flush(now=1.0 + ticker.SETTLE_WINDOW)
    assert any(row.get("to_state") == "failed" for row in released), released
    assert all(row.get("to_state") != "withdrawn" for row in released), released
