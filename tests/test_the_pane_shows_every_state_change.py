"""The pane prints each real state change of the work a coordinator oversees.

A run's launch, a resume's new attempt, a review starting on the run it reads,
a landing and a drop are state changes a coordinator follows, so the Monitor
stream carries each of them once. The noise rules stay: a launch flicker inside
the arrival window, a reload baseline, a replayed start, a repeated landing and
another session's arrival print nothing.

Every sequence states the instant each event arrives, so a hold's window is
measured rather than waited out.
"""

from __future__ import annotations

import json

from reckon.crew import recovery_watch, ticker

SESSION = "s-own"
PEER = "s-peer"
IMPL = "r-impl-1"
IMPL_NODE = "impl-node"
REVIEW = "r-review-1"
REVIEW_NODE = f"review-of-{IMPL}-0123456789ab"


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
    """A review run's row, naming the run it reads as the producer now does."""
    return _row(
        REVIEW,
        from_state,
        to_state,
        reviewed_run_id=IMPL,
        reviewed_node=IMPL_NODE,
        **overrides,
    )


def _print(sequence, *, own_session=SESSION):
    """Feed timed events through one pane and return every row it printed."""
    path = ticker.PaneRowPath(own_session=own_session)
    printed = []
    for moment, event in sequence:
        printed.extend(path.feed(event, now=moment))
    printed.extend(path.flush(now=1e9))
    return printed


def _words(rows):
    return [ticker._pane_word(row) for row in rows]


def test_an_implement_run_and_its_review_print_each_state_change_once() -> None:
    """The launch, delivery, in-review, review delivery and landing, in order."""
    printed = _print(
        [
            (0.0, _row(IMPL, None, "dispatched")),
            (10.0, _row(IMPL, "dispatched", "starting")),
            (20.0, _row(IMPL, "starting", "working")),
            (30.0, _row(IMPL, "working", "complete")),
            (70.0, _review_row(None, "dispatched")),
            (80.0, _review_row("dispatched", "starting")),
            (90.0, _review_row("starting", "working")),
            (100.0, _review_row("working", "complete")),
            (140.0, _row(IMPL, "complete", "recorded")),
        ]
    )

    assert _words(printed) == [
        "working",
        "delivered",
        "in review",
        "delivered",
        "landed",
    ], [(row.get("run_id"), ticker._pane_word(row)) for row in printed]
    # The launch, the delivery and the landing belong to the implement run;
    # the review's own launch is drawn as the implement run's in-review row and
    # never under the review run's name.
    assert [row.get("run_id") for row in printed] == [
        IMPL,
        IMPL,
        IMPL,
        REVIEW,
        IMPL,
    ], [row.get("run_id") for row in printed]

    in_review = printed[2]
    assert in_review.get("node") == IMPL_NODE, in_review.get("node")
    assert in_review.get("review_run_id") == REVIEW, in_review.get("review_run_id")


def test_the_in_review_row_renders_on_the_reviewed_run() -> None:
    """The row reads ``delivered -> in review`` under the reviewed run's node."""
    printed = _print(
        [
            (0.0, _row(IMPL, None, "dispatched")),
            (10.0, _row(IMPL, "dispatched", "working")),
            (30.0, _row(IMPL, "working", "complete")),
            (70.0, _review_row(None, "dispatched")),
            (80.0, _review_row("dispatched", "starting")),
            (90.0, _review_row("starting", "working")),
        ]
    )
    delivery = next(row for row in printed if ticker._pane_word(row) == "delivered")
    in_review = next(row for row in printed if ticker._pane_word(row) == "in review")

    grid = ticker.Ticker()
    grid.render(delivery)
    drawn = grid.render(in_review)

    assert "in review" in drawn, drawn
    assert "delivered" in drawn, drawn
    assert IMPL_NODE in drawn, drawn


def test_a_discarded_run_prints_its_launch_and_one_dropped_row() -> None:
    printed = _print(
        [
            (0.0, _row("r-drop", None, "dispatched")),
            (10.0, _row("r-drop", "dispatched", "starting")),
            (20.0, _row("r-drop", "starting", "working")),
            (30.0, _row("r-drop", "working", "discarded")),
        ]
    )
    assert _words(printed) == ["working", "dropped"], _words(printed)


def test_a_resumed_run_prints_the_new_attempts_launch_once() -> None:
    """A stop and a resume is a second launch, and a replay of it adds nothing."""
    printed = _print(
        [
            (0.0, _row("r-fresh", None, "dispatched")),
            (10.0, _row("r-fresh", "dispatched", "working")),
            (30.0, _row("r-fresh", "working", "blocked")),
            (40.0, _row("r-fresh", "blocked", "working")),
            (45.0, _row("r-fresh", "blocked", "working")),
        ]
    )
    launches = [row for row in printed if row.get(ticker.LAUNCH_FIELD)]
    assert len(launches) == 2, _words(printed)
    assert [row.get("from_state") for row in launches] == ["dispatched", "blocked"]


