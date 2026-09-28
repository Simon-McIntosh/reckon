"""The follower prints only the rows the signal audit's policy keeps.

The pane is a reading surface for a coordinator, so a row is worth its space
only when it tells a reader something the pane does not already say. The policy
that decides that keeps four classes: a coordinator row prints with its plain
reason, an observer row prints unmarked, a same-state rewrite moves the counter
block alone, and a row that opens a noise pair is held for the pair's window so
a flicker never reaches the pane.

Every hold is paired here with its real counterpart, because a rule that drops
noise is only correct if it still prints the event that merely resembles it.

Two of these drive the follower's own generator against a synthetic config home
and a real stream, because a policy that answers correctly when fed directly can
still be wired so that nothing reaches it, or be bypassed by the loop that calls
it. The rest are the policy's own unit cases.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import follow_checkpoint, runs, ticker

PROJECT = "policy-proj"
SESSION = "s1"
RUN_A = "r-policy-a"
RUN_B = "r-policy-b"

# Long enough that an arming reaches its first read and records its place even
# when the host is loaded, and short enough that a file of them stays quick.
ARM_LIFETIME = 0.75

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


def _row(run_id: str, from_state: object, to_state: object, **extra: object):
    row = {"run_id": run_id, "from_state": from_state, "to_state": to_state}
    row.update(extra)
    return row


def _window(kind: tuple[object, object]) -> float:
    return ticker.NOISE_PAIRS[kind][1]


# --- the follower harness -------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and checkpoints in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _arm(*, lifetime: float = ARM_LIFETIME, **kwargs) -> list[dict]:
    """Run one arming to its own lifetime and collect the rows it drew."""
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=lifetime,
        **kwargs,
    )
    return [event for event in generator if event.get("event") in _FLEET_EVENTS]


def _event(
    run_id: str,
    node: str,
    *,
    state: str,
    observed_at: str,
    previous: str | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "event": "transition",
        "run_id": run_id,
        "node": node,
        "session": SESSION,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": observed_at,
        "legacy": False,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _replace_stream(stream_path: Path, events: list[dict]) -> None:
    """Put a fresh stream at the same path, so its identity changes with it."""
    replacement = stream_path.with_name(stream_path.name + ".replacement")
    replacement.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8"
    )
    os.replace(replacement, stream_path)


def _live_runs(home: Path, *run_ids: str) -> None:
    for run_id in run_ids:
        _write_pointer(home, run_id, f"node-{run_id}", phase="working")


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


# The kinds the audit's census table classes coordinator, plus the three kinds
# that open a held noise pair. The second group is classed ``drop`` in that
# column, but the audit's policy section keeps a block, a stall or an
# abandonment coordinator-visible when it lacks its recovery evidence, and the
# hold is what decides that — so each of them prints with a plain reason once
# its window closes without the pair completing. Written out rather than read
# from the set under test: a literal is what makes a trimmed set fail.
AUDITED_COORDINATOR_KINDS = {
    ("abandoned", "complete"),
    ("abandoned", "unreadable"),
    ("blocked", "complete"),
    ("complete", "blocked"),
    ("complete", "completed_unpromoted"),
    ("complete", "stalled"),
    ("completed_unpromoted", "blocked"),
    ("completed_unpromoted", "complete"),
    ("dispatched", "abandoned"),
    ("dispatched", "blocked"),
    ("dispatched", "complete"),
    ("dispatched", "completed_unpromoted"),
    ("dispatched", "stalled"),
    ("dispatched", "waiting"),
    ("unreadable", "blocked"),
    ("unreadable", "complete"),
    ("waiting", "blocked"),
    ("working", "blocked"),
    ("working", "complete"),
    ("working", "completed_unpromoted"),
    ("working", "stalled"),
    ("working", "unreadable"),
    ("working", "waiting"),
}


def test_the_coordinator_kinds_are_exactly_the_audited_ones():
    """The set is the document's list, so trimming it fails here and not silently."""
    assert len(AUDITED_COORDINATOR_KINDS) == 23
    assert ticker.COORDINATOR_KINDS == AUDITED_COORDINATOR_KINDS
    # A kind the audit does not name belongs to its catch-all rule rather than
    # to this set, so membership is a claim about the document.
    assert ("stalled", "blocked") not in ticker.COORDINATOR_KINDS


