"""RowPolicy holds observer context back on the coordinator's pane.

The pane the coordinator reads should carry the rows that ask for something.
A row whose kind ``transition_class`` classes as observer context asks for
nothing — a start, a launch's ``dispatched -> working``, a terminal echo, a
bare re-announcement — so the policy withholds it, with three exceptions that
print: a recovery from a duty already shown, an unexplained end, and the
actionable resolution of a held noise pair.

Each case below feeds one instance the measured sequence and states the rows
the pane receives. The clock is a stub, so a case states the instant each row
arrives and every window is measured rather than waited out.
"""

from __future__ import annotations

from reckon.crew import ticker

RUN = "r-one"


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


def _baseline(to_state, **extra):
    return _row(None, to_state, event="baseline", **extra)


def _kinds(rows):
    return [(row.get("from_state"), row.get("to_state")) for row in rows]


def _drive(sequence, *, show_observer=False, flush_at=1e9):
    """Feed a ``(event, now)`` sequence and collect every printed row."""
    policy = ticker.RowPolicy(show_observer=show_observer)
    printed = []
    for event, now in sequence:
        printed.extend(policy.feed(event, now=now))
    if flush_at is not None:
        printed.extend(policy.flush(now=flush_at))
    return printed


def test_a_launch_prints_only_its_completion() -> None:
    """arrival, ``dispatched -> working``, ``working -> complete``, ``complete -> recorded``.

    A launch's arrival resolves into the observer ``dispatched -> working`` and
    prints nothing; the completed coordinator row prints once; its terminal echo
    is held back. One row reaches the pane, and it is the completion.
    """
    printed = _drive(
        [
            (_baseline("dispatched"), 0.0),
            (_row("dispatched", "working"), 1.0),
            (_row("working", "complete"), 2.0),
            (_row("complete", "recorded"), 3.0),
        ]
    )
    assert len(printed) == 1, printed
    assert _kinds(printed) == [("working", "complete")]


def test_a_settle_chain_prints_once_as_its_latest_row() -> None:
    """``working -> exited-unfinished -> blocked`` two seconds apart prints one row."""
    printed = _drive(
        [
            (
                _row(
                    "working",
                    "exited-unfinished",
                    recovery_classification="exited-unfinished",
                ),
                0.0,
            ),
            (
                _row("exited-unfinished", "blocked", recovery_classification="blocked"),
                2.0,
            ),
        ]
    )
    assert len(printed) == 1, printed
    assert printed[0]["to_state"] == "blocked", printed


def test_a_printed_duty_withholds_its_transient_recovery_row() -> None:
    """A blocked row printed at its window, then ``blocked -> working``.

    With the exit phantom's window passed, the blocked opener is a real event
    and prints late; the return to working then reads as a recovery and prints.
    """
    policy = ticker.RowPolicy()
    assert policy.feed(_row("working", "blocked"), now=0.0) == []
    printed = policy.flush(now=ticker.NOISE_PAIRS[("working", "blocked")][1])
    assert _kinds(printed) == [("working", "blocked")], printed
    recovery = policy.feed(_row("blocked", "working"), now=301.0)
    assert _kinds(recovery) == [("blocked", "working")], recovery


def test_a_refused_dispatch_is_never_unexplained() -> None:
    """An arrival then ``dispatched -> withdrawn`` twice prints nothing.

    A launch withdrawn straight from dispatched within the arrival window is the
    coordinator's own refused dispatch, which the dispatch call already
    reported, so neither the withdrawal nor its repeat reaches the pane.
    """
    policy = ticker.RowPolicy()
    assert policy.feed(_baseline("dispatched"), now=0.0) == []
    assert policy.feed(_row("dispatched", "withdrawn"), now=1.0) == []
    assert policy.feed(_row("dispatched", "withdrawn"), now=2.0) == []
    assert policy.flush(now=1e9) == []


def test_an_actionable_resolution_prints_as_the_net_change() -> None:
    """``working -> blocked`` then ``blocked -> completed_unpromoted`` two seconds apart.

    The run finished and waits for promotion behind a held block; the resolution
    lands in an actionable state, so the pair prints one row reading the net
    change under the opener's left side.
    """
    policy = ticker.RowPolicy()
    assert policy.feed(_row("working", "blocked"), now=0.0) == []
    printed = policy.feed(
        _row(
            "blocked",
            "completed_unpromoted",
            recovery_classification="completed_unpromoted",
        ),
        now=2.0,
    )
    assert _kinds(printed) == [("working", "completed_unpromoted")], printed


def test_a_pair_wider_than_its_window_prints_both_sides() -> None:
    """The same pair 400 s apart prints the block, then the promotion."""
    policy = ticker.RowPolicy()
    assert policy.feed(_row("working", "blocked"), now=0.0) == []
    opener = policy.feed(
        _row(
            "blocked",
            "completed_unpromoted",
            recovery_classification="completed_unpromoted",
        ),
        now=400.0,
    )
    assert _kinds(opener) == [("working", "blocked")], opener
    tail = policy.flush(now=1e9)
    assert _kinds(tail) == [("blocked", "completed_unpromoted")], tail


def test_a_stall_chain_wider_than_its_window_prints_both_sides() -> None:
    """``working -> stalled`` then ``stalled -> working`` 400 s apart."""
    policy = ticker.RowPolicy()
    assert policy.feed(_row("working", "stalled"), now=0.0) == []
    printed = policy.feed(
        _row("stalled", "working", recovery_classification="observe"), now=400.0
    )
    assert _kinds(printed) == [("working", "stalled"), ("stalled", "working")], printed
    assert policy.flush(now=1e9) == []


def test_a_run_that_never_asked_reaches_the_pane_when_it_ends() -> None:
    """A working run whose next row is ``working -> discarded`` prints the discard.

    The run never produced a coordinator row, so its end is the only notice the
    reader gets that it stopped.
    """
    policy = ticker.RowPolicy()
    assert policy.feed(_baseline("working"), now=0.0) == []
    printed = policy.feed(_row("working", "discarded"), now=1.0)
    assert _kinds(printed) == [("working", "discarded")], printed


def test_a_fast_changing_run_prints_within_the_cap() -> None:
    """A run changing state every ten seconds prints within two minutes.

    The settle window would hold a row a later coordinator row supersedes; the
    cap bounds the wait from the chain's first held row, so the pane is told
    within two minutes of the run's first change.
    """
    policy = ticker.RowPolicy()
    printed = []
    # The chain alternates between two action states ten seconds apart, so no
    # row settles: only the cap ends the wait.
    for index in range(40):
        now = index * 10.0
        to_state = "wait-aged" if index % 2 == 0 else "blocked"
        from_state = "blocked" if index % 2 == 0 else "wait-aged"
        printed.extend(
            policy.feed(
                _row(from_state, to_state, recovery_classification=to_state), now=now
            )
        )
        if printed:
            break
    assert printed, "a fast-changing run never reached the pane"
    elapsed = index * 10.0
    assert elapsed <= ticker.SETTLE_CAP, (elapsed, printed)


def test_a_shown_observer_pane_prints_today_rows() -> None:
    """With observer context shown, the launch sequence prints every row."""
    printed = _drive(
        [
            (_baseline("dispatched"), 0.0),
            (_row("dispatched", "working"), 1.0),
            (_row("working", "complete"), 2.0),
            (_row("complete", "recorded"), 3.0),
        ],
        show_observer=True,
    )
    assert _kinds(printed) == [
        ("dispatched", "working"),
        ("working", "complete"),
        ("complete", "recorded"),
    ], printed
