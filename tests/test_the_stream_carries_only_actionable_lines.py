"""The Monitor stream carries only the six kinds that ask the coordinator to act.

A line the follower prints reaches its coordinator through the session host as a
notification that starts a turn, so the stream carries a row only when it names
something only the coordinator can do next: a run of this session delivered and
unpromoted, a run halted, a queued run refused at launch, a review finding that
needs the coordinator, a limit reset, or the follower's own end. Everything else
— a launch, a ``working``/``waiting`` transition, a ``landed`` or ``dropped`` row
the coordinator's own merge or discard caused, a re-arm baseline and a reload
notice — stays in ``crew view`` and the pane's history and prints nothing.

The rule is stated once, in :func:`reckon.crew.ticker.stream_carries`, and the
row policy applies it to every row it would release. A case drives the follower's
own row path over a stream holding one event of every kind and counts the lines.
"""

from __future__ import annotations

from reckon.crew import ticker
from reckon.crew_follow_commands import follower_row_path

SESSION = "s-own"
PEER = "s-peer"
RUN = "r-own"
PEER_RUN = "r-peer"

# The event name the standing needs-you summary line was written under, spelled
# here as a literal rather than read from the module: the follower no longer
# writes it, and a restored emitter is what the negative control reinstates.
NEEDED_YOU_EVENT = "follow.needs-you"

# A moment far past every hold's window, so a row a hold withheld is released.
FAR_FUTURE = 10_000.0


def _row(run_id: str, from_state: str | None, to_state: str, **overrides):
    row = {
        "event": "transition",
        "run_id": run_id,
        "node": "own-node",
        "session": SESSION,
        "from_state": from_state,
        "to_state": to_state,
    }
    row.update(overrides)
    return row


def _lines(events) -> list[dict]:
    """Drive the follower's own row path over one stream and return its lines."""
    path = follower_row_path(
        session=SESSION,
        lift_claim=lambda lift_id, transition: True,
    )
    printed = []
    now = 1_000.0
    for event in events:
        # The follower polls between the rows it reads, so a hold whose window
        # closes in the gap releases there rather than at the end.
        printed.extend(path.flush(now=now))
        printed.extend(path.feed(event, now=now))
        now += 250.0
    printed.extend(path.flush(now=FAR_FUTURE))
    return printed


def _words(printed) -> list[str]:
    return [ticker._pane_word(row) for row in printed]


def test_only_the_actionable_kinds_reach_the_stream():
    """One event of every kind; a line for the kinds that ask for the coordinator."""
    printed = _lines(
        [
            # A launch: the arrival and the start. The arrival resolves into
            # the launch row the pane draws, and the start opens the attempt.
            _row(RUN, None, "dispatched"),
            _row(RUN, "dispatched", "working"),
            # A move to waiting is an audited coordinator kind: the run needs the
            # coordinator, whatever the word says. The resume out of it asks
            # nothing and leaves.
            _row(RUN, "working", "waiting"),
            _row(RUN, "waiting", "working"),
            # A re-arm baseline replaying a run the pane already shows.
            _row(RUN, "working", "working", event="baseline"),
            # Delivered and unpromoted: the coordinator owes a promotion.
            _row(RUN, "working", "completed_unpromoted"),
            # The coordinator's own merge, which it just caused itself.
            _row(RUN, "completed_unpromoted", "promoted"),
        ]
    )

    words = _words(printed)
    # The launch, the waiting row, the delivery, and the landing the coordinator
    # caused itself after the pane drew the delivery.
    assert words == ["working", "waiting", "delivered", "landed"], words


def test_a_halted_run_reaches_the_stream_and_its_recovery_does_not():
    """A halt is a duty; the working/waiting churn around it is not."""
    printed = _lines(
        [
            _row(RUN, "working", "blocked"),
            _row(RUN, "blocked", "working"),
        ]
    )
    assert _words(printed) == ["halted", "working"], _words(printed)


def test_a_delivered_run_prints_its_row_once_and_no_summary_line():
    """A delivered run draws its own row once and never a ``needs you:`` line."""
    printed = _lines(
        [
            _row(RUN, "working", "completed_unpromoted"),
            # A re-derivation of the same word is not news a second time.
            _row(RUN, "working", "completed_unpromoted", event="baseline"),
        ]
    )
    assert len(printed) == 1, _words(printed)
    assert _words(printed) == ["delivered"], _words(printed)
    assert all(row.get("event") != NEEDED_YOU_EVENT for row in printed)


def test_a_limit_reset_reaches_the_stream_once():
    """The reset is one of the six, and it is drawn once per event."""
    reset = {
        "event": "transition",
        "run_id": None,
        "node": "claude-orchestrators",
        "session": SESSION,
        "from_state": None,
        "to_state": ticker.LIMIT_RESET,
        "lift_id": "reset-1",
    }
    printed = _lines([reset])
    assert len(printed) == 1, printed
    assert str(printed[0].get("to_state")) == ticker.LIMIT_RESET


def test_the_rule_names_the_words_the_stream_carries():
    """The policy and the summaries read one rule, stated in ``ticker``."""
    assert frozenset({"delivered", "halted", "in review", "seat moved"}) == (
        ticker.CARRIED_PANE_WORDS
    )
    assert ticker.stream_carries({"to_state": "completed_unpromoted"})
    assert ticker.stream_carries({"to_state": "blocked"})
    assert ticker.stream_carries({"to_state": ticker.LIMIT_RESET})
    for silent in ("working", "waiting", "promoted", "recorded", "withdrawn"):
        # A landing is not carried on its word: the row policy marks the one it
        # judged news, and this rule stays a function of the row alone.
        assert not ticker.stream_carries({"to_state": silent}), silent
    launched = {"to_state": "working", ticker.LAUNCH_FIELD: True}
    assert ticker.stream_carries(launched)
    landing = {"to_state": "recorded", ticker.LANDING_FIELD: True}
    assert ticker.stream_carries(landing)


def test_the_follower_carries_no_rule_of_its_own():
    """The follower states no second rule, so the two cannot disagree."""
    # The standing summary line is gone from the follower entirely.
    import reckon.crew_follow_commands as follow
    from reckon.crew import needs_you

    assert not hasattr(follow, "_needs_you_events")
    # No composer survives anywhere: the pane carries counts, and the runs that
    # need action are named by the obligations list.
    assert not hasattr(needs_you, "needs_you_line")
