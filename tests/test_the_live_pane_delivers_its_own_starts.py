"""A live follower's own start reaches the view and never the Monitor stream.

The follower reads the producer's own transition as a run leaves ``dispatched``
and begins working, and the transition names the run, node, lane, model and
time budget — the fields the view draws the work by. It is not a line the
stream carries, for the pane's own session and for a peer's alike, because a
run beginning work asks the coordinator for nothing.

The events here are built by the producer's own transition builder, not by
hand, so a case exercises the same row a live follower reads rather than a
second opinion of its shape. The clock is a stub, so a case states the instant
each event arrives and a hold's window is measured rather than waited out.
"""

from __future__ import annotations

from reckon.crew import recovery_watch, ticker
from reckon.crew_follow_commands import follower_row_path

SESSION = "s-own"
PEER = "s-peer"
RUN = "r-own-1"

COUNTS = {"working": 1, "blocked": 0, "unpromoted": 0}


def _snapshot(state: str, **overrides):
    row = {
        "run_id": RUN,
        "node": "claude-1",
        "session": SESSION,
        "project": "reckon",
        "lane": "local",
        "backend": "clive",
        "model": "deepseek-v4.1-flash",
        "effort": "high",
        "alias": "dsv4.1-flash",
        "role": "implement",
        "time_budget": "30m",
        "state": state,
        "classification": "working",
        "recovery_classification": "working",
        "detail": "",
    }
    row.update(overrides)
    return row


def _transition(snapshot, previous, current):
    return recovery_watch._watch_transition(
        "reckon",
        kind="transition",
        snapshot=snapshot,
        previous=previous,
        current=current,
        counts=COUNTS,
        spend_runs=[],
        rate_statuses={},
    )


def _baseline(snapshot):
    return recovery_watch._watch_transition(
        "reckon",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=COUNTS,
        spend_runs=[],
        rate_statuses={},
    )


def test_the_live_follower_keeps_its_own_start_out_of_the_stream() -> None:
    """The live follower's own start prints one launch row, and keeps its fields.

    The producer's transition names run, node, lane, model and budget, and the
    pane's own row path reads it into the launch the pane draws. The transition
    still carries every field the view draws the work by, and the launch row it
    is released as carries them too.
    """
    path = follower_row_path(session=SESSION)
    assert path.feed(_baseline(_snapshot("dispatched")), now=0.0) == []
    start = _transition(_snapshot("working"), "dispatched", "working")
    printed = path.feed(start, now=5.0)
    assert [ticker._pane_word(row) for row in printed] == ["working"], printed
    assert printed[0].get("run_id") == RUN, printed[0]
    assert printed[0].get("lane") == "local", printed[0]
    assert path.flush(now=1e9) == []

    for field in ("run_id", "node", "lane", "model", "time_budget"):
        assert start.get(field), (field, start)
    assert start["run_id"] == RUN
    assert start["node"] == "claude-1"
    assert start["lane"] == "local"
    assert start["model"] == "deepseek-v4.1-flash"
    assert start["time_budget"] == "30m"
    assert start["to_state"] == "working"


def test_a_peer_sessions_start_delivers_nothing_through_the_live_follower() -> None:
    """The pane is silent about a peer session's run: it is not this reader's work."""
    path = follower_row_path(session=SESSION)
    peer = _snapshot("dispatched", session=PEER, run_id="r-peer-1")
    assert path.feed(_baseline(peer), now=0.0) == []
    assert path.feed(_transition(peer, "dispatched", "working"), now=5.0) == []
    assert path.flush(now=1e9) == []


def test_the_transition_carries_the_lane_and_time_budget_as_facts() -> None:
    """The producer's own transition names the lane and the declared allowance.

    This is the fact the pane's start row is built from: without it on the
    transition, the row could carry neither even with the session wired.
    """
    event = _transition(_snapshot("working"), "dispatched", "working")
    assert event["lane"] == "local"
    assert event["time_budget"] == "30m"