def test_every_coordinator_kind_prints_with_its_plain_reason():
    for kind in sorted(AUDITED_COORDINATOR_KINDS, key=str):
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


# --- the replay answers through the same path the pane uses ---------------


def test_replay_prints_fewer_rows_than_it_fed_and_other_sessions_stay_out():
    events = []
    for index, run in enumerate([f"run-{n}" for n in range(40)]):
        events.append(_row(run, "dispatched", "abandoned", observed_at=_stamp(index)))
        events.append(_row(run, "abandoned", "working", observed_at=_stamp(index)))
        events.append(_row(run, "working", "stalled", observed_at=_stamp(index + 1)))
        events.append(_row(run, "stalled", "working", observed_at=_stamp(index + 1)))
        events.append(_row(run, "working", "complete", observed_at=_stamp(index + 2)))
    # The path carries the follower's own selection, so an event the follower
    # would never have offered the policy is not a row the replay can count.
    mine = {"run-0", "run-1"}
    printed = ticker.replay_row_policy(
        events,
        selects=lambda event: event.get("run_id") in mine,
    )
    assert len(printed) < len(events)
    kinds = {(row.get("from_state"), row.get("to_state")) for row in printed}
    assert ("working", "complete") in kinds
    assert ("dispatched", "abandoned") not in kinds
    assert ("working", "stalled") not in kinds
    assert {row["run_id"] for row in printed} == mine


def test_the_replay_path_is_the_path_the_follower_builds():
    path = cli.follower_row_path(session=SESSION, run_ids=[RUN_A])
    assert isinstance(path, ticker.PaneRowPath)
    assert path.reported == {}
    # The path's memory is written where a row is released, so a withheld row
    # leaves no trace a later arming could mistake for a delivered row.
    assert path.feed(_row(RUN_A, "working", "stalled"), now=1.0) == []
    assert path.reported == {}
    assert path.feed(_row(RUN_A, "working", "complete"), now=2.0) != []
    assert path.reported == {RUN_A: "complete"}
    assert path.feed(_row("r-someone-else", "working", "complete"), now=3.0) == []


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


# --- the follower's own generator, which is what the pane actually reads ---


def test_a_noise_pair_prints_nothing_through_the_followers_own_rows(home) -> None:
    """The pair completes inside its window, so neither side reaches the pane.

    A run's unrelated transition rides in the same append, so a follower that
    read nothing at all cannot pass this by staying silent.
    """
    _live_runs(home, RUN_A, RUN_B)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first

        now = _iso(time.time())
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="stalled",
                    observed_at=now,
                    previous="working",
                ),
                _event(
                    RUN_A,
                    "node-a",
                    state="working",
                    observed_at=now,
                    previous="stalled",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="complete",
                    observed_at=now,
                    previous="working",
                ),
            ],
        )
        second = _arm()
        moved = {event["run_id"] for event in second}
        assert RUN_B in moved, second
        assert RUN_A not in moved, second


def test_a_row_the_policy_held_is_not_remembered_as_delivered(home) -> None:
    """A held row leaves no memory that claims the pane was shown it.

    The opener is fed to the follower and withheld. The arming that withheld it
    releases it on its own way out — that is what a held row does when the pane
    has no further second to wait — but the place it recorded must not carry the
    opener's state, because the pane never showed it. The stream is then
    replaced, so the next arming reads it from the start and meets the opener
    again; that row reaches the pane, and a memory written where a row is
    *selected* rather than where it is *released* is what suppresses it.
    """
    _live_runs(home, RUN_A)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A}, first

        opener = _event(
            RUN_A,
            "node-a",
            state="stalled",
            observed_at=_iso(time.time()),
            previous="working",
        )
        _append_stream(stream_path, [opener])
        holding = _arm()
        assert [event["to_state"] for event in holding if event["run_id"] == RUN_A] == [
            "stalled"
        ], holding

        record = follow_checkpoint.read(PROJECT, SESSION) or {}
        assert (record.get("reported") or {}).get(RUN_A) != "stalled"

        _replace_stream(stream_path, [opener])
        rearmed = _arm()
        assert "stalled" in [
            event["to_state"] for event in rearmed if event["run_id"] == RUN_A
        ], rearmed