def test_a_flicker_and_a_reload_baseline_print_nothing() -> None:
    """A working/running flap and an attach baseline are not state changes."""
    flicker = _print(
        [
            (0.0, _row("r-flap", None, "dispatched")),
            (10.0, _row("r-flap", "dispatched", "working")),
            (20.0, _row("r-flap", "working", "running")),
            (30.0, _row("r-flap", "running", "working")),
        ]
    )
    assert _words(flicker) == ["working"], _words(flicker)

    baseline = _print(
        [
            (
                0.0,
                {
                    "event": "baseline",
                    "run_id": "r-attach",
                    "node": IMPL_NODE,
                    "session": SESSION,
                    "from_state": None,
                    "to_state": "working",
                },
            ),
            (
                10.0,
                {
                    "event": "baseline",
                    "run_id": "r-attach-2",
                    "node": IMPL_NODE,
                    "session": SESSION,
                    "from_state": None,
                    "to_state": "dispatched",
                },
            ),
        ]
    )
    assert baseline == [], _words(baseline)


def test_another_sessions_arrival_prints_no_launch_on_any_release_path() -> None:
    """A peer's arrival is no launch whether it resolves, expires or goes stale."""
    resolved = _print(
        [
            (0.0, _row("r-peer", None, "dispatched", session=PEER, node="peer-node")),
            (
                10.0,
                _row("r-peer", "dispatched", "working", session=PEER, node="peer-node"),
            ),
        ]
    )
    assert resolved == [], _words(resolved)

    expired = _print(
        [(0.0, _row("r-peer", None, "dispatched", session=PEER, node="peer-node"))]
    )
    assert expired == [], _words(expired)

    stale = _print(
        [
            (0.0, _row("r-peer", None, "dispatched", session=PEER, node="peer-node")),
            (
                10.0,
                _row("r-peer", "starting", "running", session=PEER, node="peer-node"),
            ),
        ]
    )
    assert stale == [], _words(stale)

    # The same arrival from the pane's own session is a launch, so the arms
    # above measure the session test and not a dead release path.
    own = _print([(0.0, _row("r-own", None, "dispatched"))])
    assert _words(own) == ["working"], _words(own)


def test_a_repeated_landing_prints_one_row() -> None:
    printed = _print(
        [
            (0.0, _row("r-land", None, "dispatched")),
            (10.0, _row("r-land", "dispatched", "working")),
            (30.0, _row("r-land", "working", "complete")),
            (80.0, _row("r-land", "complete", "recorded")),
            (90.0, _row("r-land", "complete", "recorded")),
        ]
    )
    assert _words(printed) == ["working", "delivered", "landed"], _words(printed)


def test_a_review_event_names_the_reviewed_run_and_its_node(tmp_path, monkeypatch):
    """The producer puts the reviewed run's id and node on a review run's event."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    live = tmp_path / "crew" / "live"
    live.mkdir(parents=True)
    (live / f"{IMPL}.json").write_text(
        json.dumps(
            {
                "run_id": IMPL,
                "project": "reckon",
                "node": {"id": IMPL_NODE},
            }
        ),
        encoding="utf-8",
    )

    snapshot = {
        "run_id": REVIEW,
        "node": REVIEW_NODE,
        "session": SESSION,
        "role": "review",
        "project": "reckon",
        "state": "working",
        "classification": "working",
        "recovery_classification": "working",
        "process_alive": True,
    }
    event = recovery_watch._watch_transition(
        "reckon",
        kind="transition",
        snapshot=snapshot,
        previous="starting",
        current="working",
        counts={"working": 1, "blocked": 0, "unpromoted": 0},
    )

    assert event.get("reviewed_run_id") == IMPL, event.get("reviewed_run_id")
    assert event.get("reviewed_node") == IMPL_NODE, event.get("reviewed_node")

    # The row policy draws that event as the reviewed run's in-review row.
    printed = _print(
        [
            (0.0, _row(IMPL, None, "dispatched")),
            (10.0, _row(IMPL, "dispatched", "working")),
            (30.0, _row(IMPL, "working", "complete")),
            (60.0, event),
        ]
    )
    in_review = [row for row in printed if ticker._pane_word(row) == "in review"]
    assert len(in_review) == 1, _words(printed)
    assert in_review[0].get("run_id") == IMPL, in_review[0].get("run_id")
    assert in_review[0].get("node") == IMPL_NODE, in_review[0].get("node")
