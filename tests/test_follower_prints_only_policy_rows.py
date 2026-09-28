"""The follower prints only the rows the signal audit's policy keeps.

The pane is a reading surface for a coordinator, so a row is worth its space
only when it tells a reader something the pane does not already say. The policy
that decides that keeps four classes: a coordinator row prints with its plain
reason, an observer row prints unmarked, a same-state rewrite moves the counter
block alone, and a row that opens a noise pair is held for the pair's window so
a flicker never reaches the pane.

Every hold is paired here with its real counterpart, because a rule that drops
noise is only correct if it still prints the event that merely resembles it.
"""

from __future__ import annotations

from reckon.crew import ticker


def _row(run_id: str, from_state: object, to_state: object, **extra: object):
    row = {"run_id": run_id, "from_state": from_state, "to_state": to_state}
    row.update(extra)
    return row


def _window(kind: tuple[object, object]) -> float:
    return ticker.NOISE_PAIRS[kind][1]


# --- the held pairs print nothing -----------------------------------------


def test_launch_flicker_pair_prints_nothing():
    policy = ticker.RowPolicy()
    policy.seed("run-a", "working")
    assert policy.feed(_row("run-a", "dispatched", "abandoned"), now=1000.0) == []
    assert policy.feed(_row("run-a", "abandoned", "working"), now=1005.0) == []
    assert policy.flush(now=1e12) == []


def test_exit_phantom_pair_prints_nothing():
    policy = ticker.RowPolicy()
    policy.seed("run-b", "working")
    assert policy.feed(_row("run-b", "working", "blocked"), now=2000.0) == []
    assert (
        policy.feed(_row("run-b", "blocked", "completed_unpromoted"), now=2010.0) == []
    )
    assert policy.flush(now=1e12) == []


def test_stall_flap_pair_prints_nothing():
    policy = ticker.RowPolicy()
    policy.seed("run-c", "working")
    assert policy.feed(_row("run-c", "working", "stalled"), now=3000.0) == []
    assert policy.feed(_row("run-c", "stalled", "working"), now=3005.0) == []
    assert policy.flush(now=1e12) == []


# --- each pair's real counterpart still prints ----------------------------


def test_abandonment_that_does_not_revert_prints_after_its_window():
    policy = ticker.RowPolicy()
    opener = _row("run-d", "dispatched", "abandoned")
    assert policy.feed(opener, now=1000.0) == []
    window = _window(("dispatched", "abandoned"))
    assert policy.flush(now=1000.0 + window - 1.0) == []
    assert policy.flush(now=1000.0 + window) == [opener]


def test_stall_that_does_not_recover_prints_after_its_window():
    policy = ticker.RowPolicy()
    opener = _row("run-e", "working", "stalled")
    assert policy.feed(opener, now=1000.0) == []
    window = _window(("working", "stalled"))
    assert policy.flush(now=1000.0 + window) == [opener]


def test_blocked_run_whose_worker_did_not_survive_prints_after_its_window():
    policy = ticker.RowPolicy()
    opener = _row("run-f", "working", "blocked", reason="worker gone")
    assert policy.feed(opener, now=1000.0) == []
    window = _window(("working", "blocked"))
    assert policy.flush(now=1000.0 + window) == [opener]


def test_unresolved_opener_prints_when_a_later_row_needs_the_pane():
    """A held opener cannot be silently swallowed by a rule applied after it."""
    policy = ticker.RowPolicy()
    opener = _row("run-g", "working", "blocked")
    assert policy.feed(opener, now=1000.0) == []
    late = _row("run-g", "blocked", "complete")
    printed = policy.feed(late, now=1005.0)
    assert printed == [opener, late]


# --- the three kept classes -----------------------------------------------


def test_same_state_rewrite_moves_only_the_counter():
    assert ticker.transition_class("working", "working") == ticker.ROW_COUNTER
    policy = ticker.RowPolicy()
    assert policy.feed(_row("run-h", "working", "working"), now=1.0) == []
    # The counter path still advances the memory, so a real move afterwards is
    # news rather than a repeat of what the counter was counting.
    move = _row("run-h", "working", "complete")
    assert policy.feed(move, now=2.0) == [move]


def test_every_coordinator_kind_prints_with_its_plain_reason():
    for kind in sorted(ticker.COORDINATOR_KINDS, key=str):
        assert ticker.transition_class(kind[0], kind[1]) == ticker.ROW_COORDINATOR
        policy = ticker.RowPolicy()
        row = _row("run-i", kind[0], kind[1], reason="landed clean")
        printed = policy.feed(row, now=1.0)
        if not printed:
            # This kind also opens a noise pair, so the hold takes it first and
            # it prints for real once the pair's window closes without it.
            printed = policy.flush(now=1.0 + _window(kind))
        assert printed == [row], kind
        assert printed[0]["reason"] == "landed clean"


def test_observer_rows_print_unmarked():
    assert ticker.transition_class("unreadable", "working") == ticker.ROW_OBSERVER
    policy = ticker.RowPolicy()
    row = _row("run-j", "unreadable", "working")
    printed = policy.feed(row, now=1.0)
    assert printed == [row]
    assert printed[0] is row


def test_re_derived_baseline_row_is_silent():
    policy = ticker.RowPolicy()
    policy.seed("run-k", "working")
    assert policy.feed(_row("run-k", "dispatched", "working"), now=1.0) == []


def test_declared_wait_prints_even_when_its_state_word_repeats():
    policy = ticker.RowPolicy()
    policy.seed("run-l", "waiting")
    row = _row(
        "run-l",
        "working",
        "waiting",
        wait_condition_state="pending",
        wait_overdue=False,
    )
    assert policy.feed(row, now=1.0) == [row]


# --- the replay answers the same way the pane does ------------------------


def test_replay_prints_fewer_rows_than_it_fed_and_keeps_every_kind():
    events = []
    for index, run in enumerate([f"run-{n}" for n in range(40)]):
        events.append(_row(run, "dispatched", "abandoned", observed_at=_stamp(index)))
        events.append(_row(run, "abandoned", "working", observed_at=_stamp(index)))
        events.append(_row(run, "working", "stalled", observed_at=_stamp(index + 1)))
        events.append(_row(run, "stalled", "working", observed_at=_stamp(index + 1)))
        events.append(_row(run, "working", "complete", observed_at=_stamp(index + 2)))
    printed = ticker.replay_row_policy(events)
    assert len(printed) < len(events)
    kinds = {(row.get("from_state"), row.get("to_state")) for row in printed}
    assert ("working", "complete") in kinds
    assert ("dispatched", "abandoned") not in kinds
    assert ("working", "stalled") not in kinds


def _stamp(minute: int) -> str:
    return f"2026-09-25T00:{minute:02d}:00Z"


def test_pair_definitions_match_the_audit():
    """The three pairs, their resolutions and their windows are the audited ones."""
    assert set(ticker.NOISE_PAIRS) == {
        ("dispatched", "abandoned"),
        ("working", "blocked"),
        ("working", "stalled"),
    }
    assert ticker.NOISE_PAIRS[("dispatched", "abandoned")][0] == (
        "abandoned",
        "working",
    )
    assert ticker.NOISE_PAIRS[("working", "blocked")][0] == (
        "blocked",
        "completed_unpromoted",
    )
    assert ticker.NOISE_PAIRS[("working", "stalled")][0] == ("stalled", "working")
    resolutions = frozenset(resolution for resolution, _ in ticker.NOISE_PAIRS.values())
    assert resolutions == ticker.NOISE_RESOLUTIONS
