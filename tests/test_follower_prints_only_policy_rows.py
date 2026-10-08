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


@pytest.fixture()
def observer_pane(monkeypatch):
    """Build the follower's pane with observer context shown.

    The coordinator's pane holds observer rows back, and a resume re-derives a
    bare baseline — observer context — so a test of the resume's own baseline
    path builds the pane the ``show_observer`` flag exists for, exactly as the
    comparison and the negative control do.
    """
    original = ticker.PaneRowPath

    class _Showing(original):
        def __init__(self, **kwargs):
            kwargs["show_observer"] = True
            super().__init__(**kwargs)

    monkeypatch.setattr(ticker, "PaneRowPath", _Showing)
    return _Showing


# --- the held pairs print nothing -----------------------------------------


def test_launch_flicker_pair_prints_nothing():
    policy = ticker.RowPolicy()
    policy.seed("run-a", "working")
    assert policy.feed(_row("run-a", "dispatched", "abandoned"), now=1000.0) == []
    assert policy.feed(_row("run-a", "abandoned", "working"), now=1005.0) == []
    assert policy.flush(now=1e12) == []


def test_exit_phantom_pair_prints_the_net_change():
    """A run that finished behind a held block reaches the pane as one row.

    The pair's resolution lands in the action state ``completed_unpromoted``, so
    it prints the net change under the opener's left side rather than dropping
    the promotion the coordinator owes the run.
    """
    policy = ticker.RowPolicy()
    policy.seed("run-b", "working")
    assert policy.feed(_row("run-b", "working", "blocked"), now=2000.0) == []
    printed = policy.feed(_row("run-b", "blocked", "completed_unpromoted"), now=2010.0)
    assert [(row["from_state"], row["to_state"]) for row in printed] == [
        ("working", "completed_unpromoted")
    ], printed
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
    """A held opener cannot be silently swallowed by a rule applied after it.

    The later coordinator row is itself held for the settle window, so it
    reaches the pane on its own release rather than in the same call.
    """
    policy = ticker.RowPolicy()
    opener = _row("run-g", "working", "blocked")
    assert policy.feed(opener, now=1000.0) == []
    late = _row("run-g", "blocked", "complete")
    assert policy.feed(late, now=1005.0) == [opener]
    assert policy.flush(now=1e12) == [late]


# --- the three kept classes -----------------------------------------------


def test_same_state_rewrite_moves_only_the_counter():
    assert ticker.transition_class("working", "working") == ticker.ROW_COUNTER
    policy = ticker.RowPolicy()
    assert policy.feed(_row("run-h", "working", "working"), now=1.0) == []
    # The counter path still advances the memory, so a real move afterwards is
    # news rather than a repeat of what the counter was counting. The move is a
    # coordinator row, so it reaches the pane on its settle window's release.
    move = _row("run-h", "working", "complete")
    assert policy.feed(move, now=2.0) == []
    assert policy.flush(now=2.0 + ticker.SETTLE_WINDOW) == [move]


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
        assert policy.feed(row, now=1.0) == []
        # The kind is either a noise opener, held for its own window, or a
        # coordinator row, held for the settle window; either way it prints on
        # its release, carrying its plain reason.
        printed = policy.flush(now=1e12)
        assert printed == [row], kind
        assert printed[0]["reason"] == "landed clean"


def test_observer_rows_print_unmarked():
    assert ticker.transition_class("unreadable", "working") == ticker.ROW_OBSERVER
    # A pane that shows observer context prints the row as it always did; the
    # coordinator's default pane holds it back.
    policy = ticker.RowPolicy(show_observer=True)
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
    # The exemption keeps the row off the re-derivation rule, so it is held for
    # its settle window and prints on release, not suppressed.
    assert policy.feed(row, now=1.0) == []
    assert policy.flush(now=1.0 + ticker.SETTLE_WINDOW) == [row]


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
    # The stall opener prints late when its pair does not complete, so that row
    # is the one the memory records; the coordinator row that displaced the
    # hold is itself held for the settle window and reaches the pane later.
    assert path.feed(_row(RUN_A, "working", "complete"), now=2.0) != []
    assert path.reported == {RUN_A: "stalled"}
    assert path.flush(now=1e12) != []
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
        # A fresh attach of working runs is a bare re-announcement, which the
        # coordinator's pane holds back, so the attach wakes nobody.
        assert first == [], first

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
        # A fresh attach of a working run is a bare re-announcement, held back.
        assert first == [], first

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


def test_a_resume_baseline_stays_quiet_for_a_row_the_pane_already_shows(
    home, observer_pane
) -> None:
    """A re-derived baseline is fed through the row path, so a known row is silent.

    A resume carries the states the pane last drew. When its checkpoint no longer
    names this stream, the arming re-derives the fleet baseline rather than
    continuing from a place that does not name the file — and that baseline is
    fed through the same row path the pane reads, so a run the pane already
    shows at its current state must not be announced a second time. The run the
    resume does not name is genuinely new, so it reaches the pane: an arming
    that printed nothing at all cannot pass this.

    The baseline is observer context, so the pane is built with
    ``show_observer`` true — the coordinator's own pane holds it back, and this
    test is about the resume's baseline routing rather than the hold.
    """
    _live_runs(home, RUN_A, RUN_B)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        # Read the state the fleet's own baseline carries, rather than writing
        # the literal here, so the expectation comes from the same fixture the
        # follower reads.
        first = _arm()
        states = {event["run_id"]: event["to_state"] for event in first}
        assert set(states) == {RUN_A, RUN_B}, first

        # The pane this image replaces already shows RUN_A at its current
        # state. The recorded stream does not name this file, so the arming
        # re-derives the baseline instead of continuing.
        resumed = _arm(
            resume={
                "stream_path": f"{stream_path}.replaced",
                "offset": 0,
                "reported": {RUN_A: states[RUN_A]},
            }
        )
        by_run = {event["run_id"]: event for event in resumed}
        assert RUN_A not in by_run, resumed
        assert RUN_B in by_run, resumed
        # The row that did reach the pane came from the re-derived baseline, so
        # the arming took the baseline branch rather than a continuation.
        assert by_run[RUN_B]["event"] == "baseline", resumed


def test_a_resume_that_names_a_replaced_stream_owes_its_rows_on_its_first_pass(
    home,
    observer_pane,
) -> None:
    """A replaced-stream resume derives the baseline on one pass, budget or none.

    A resume whose checkpoint names a replaced stream re-derives the fleet
    baseline, and that derivation happens on the arming's first pass — before
    any wait — so a run the pane does not yet show reaches it however little of
    the arming's lifetime is left. An arming that instead read the replaced
    stream would deliver the row only on a pass gated on the remaining
    lifetime, and an arming whose lifetime is already spent never reaches it.
    This drives the same case with no budget left and requires the row anyway,
    so the owed row cannot depend on how much of the arming's time remains.

    The baseline is observer context, so the pane is built with
    ``show_observer`` true, as above.
    """
    _live_runs(home, RUN_A, RUN_B)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        first = _arm()
        states = {event["run_id"]: event["to_state"] for event in first}
        assert set(states) == {RUN_A, RUN_B}, first

        resumed = _arm(
            lifetime=0.0,
            resume={
                "stream_path": f"{stream_path}.replaced",
                "offset": 0,
                "reported": {RUN_A: states[RUN_A]},
            },
        )
        by_run = {event["run_id"]: event for event in resumed}
        assert RUN_A not in by_run, resumed
        assert RUN_B in by_run, resumed
        assert by_run[RUN_B]["event"] == "baseline", resumed
