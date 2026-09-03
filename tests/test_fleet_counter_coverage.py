"""A fleet state cannot fall out of the counters.

The three summary figures a reader adds up have to add up. A state a snapshot
can carry but that belongs to no bucket silently vanishes from every total
while the line still prints: it happened once, when a state was added to the
fleet classifier, given a renderer column and a colour, and only later
reconciled into the bucket that decides what needs attention — with nothing in
between to catch the gap. These tests bind that relationship so a new state
fails by naming itself instead of by not being counted.

Two assertions, both about what ``_fleet_counts`` cannot be trusted to do on
its own. First, the buckets sum to the row count over any set of snapshots.
Second, every state the classifier can emit is a watched state, an actionable
state, or a delivered state — and the actionable bucket is derived from the
attention set, not kept beside it as a second list that can drift.

The emitted vocabulary is read out of the function that emits it rather than
maintained beside it: a hand-written list is exactly the second copy that made
the historical gap possible, so the tests derive the vocabulary from the code
and would include a new state automatically.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module


def _emitted_states() -> set[str]:
    """The states ``_watch_snapshot`` can assign, read from its source.

    The possible values are the literal ``state = "..."`` assignments, the
    manifest verdicts assigned from ``manifest_status``, the classifier's
    classifications reachable through the fallback assignment, and that
    fallback's literal ``"unknown"``. Reading them from the emitter is what
    makes a new state appear in these tests the moment it is added; a list
    written out here would have to stay in step on its own.
    """
    source = inspect.getsource(recovery._watch_snapshot)
    emitted = set(re.findall(r'state = "([a-z_]+)"', source))
    emitted |= {"complete", "blocked", "failed"}  # assigned from manifest_status
    emitted |= set(recovery.RECOVERY_CLASSES)  # classification via the fallback
    emitted |= {"unknown"}  # the fallback's last resort
    return emitted


def _fleet_of_every_state() -> dict[str, dict[str, str]]:
    """One snapshot per emitted state, keyed as ``_fleet_counts`` expects."""
    return {
        f"r-{state}": {"state": state, "run_id": f"r-{state}"}
        for state in sorted(_emitted_states())
    }


def test_counts_add_up_to_the_row_count_for_every_emitted_state() -> None:
    """Over any set of snapshots the three buckets sum to the rows.

    Work in progress, work needing the coordinator, and work delivered but
    unpromoted are the whole fleet. A snapshot in an emitted state that fell
    into none of them would make the sum drop below the row count, so the
    assertion fails naming the uncounted states rather than letting a number
    silently disagree with the lines it summarises.
    """
    # The fleet with one snapshot per emitted state, every bucket populated.
    fleet = _fleet_of_every_state()
    counts = recovery._fleet_counts(fleet)
    assert sum(counts.values()) == len(fleet), (
        f"uncounted fleet states: {sorted(_emitted_states() - _counted_states())}"
    )

    # A single row in each state: an uncounted state shows as a zero anyway,
    # so the per-row form makes the missing member name itself directly.
    for state in sorted(_emitted_states()):
        alone = {"r-1": {"state": state, "run_id": "r-1"}}
        assert sum(recovery._fleet_counts(alone).values()) == 1, state

    # A mixed fleet whose numbers a reader would actually add up.
    mixed = {
        "r-work-1": {"state": "working"},
        "r-work-2": {"state": "running"},
        "r-work-3": {"state": "dispatched"},
        "r-blocked-1": {"state": "blocked"},
        "r-blocked-2": {"state": "unreadable"},
        "r-blocked-3": {"state": "stalled"},
        "r-done-1": {"state": "complete"},
        "r-done-2": {"state": "completed_unpromoted"},
    }
    counts = recovery._fleet_counts(mixed)
    assert count_tuple(counts) == (3, 3, 2)
    assert sum(counts.values()) == len(mixed)

    # An empty fleet is the boundary case of the same invariant.
    assert sum(recovery._fleet_counts({}).values()) == 0


def test_a_state_in_no_bucket_breaks_the_sum_instead_of_disappearing() -> None:
    """The guard has teeth: an unregistered state reads as a dropped row.

    This is the historical failure mode made observable. If the classifier
    emitted a state that was not yet reconciled into a bucket, its row would
    render while the counters ignored it. Feed such a state through
    ``_fleet_counts`` and the sums stop adding up, so the "adds up" property
    above is what converts a silent gap into a red test.
    """
    unregistered = {"r-new": {"state": "frobbed", "run_id": "r-new"}}
    counts = recovery._fleet_counts(unregistered)
    assert counts == {"working": 0, "blocked": 0, "unpromoted": 0}
    assert sum(counts.values()) != len(unregistered)


def test_the_blocked_bucket_is_derived_from_the_attention_set() -> None:
    """What the counter calls blocked is exactly what a reader must act on.

    The blocked tally and the attention set are one proposition, so the bucket
    is derived from the set rather than kept as a second list that can drift
    apart — the drift that let one attention-worthy state fall out. And the
    attention set is not a bucket of imaginary states: every member is one a
    snapshot can actually carry, which combined with the add-up assertion above
    means a state needing attention is always counted as needing attention.
    """
    assert tuple(sorted(ticker_module.NEEDS_ACTION)) == recovery.FLEET_BLOCKED_STATES
    assert _emitted_states() >= ticker_module.NEEDS_ACTION


def test_every_emitted_state_is_watched_actionable_or_delivered() -> None:
    """The emitted vocabulary splits into three designed dispositions.

    A snapshot state is either work in progress, work a coordinator must act
    on, or delivered work waiting on a gate. Stated as the reading that decides
    whether someone wakes up: every emitted state that is not a delivered state
    is either watched or in the attention set. A state outside all three is
    exactly the one with no bucket, and it names itself here before any counter
    can hide it.
    """
    working = set(recovery.FLEET_WORKING_STATES)
    blocked = set(recovery.FLEET_BLOCKED_STATES)
    unpromoted = set(recovery.FLEET_UNPROMOTED_STATES)
    disposed = {"working": working, "blocked": blocked, "unpromoted": unpromoted}

    # The three dispositions are mutually exclusive: a double-counted state
    # would inflate a total beyond the row count rather than drop out.
    for left, right in (
        ("working", "blocked"),
        ("working", "unpromoted"),
        ("blocked", "unpromoted"),
    ):
        assert not disposed[left] & disposed[right], f"{left} and {right} overlap"

    emitted = _emitted_states()
    unexplained = emitted - working - blocked - unpromoted
    assert not unexplained, f"states with no bucket: {sorted(unexplained)}"

    # The reading that decides whether someone wakes up, stated directly: every
    # non-delivered state is a watched state or an attention state, so nothing
    # a snapshot can carry is invisible to the columns that decide.
    unwatched = emitted - unpromoted - working - blocked
    assert not unwatched, f"states neither watched nor actionable: {sorted(unwatched)}"


def _counted_states() -> set[str]:
    return (
        set(recovery.FLEET_WORKING_STATES)
        | set(recovery.FLEET_BLOCKED_STATES)
        | set(recovery.FLEET_UNPROMOTED_STATES)
    )


def count_tuple(counts: Mapping[str, int]) -> tuple[int, int, int]:
    return counts["working"], counts["blocked"], counts["unpromoted"]
