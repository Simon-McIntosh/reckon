"""A run's start reaches the view, and the pane's own session's start prints once.

A run beginning work is the launch the pane shows: the pane's own session's
start prints one row, drawn for the session the pane follows and for no other.
A peer session's start prints nothing, and a re-announcement or a replay of a
start the pane already shows adds no row.

The clock is a stub, so a case states the instant each event arrives and a
hold's window is measured rather than waited out.
"""

from __future__ import annotations

from reckon.crew import ticker

SESSION = "s-own"
PEER = "s-peer"
RUN = "r-own-1"


def _arrival(**overrides):
    row = {
        "event": "baseline",
        "run_id": RUN,
        "node": "claude-1",
        "session": SESSION,
        "from_state": None,
        "to_state": "dispatched",
    }
    row.update(overrides)
    return row


def _start(**overrides):
    """The row a run emits as it leaves dispatched and begins working."""
    row = {
        "event": "transition",
        "run_id": RUN,
        "node": "claude-1",
        "session": SESSION,
        "lane": "local",
        "model": "deepseek-v4.1-flash",
        "time_budget": "20m",
        "from_state": "dispatched",
        "to_state": "working",
    }
    row.update(overrides)
    return row


def test_a_panes_own_start_stays_in_the_view_and_leaves_the_stream() -> None:
    """The own session's start renders in the view and prints one launch row.

    A run beginning work is a state change the pane shows, so the held arrival
    resolves into the launch row rather than into silence. The pane's view still
    draws the start, naming the node and the model a reader follows the work by.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    assert path.feed(_arrival(), now=0.0) == []
    rows = path.feed(_start(), now=5.0)
    assert [ticker._pane_word(row) for row in rows] == ["working"], rows
    assert path.flush(now=1e9) == []

    drawn = ticker.Ticker().render(_start())
    assert "working" in drawn, drawn
    assert "claude-1" in drawn, drawn
    assert "deepseek" in drawn, drawn


def test_a_panes_own_start_prints_with_no_held_arrival() -> None:
    """A start prints its launch whether or not its arrival was held.

    The hold decides when the launch prints, not whether it does: feeding the
    start with no arrival before it and feeding it after a held arrival both
    draw the launch, once each.
    """
    bare = ticker.PaneRowPath(own_session=SESSION)
    assert [ticker._pane_word(row) for row in bare.feed(_start(), now=0.0)] == [
        "working"
    ]
    assert bare.flush(now=1e9) == []

    held = ticker.PaneRowPath(own_session=SESSION)
    assert held.feed(_arrival(), now=0.0) == []
    assert [ticker._pane_word(row) for row in held.feed(_start(), now=5.0)] == [
        "working"
    ]
    assert held.flush(now=1e9) == []


def test_a_peer_sessions_start_delivers_nothing() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)
    peer = _start(session=PEER, run_id="r-peer-1")
    assert path.feed(_arrival(session=PEER, run_id="r-peer-1"), now=0.0) == []
    assert path.feed(peer, now=5.0) == []
    assert path.flush(now=1e9) == []


def test_a_reannouncement_or_replay_of_the_same_start_delivers_nothing() -> None:
    """The launch prints once, and a replay or re-announcement adds nothing.

    The attempt's own memory is what decides: the pane already shows the run
    working, so neither the identical start again nor an arrival re-derived
    from the fleet is a second launch.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    assert path.feed(_arrival(), now=0.0) == []
    printed = path.feed(_start(), now=5.0)
    assert [ticker._pane_word(row) for row in printed] == ["working"], printed
    # A replay of the identical start: the pane already shows the run working.
    assert path.feed(_start(), now=6.0) == []
    # A re-announcement re-derives the arrival and repeats the start.
    assert path.feed(_arrival(), now=7.0) == []
    assert path.feed(_start(), now=8.0) == []
    assert path.flush(now=1e9) == []
