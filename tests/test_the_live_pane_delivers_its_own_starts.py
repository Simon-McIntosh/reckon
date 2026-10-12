"""The live follower delivers its own session's run start to the pane.

The row policy holds a start back as observer context, delivering one only for
the pane's own session's runs and only when it is told which session that is.
Two things therefore have to be true on the way through the follower's own row
path: the path must carry the pane's session, and the transition it reads must
name the lane and the time budget as well as the run, node and model. Either
one missing and the start the pane's second reader waits for never arrives.

The events here are built by the producer's own transition builder, not by
hand, so a case exercises the same row a live follower reads rather than a
second opinion of its shape. The clock is a stub, so a case states the instant
each row arrives and a hold's window is measured rather than waited out.
"""

from __future__ import annotations

from reckon.crew import recovery_watch
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


def test_the_live_follower_delivers_its_own_start_naming_all_five_fields() -> None:
    """One dispatched -> working row, naming run, node, lane, model and budget."""
    path = follower_row_path(session=SESSION)
    assert path.feed(_baseline(_snapshot("dispatched")), now=0.0) == []
    printed = path.feed(
        _transition(_snapshot("working"), "dispatched", "working"), now=5.0
    )
    assert len(printed) == 1, printed
    row = printed[0]
    assert (row.get("from_state"), row.get("to_state")) == ("dispatched", "working")
    for field in ("run_id", "node", "lane", "model", "time_budget"):
        assert row.get(field), (field, row)
    assert row["run_id"] == RUN
    assert row["node"] == "claude-1"
    assert row["lane"] == "local"
    assert row["model"] == "deepseek-v4.1-flash"
    assert row["time_budget"] == "30m"
    assert path.flush(now=1e9) == []


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
