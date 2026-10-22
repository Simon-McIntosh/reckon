"""A run's start reaches the view and never the Monitor stream.

A run beginning work asks the coordinator for nothing, so a launch and the
``dispatched -> working`` start that follows it are not lines the stream
carries: the pane's own session's start is drawn in the view and in the pane's
history and prints no row, for the own session and for a peer's alike, on a
re-announcement and on a replay.

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
    """The own session's start renders in the view and reaches the stream never.

    A run beginning work asks the coordinator for nothing, so the start is not a
    line the Monitor stream carries, however the launch and the start are fed.
    The pane's view still draws it, naming the node and the model a reader
    follows the work by.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    assert path.feed(_arrival(), now=0.0) == []
    assert path.feed(_start(), now=5.0) == []
    assert path.flush(now=1e9) == []

    drawn = ticker.Ticker().render(_start())
    assert "working" in drawn, drawn
    assert "claude-1" in drawn, drawn
    assert "deepseek" in drawn, drawn


def test_a_panes_own_start_prints_with_no_held_arrival() -> None:
    """A start reaches no pane row whether or not its arrival was held.

    The hold decides nothing here: the start is not carried at all, so feeding
    it with no arrival before it and feeding it after a held arrival are the
    same silence.
    """
    bare = ticker.PaneRowPath(own_session=SESSION)
    assert bare.feed(_start(), now=0.0) == []
    assert bare.flush(now=1e9) == []

    held = ticker.PaneRowPath(own_session=SESSION)
    assert held.feed(_arrival(), now=0.0) == []
    assert held.feed(_start(), now=5.0) == []
    assert held.flush(now=1e9) == []


def test_a_peer_sessions_start_delivers_nothing() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)
    peer = _start(session=PEER, run_id="r-peer-1")
    assert path.feed(_arrival(session=PEER, run_id="r-peer-1"), now=0.0) == []
    assert path.feed(peer, now=5.0) == []
    assert path.flush(now=1e9) == []


def test_a_reannouncement_or_replay_of_the_same_start_delivers_nothing() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)
    assert path.feed(_arrival(), now=0.0) == []
    assert path.feed(_start(), now=5.0) == []
    # A replay of the identical start: the pane already shows the run working.
    assert path.feed(_start(), now=6.0) == []
    # A re-announcement re-derives the arrival and repeats the start.
    assert path.feed(_arrival(), now=7.0) == []
    assert path.feed(_start(), now=8.0) == []
    assert path.flush(now=1e9) == []
