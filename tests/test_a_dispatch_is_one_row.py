"""A launch prints no row when it lands in an observer kind.

Every run opens the pane with an arrival row — its first sighting in
``dispatched`` — and then, within the same minute, the run's first transition
out of ``dispatched``. The arrival says nothing the dispatch payload did not
already carry, and a launch that settles into an observer kind such as
``working`` says nothing either, so the pane receives no row for it. An arrival
that never moves prints at its window, because a run still sitting in
``dispatched`` is a duty the pane must show, and an arrival whose first
transition is a coordinator row prints as the arrival collapsed into it.

The clock is a stub, so a case states the instant each row arrives and the
window is measured rather than waited out.
"""

from __future__ import annotations

from reckon.crew import ticker

ARRIVAL = {
    "event": "baseline",
    "run_id": "r-one",
    "node": "one",
    "from_state": None,
    "to_state": "dispatched",
}


def _arrival(**overrides):
    row = dict(ARRIVAL)
    row.update(overrides)
    return row


def _row(from_state, to_state, **extra):
    row = {
        "event": "transition",
        "run_id": "r-one",
        "node": "one",
        "from_state": from_state,
        "to_state": to_state,
    }
    row.update(extra)
    return row


def _kinds(rows):
    return [(row.get("from_state"), row.get("to_state")) for row in rows]


def test_arrival_then_working_prints_no_row() -> None:
    path = ticker.PaneRowPath()
    assert path.feed(_arrival(), now=0.0) == []
    # The arrival resolves into the observer kind ``working``, which asks the
    # coordinator for nothing, so the launch reaches the pane as no row at all.
    assert path.feed(_row("dispatched", "working"), now=19.0) == []
    assert path.flush(now=1e9) == []


def test_arrival_alone_prints_at_the_window() -> None:
    path = ticker.PaneRowPath()
    assert path.feed(_arrival(), now=0.0) == []
    assert path.flush(now=ticker.ARRIVAL_WINDOW - 1.0) == []
    printed = path.flush(now=ticker.ARRIVAL_WINDOW)
    assert len(printed) == 1, printed
    assert _kinds(printed) == [(None, "dispatched")]


def test_arrival_collapses_through_the_launch_flicker() -> None:
    path = ticker.PaneRowPath()
    assert path.feed(_arrival(), now=0.0) == []
    # The first transition out of dispatched is itself a held flicker opener,
    # so the arrival's hold passes to it and neither prints yet. The pair then
    # resolves into the observer ``working``, so nothing reaches the pane.
    assert path.feed(_row("dispatched", "abandoned"), now=5.0) == []
    assert path.feed(_row("abandoned", "working"), now=40.0) == []
    assert path.flush(now=1e9) == []


def test_arrival_then_blocked_prints_one_row() -> None:
    path = ticker.PaneRowPath()
    assert path.feed(_arrival(), now=0.0) == []
    printed = path.feed(_row("dispatched", "blocked"), now=30.0)
    assert len(printed) == 1, printed
    assert _kinds(printed) == [("dispatched", "blocked")]


def test_a_re_armed_pane_does_not_reannounce_a_working_run() -> None:
    path = ticker.PaneRowPath(reported={"r-one": "working"})
    assert path.feed(_arrival(to_state="working"), now=0.0) == []
    assert path.flush(now=1e9) == []


def test_a_repeated_arrival_still_prints_no_row() -> None:
    path = ticker.PaneRowPath()
    assert path.feed(_arrival(), now=0.0) == []
    # A re-arm re-derives the baseline, so the run's arrival arrives twice. The
    # second is not a move out, and the move out lands in an observer kind, so
    # neither the repeated arrival nor the move reaches the pane.
    assert path.feed(_arrival(), now=15.0) == []
    assert path.feed(_row("dispatched", "working"), now=25.0) == []
    assert path.flush(now=1e9) == []
