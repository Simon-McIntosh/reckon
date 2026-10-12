"""A run's start is one row on its own session's pane.

A session with work in flight used to show nothing between a run's launch and
its end: the start is observer context, which the coordinator's pane holds back
because it asks the coordinator for nothing. The pane has a second reader — the
person watching — who wants to see the work while it runs, so the pane's own
session's start is delivered. It is delivered for the own session alone, once,
and never for a peer's run, a re-announcement or a replay.

The clock is a stub, so a case states the instant each row arrives and a hold's
window is measured rather than waited out.
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


def _kinds(rows):
    return [(row.get("from_state"), row.get("to_state")) for row in rows]


def test_a_panes_own_start_is_one_row_carrying_its_fields() -> None:
    """The own session's start prints once, naming run, node, lane, model, budget.

    The launch is held as an arrival and collapses into the start, so exactly one
    row reaches the pane and it reads ``dispatched -> working``.
    """
    path = ticker.PaneRowPath(own_session=SESSION)
    assert path.feed(_arrival(), now=0.0) == []
    printed = path.feed(_start(), now=5.0)
    assert len(printed) == 1, printed
    assert _kinds(printed) == [("dispatched", "working")]
    row = printed[0]
    for field in ("run_id", "node", "lane", "model", "time_budget"):
        assert row.get(field), (field, row)
    assert row["run_id"] == RUN
    assert row["node"] == "claude-1"
    assert path.flush(now=1e9) == []


def test_a_panes_own_start_prints_with_no_held_arrival() -> None:
    """A start whose arrival hold is already gone prints directly, not held."""
    path = ticker.PaneRowPath(own_session=SESSION)
    printed = path.feed(_start(), now=0.0)
    assert len(printed) == 1, printed
    assert _kinds(printed) == [("dispatched", "working")]
    assert path.flush(now=1e9) == []


def test_a_peer_sessions_start_delivers_nothing() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)
    peer = _start(session=PEER, run_id="r-peer-1")
    assert path.feed(_arrival(session=PEER, run_id="r-peer-1"), now=0.0) == []
    assert path.feed(peer, now=5.0) == []
    assert path.flush(now=1e9) == []


def test_a_reannouncement_or_replay_of_the_same_start_delivers_nothing() -> None:
    path = ticker.PaneRowPath(own_session=SESSION)
    assert path.feed(_arrival(), now=0.0) == []
    assert len(path.feed(_start(), now=5.0)) == 1
    # A replay of the identical start: the pane already shows the run working.
    assert path.feed(_start(), now=6.0) == []
    # A re-announcement re-derives the arrival and repeats the start.
    assert path.feed(_arrival(), now=7.0) == []
    assert path.feed(_start(), now=8.0) == []
    assert path.flush(now=1e9) == []
