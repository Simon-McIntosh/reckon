"""Every arming of the follower replays the fleet with each run's recorded time.

A follower's checkpoint travels through the environment an in-place reload hands
to its replacement, so a re-arm — a new process, no such environment — used to
begin from nothing and re-announce a burst of baseline rows each stamped with
the moment it attached, then start reading at the stream's end so every
transition written while nothing was attached was skipped. A re-arm after a
quiet stretch was blank; a re-arm after a busy one showed a blob under one
timestamp.

The contract now is a replay. Every arming of a fresh image — the first and
every re-arm alike — draws one row for each live run, stamped with the time that
run's own record says it entered its current state and ordered by that time.
Only an in-place reload, which keeps its grid on screen, continues from its
recorded offset and delivers just what moved.

These tests drive the follower's own generator against a synthetic config home
and a real producer, so a row stamped with the attach time cannot pass by
matching a time the test itself chose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import cli, crew, crew_follow_commands
from reckon.crew import follow_checkpoint, recovery, runs
from reckon.crew import ticker as ticker_module

PROJECT = "rearm-proj"
SESSION = "s1"
RUN_A = "r-rearm-a"
RUN_B = "r-rearm-b"

# The follower's own lifetime for each arming. Long enough that an arming
# reaches its first read and records its place even when the host is loaded, and
# short enough that a suite of them stays quick: an interpreter running the
# whole file alongside other work can otherwise spend most of a shorter arming
# before it reaches the stream at all, so an arming that expired early would
# look like one that had nothing to deliver.
ARM_LIFETIME = 0.75

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and checkpoints in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture(autouse=True)
def _a_live_owner(monkeypatch):
    """Isolate every arming from an owner stamped into the ambient environment.

    A follower stamps its own pid into ``RECKON_FOLLOWER_OWNER`` for the
    processes it launches, and one it launched outlives it once the follower
    exits on its lifetime. An arming that read that owner as its own ends at its
    first wait pass, so a file that passes under a plain shell fails under a
    follower: several cases here measure a *second* arming, and a first arming
    ended early leaves the place it should have written unset. The variable is
    removed before each case, and the resolved owner cleared with it, because
    the identity is cached on the module after its first read. The previous
    cache is restored after, so nothing leaks into another file in the process.
    """
    previous = runs._RESOLVED_FOLLOWER_OWNER.resolved
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    runs._RESOLVED_FOLLOWER_OWNER.resolved = None
    yield
    runs._RESOLVED_FOLLOWER_OWNER.resolved = previous


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


def _deliver(home: Path, run_id: str, status: str) -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: rearm-node\nstatus: {status}\ncommits: HEAD\nblockers: none\n"
    )


def _arm(*, lifetime: float = ARM_LIFETIME, **kwargs) -> list[dict]:
    """Run one arming to its own lifetime and collect the fleet rows it drew.

    Driven directly rather than through the command, because the measure is what
    a *second* arming emits for a *first* one's stream; the command's rendering
    is exercised elsewhere. The arming ends by its own deadline, so nothing
    external stops it and the rows are exactly what it delivered.
    """
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=lifetime,
        **kwargs,
    )
    return [event for event in generator if event.get("event") in _FLEET_EVENTS]


def _gap_events(stream_path: Path, before: int) -> list[dict]:
    events = list(runs.read_stream_events(stream_path))
    return events[before:]


def _read_stream(stream_path: Path) -> list[dict]:
    return list(runs.read_stream_events(stream_path))


def _event(
    run_id: str,
    node: str,
    *,
    state: str,
    observed_at: str,
    event: str = "transition",
    previous: str | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "event": event,
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
    """An epoch as the stream writes one, so a chosen stamp round-trips."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    """Add transitions to the stream as a producer would, at the given stamps."""
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


def _two_live_runs(home: Path) -> None:
    """Two live runs whose rows an arming draws.

    Delivered into ``complete``, so each run's baseline is an action row the
    pane keeps — a run still in a progress state is observer context the pane
    withholds, and would draw nothing.
    """
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _write_pointer(home, RUN_B, "node-b", phase="working")
    _deliver(home, RUN_A, "complete")
    _deliver(home, RUN_B, "complete")


# ── Every arming replays the fleet, one row per live run ────────────────────


def test_a_rearm_with_nothing_new_draws_one_row_per_live_run(home) -> None:
    """A re-arm with nothing new is the fleet, not a blank pane.

    The first arming draws the fleet. Nothing moves while the follower is away,
    and the second arming begins with no environment checkpoint, exactly as a
    first attach does. It replays the fleet all the same: one row per live run,
    so a reader re-arming after a Monitor's end is never left staring at an
    empty pane.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first

        second = _arm(resume=None)

    assert [event["run_id"] for event in second] == [RUN_A, RUN_B], (
        f"a re-arm with nothing new draws one row per live run; got {second!r}"
    )


def test_a_rearm_with_nothing_new_carries_the_recorded_state_time(home) -> None:
    """A run that has not moved still carries the time its state was recorded.

    The run is live and working, and the stream's last word for it says so at a
    stamp long before the arming. Nothing changes in the gap, so no transition
    is replayed for that run: its row is derived from the live pointer, and its
    time is the stream's own record for that state, not the second the pane
    attached. Reading only the replayed lines would stamp it with the attach
    time, so the expectation is the recorded past stamp read back.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        past = _iso(time.time() - 40 * 60)
        assert ticker_module.local_clock(past) != ticker_module.local_clock(
            _iso(time.time())
        )
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="completed_unpromoted",
                    observed_at=past,
                    previous="working",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="completed_unpromoted",
                    observed_at=past,
                    previous="working",
                ),
            ],
        )
        # Consume the appended lines, so the next arming has no gap to replay
        # and derives both rows from the fleet.
        _arm()
        third = _arm(resume=None)

    assert [str(event["observed_at"]) for event in third] == [past, past], (
        f"a run that did not move still carries its recorded state time; got "
        f"{[event['observed_at'] for event in third]!r}"
    )


def test_a_rearm_stamps_the_entry_into_a_state_not_its_last_re_emission(
    home,
) -> None:
    """A run carries the time it entered its state, not its last re-emission.

    The stream records a transition into ``working`` and then three baselines
    that re-announce the same state. Those baselines extend the unbroken
    trailing run of ``working`` the transition opened, so the row a later
    arming derives from the fleet carries the transition's stamp. Reading the
    last event naming the run would stamp the row with the final re-emission
    instead, which is the moment that event was written, not the moment the run
    began working.
    """
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _deliver(home, RUN_A, "complete")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        entered = _iso(time.time() - 40 * 60)
        reemits = [
            _iso(time.time() - 30 * 60),
            _iso(time.time() - 20 * 60), _iso(time.time() - 20 * 60),
            _iso(time.time() - 10 * 60),
        ]
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="completed_unpromoted",
                    observed_at=entered,
                    previous="working",
                ),
                *[
                    _event(
                        RUN_A,
                        "node-a",
                        state="completed_unpromoted",
                        observed_at=stamp,
                        event="baseline",
                    )
                    for stamp in reemits
                ],
            ],
        )
        # Consume the appended lines, so the next arming has no gap to replay
        # and derives the row from the fleet.
        _arm()
        third = _arm(resume=None)

    assert [str(event["observed_at"]) for event in third] == [entered], (
        f"a run's row carries the time it entered working, not its last "
        f"re-emission; got {[event['observed_at'] for event in third]!r}"
    )


def test_a_rearm_stamps_the_current_entry_after_a_state_returns(home) -> None:
    """A state a run returns to is stamped at its returning entry.

    The stream records ``working``, then ``blocked``, then ``working`` again.
    The trailing run of ``working`` began at the second entry, because the
    ``blocked`` event broke the first run, so the row carries the second entry's
    stamp rather than the first. A scan that kept the last event naming the run
    would agree here by accident; a scan that kept the first event naming the
    run would stamp the row with the stale first entry.
    """
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _deliver(home, RUN_A, "complete")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        first_working = _iso(time.time() - 40 * 60)
        blocked = _iso(time.time() - 30 * 60)
        second_working = _iso(time.time() - 20 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="completed_unpromoted",
                    observed_at=first_working,
                    previous="working",
                ),
                _event(
                    RUN_A,
                    "node-a",
                    state="failed",
                    observed_at=blocked,
                    previous="completed_unpromoted",
                ),
                _event(
                    RUN_A,
                    "node-a",
                    state="completed_unpromoted",
                    observed_at=second_working,
                    previous="failed",
                ),
            ],
        )
        _arm()
        third = _arm(resume=None)

    assert [str(event["observed_at"]) for event in third] == [second_working], (
        f"a run back in working is stamped at its second entry; got "
        f"{[event['observed_at'] for event in third]!r}"
    )


def test_a_rearm_after_a_quiet_baseline_arming_draws_the_fleet(home) -> None:
    """A baseline arming against a quiet stream still leaves a place, and the
    next arming still replays the fleet.

    The baseline rows are derived from the live fleet, not read from the stream,
    so an arming can emit them having read no line at all. Here the producer
    holds its claim but has not written a line, so the stream does not exist and
    the read loop that advances the place is never entered. The re-arm must
    still draw the fleet from the live pointers, and with no stream to read it
    stamps each row from the place the fleet itself records.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        # A producer with a claim and no first line: the stream is absent, so
        # nothing is read, and the producer being live is what lets the arming
        # derive its baseline from the fleet at all.
        Path(seat["stream_path"]).unlink()

        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first
        assert follow_checkpoint.read(PROJECT, SESSION), (
            "a baseline arming leaves a durable place even when it reads no line"
        )

        second = _arm(resume=None)

    assert [event["run_id"] for event in second] == [RUN_A, RUN_B], (
        f"a re-arm after a quiet baseline arming draws the fleet; got {second!r}"
    )


def test_a_rearm_carries_each_runs_recorded_state_time(home) -> None:
    """Each row is stamped with the recorded time its run entered its state.

    A run sitting at its state since long before the arming still carries the
    clock its own record gave it, and the rendered row leads with that clock. A
    row stamped with the attach time would render today's second, so the
    stream's own past stamps are read into the expectation before the second
    arming runs.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        a_stamp = _iso(time.time() - 25 * 60)
        b_stamp = _iso(time.time() - 12 * 60)
        assert ticker_module.local_clock(a_stamp) != ticker_module.local_clock(b_stamp)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="complete",
                    observed_at=a_stamp,
                    previous="working",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="complete",
                    observed_at=b_stamp,
                    previous="working",
                ),
            ],
        )

        second = _arm(resume=None)

    assert [str(event["observed_at"]) for event in second] == [a_stamp, b_stamp], (
        f"each row carries its run's recorded time, never the arming time; got "
        f"{[event['observed_at'] for event in second]!r}"
    )
    # The stamp reaches the reader as the line's own clock, not merely as a field
    # beside it: a row stamped with the attach time would render today's second.
    for row, stamp in zip(second, (a_stamp, b_stamp), strict=True):
        line = recovery.format_watch_transition(row)
        assert line.startswith(ticker_module.local_clock(stamp)), (
            f"the rendered row does not carry the run's recorded time; {line!r}"
        )


def test_a_rearm_orders_rows_by_recorded_time(home) -> None:
    """The rows arrive in ascending recorded-time order, not fleet order.

    The fleet as it stands names RUN_A before RUN_B, but RUN_B entered its state
    first. A re-arm reads as a timeline, so the earlier record leads.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        b_early = _iso(time.time() - 30 * 60)
        a_late = _iso(time.time() - 5 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="complete",
                    observed_at=a_late,
                    previous="working",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="complete",
                    observed_at=b_early,
                    previous="working",
                ),
            ],
        )

        second = _arm(resume=None)

    assert [str(event["run_id"]) for event in second] == [RUN_B, RUN_A], (
        f"rows arrive in ascending recorded-time order; got {second!r}"
    )


def test_a_replaced_stream_draws_each_live_run_once_with_recorded_times(home) -> None:
    """When the stream cannot be continued, the fleet is still replayed whole.

    The file is replaced between the two arms, so the recorded offset names no
    boundary. The replay does not fall back to "what moved": each live run is
    drawn once, and each carries the state and recorded time the stream recorded
    for it. The run that moved carries its transition time; the run that did not
    carries the earlier clock it entered its state under.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first
        stream_path = Path(seat["stream_path"])

        a_stamp = "2026-01-02T03:04:05+00:00"
        b_stamp = "2026-01-02T03:14:15+00:00"
        _replace_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="complete",
                    observed_at=a_stamp,
                    event="baseline",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="complete",
                    observed_at=b_stamp,
                    previous="working",
                ),
            ],
        )

        second = _arm(resume=None)

    assert [str(event["run_id"]) for event in second] == [RUN_A, RUN_B], (
        f"each live run is drawn once; got {second!r}"
    )
    by_run = {str(event["run_id"]): event for event in second}
    assert str(by_run[RUN_B]["observed_at"]) == b_stamp, by_run[RUN_B]
    assert str(by_run[RUN_B]["to_state"]) == "complete", by_run[RUN_B]
    assert str(by_run[RUN_A]["observed_at"]) == a_stamp, by_run[RUN_A]


def test_a_run_that_moved_in_the_gap_appears_once_with_its_new_state(home) -> None:
    """No run is delivered twice in one arming, even when it also moved.

    RUN_A moved while nothing was attached. The replay draws it once, at the
    state and time the stream now records, and the gap transition is not
    delivered a second time beside it.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        moved = _iso(time.time() - 8 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="blocked",
                    observed_at=moved,
                    previous="working",
                )
            ],
        )

        second = _arm(resume=None)

    run_ids = [str(event["run_id"]) for event in second]
    assert run_ids.count(RUN_A) == 1, f"RUN_A must appear exactly once; got {second!r}"
    assert run_ids.count(RUN_B) == 1, f"RUN_B must appear exactly once; got {second!r}"
    moved_row = next(event for event in second if str(event["run_id"]) == RUN_A)
    assert str(moved_row["to_state"]) == "blocked", moved_row
    assert str(moved_row["observed_at"]) == moved, moved_row


def test_a_transition_appended_after_the_replay_is_read_arrives_once(
    home, monkeypatch
) -> None:
    """A line written between the replay's read and the follow is not lost.

    The replay reads to a boundary fixed before it starts, and the read loop then
    opens at exactly that byte. A transition appended after the replay has read
    — and before the loop's first read — therefore falls to the loop's side of
    the boundary and is delivered once. Taking the boundary from the file after
    the replay instead would place the loop past the new line, so it would be
    neither replayed nor followed: lost in the gap between the two reads. The
    append is injected through the stream reader so it lands in exactly that
    window rather than by racing a thread against it.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        appended = _iso(time.time() - 60)
        original = cli._stream_events_upto
        injected: list[bool] = []

        def append_after_the_replay_read(stream, *, offset, boundary):
            events = original(stream, offset=offset, boundary=boundary)
            # The recorded read is the one at offset zero; the replay's own read
            # is the later one, and it is after that read that the new line must
            # land — before the read loop opens at the boundary.
            if offset > 0 and not injected:
                injected.append(True)
                _append_stream(
                    stream_path,
                    [
                        _event(
                            RUN_A,
                            "node-a",
                            state="blocked",
                            observed_at=appended,
                            previous="working",
                        )
                    ],
                )
            return events

        monkeypatch.setattr(crew_follow_commands, "_stream_events_upto", append_after_the_replay_read)
        second = _arm(resume=None)

    assert injected, "the injection must have fired, or this check is vacuous"
    blocked = [event for event in second if str(event["to_state"]) == "blocked"]
    assert len(blocked) == 1, (
        f"the transition appended after the replay's read is delivered exactly "
        f"once; got {second!r}"
    )
    assert str(blocked[0]["observed_at"]) == appended, blocked[0]


@pytest.mark.parametrize("boundary_mid_record", [False, True])
def test_a_record_half_written_at_the_boundary_is_delivered_once_whole(
    home, monkeypatch, boundary_mid_record
) -> None:
    """A record the producer is still writing is delivered once, whole.

    The boundary the replay reads to is captured from the file's size, and that
    size can fall inside a record the producer has not finished writing. The
    guard snaps it back to the byte after the last newline at or before it, so
    the replay stops on a whole record and the read loop opens at the start of
    the one still being written, which it then reads whole once the newline
    lands. The record moves a run, so it is news the pane must show exactly
    once: a boundary taken mid-record would leave the replay reading a fragment
    the parser drops and the loop reading the completion as a second
    unparseable fragment, so the move would never reach the pane. The check runs
    with the record completed before the boundary too, where the size already
    lands on a newline and the snap is a no-op, so it does not pass by matching
    a case the snap alone creates.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        moved = _iso(time.time() - 7 * 60)
        record = (
            json.dumps(
                _event(
                    RUN_A,
                    "node-a",
                    state="complete",
                    observed_at=moved,
                    previous="working",
                )
            )
            + "\n"
        )
        half = len(record) // 2
        if boundary_mid_record:
            # The producer has written half a record and not its newline when
            # the arming captures the boundary.
            with stream_path.open("a", encoding="utf-8") as handle:
                handle.write(record[:half])
        else:
            # The whole record is there before the arming captures the boundary,
            # so the size already lands on a newline and the snap is a no-op.
            with stream_path.open("a", encoding="utf-8") as handle:
                handle.write(record)

        original = cli._stream_events_upto
        calls: list[int] = []

        def complete_after_the_replay_read(stream, *, offset, boundary):
            events = original(stream, offset=offset, boundary=boundary)
            calls.append(offset)
            # The first call is the recorded-times read at offset zero; the
            # second is the replay's own read, after the boundary was captured
            # and before the loop opens at it. The producer completes the record
            # here.
            if len(calls) == 2 and boundary_mid_record:
                with stream_path.open("a", encoding="utf-8") as handle:
                    handle.write(record[half:])
            return events

        monkeypatch.setattr(crew_follow_commands, "_stream_events_upto", complete_after_the_replay_read)
        second = _arm(resume=None)

    assert len(calls) >= 2, "the injection must have observed the record's read"
    moved_rows = [
        event
        for event in second
        if str(event.get("run_id")) == RUN_A
        and str(event.get("to_state")) == "complete"
    ]
    assert len(moved_rows) == 1, (
        f"the record at the boundary is delivered exactly once and whole; got "
        f"{second!r}"
    )
    row = moved_rows[0]
    assert str(row["observed_at"]) == moved, row
    assert str(row["node"]) == "node-a", row


def test_a_recorded_state_the_live_fleet_denies_is_not_announced(home) -> None:
    """The live fleet is the authority on a run's state, not the stream's history.

    The stream's last word for RUN_A is ``stalled``, at a stamp in the past, but
    the live pointer still classifies RUN_A as ``working``. The two disagree, so
    the replayed row keeps the live state and the fleet's own clock: a row never
    announces a state the live fleet denies. The stream's stamp is kept only
    where the stream's last state agrees with the live one.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)

        stalled = _iso(time.time() - 40 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="stalled",
                    observed_at=stalled,
                    previous="working",
                )
            ],
        )
        # The first arming advances the place past the stalled line, so it is
        # history for the re-arm, not news in its gap.
        _arm()

        second = _arm(resume=None)

    by_run = {str(event["run_id"]): event for event in second}
    assert str(by_run[RUN_A]["to_state"]) == "completed_unpromoted", (
        f"the live classification is the authority; got {by_run[RUN_A]!r}"
    )
    assert str(by_run[RUN_A]["observed_at"]) != stalled, (
        f"a state the live fleet denies must not carry the stream's time; got "
        f"{by_run[RUN_A]!r}"
    )
    assert str(by_run[RUN_B]["to_state"]) == "completed_unpromoted", by_run[RUN_B]


def test_a_rearm_records_its_own_place_for_the_next(home) -> None:
    """The durable checkpoint advances to the stream's end as the fleet is drawn.

    The replay reads the stream to date, so the place it leaves must sit at the
    stream's end: an arming that starts there again replays the fleet rather
    than re-reading the history as transitions.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()

        second = _arm(resume=None)
        assert [event["run_id"] for event in second] == [RUN_A, RUN_B], second

        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record, "an arming leaves a durable checkpoint behind"
        assert int(record["offset"]) == stream_path.stat().st_size, (
            f"the checkpoint must sit at the stream's end; got {record['offset']} "
            f"against {stream_path.stat().st_size}"
        )
        assert record["stream_identity"], record


# ── Isolation ───────────────────────────────────────────────────────────────


def _real_home_paths() -> list[Path]:
    """This test's artifacts resolved against the real config home.

    Every write here is redirected to ``RECKON_HOME``; resolving the same paths
    with the override popped yields where they would have landed without it, so
    an isolated write is proved rather than believed.
    """
    saved = os.environ.pop("RECKON_HOME", None)
    try:
        return [
            runs.follower_dir(PROJECT),
            follow_checkpoint.checkpoint_path(PROJECT, SESSION),
            runs.watch_stream_path(PROJECT),
        ]
    finally:
        if saved is not None:
            os.environ["RECKON_HOME"] = saved


def test_the_real_follower_directory_is_untouched(home) -> None:
    """No checkpoint reaches the real config home's follower directory."""
    artifacts = _real_home_paths()
    for artifact in artifacts:
        assert not artifact.exists(), f"a real-home artifact pre-exists: {artifact}"

    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _arm()
        _deliver(home, RUN_A, "complete")
        crew.list_live(project=PROJECT)
        _arm(resume=None)

    for artifact in artifacts:
        assert not artifact.exists(), f"the real home gained a file: {artifact}"

    # The temp home did receive the checkpoint, so the absence above is not a
    # checkpoint that was never written anywhere.
    assert follow_checkpoint.checkpoint_path(PROJECT, SESSION).exists()


# On re-arm, _follow_history_burst composes the history burst: on a
# terminal it returns one dim frame line followed by the replayed history
# rows, also dim; under a Monitor (stdout is not a terminal) it returns
# nothing. The burst never contains live rows. Live rows are written
# afterwards, one per transition, by the follow loop.

HISTORY_HEADER = "── history"
HISTORY_SEPARATOR = "── re-armed"


@pytest.fixture(autouse=True)
def _a_moved_source_cannot_reload_the_arming(monkeypatch):
    """Freeze the code stamp so an ambient source change cannot re-execute.

    A follower replaces its own process image when the content stamp over its
    source moves while it is attached, and in this suite the tree does move
    under a running case: a peer's commit landing in the checkout, or the
    reload cases that append probe bytes to the follower's source. An arming
    that saw the move would re-execute the process hosting the suite — the
    xdist worker — and its own lifetime would then end that worker, losing
    every case still queued on it. The arming runs in a child this test owns
    and the stamp is held still for the case, so neither the child nor the
    runner can be replaced by an ambient edit. What the cases measure is a
    re-arm's replay, which no reload takes part in.
    """
    held = runs.follower_code_stamp()
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: held)


@pytest.fixture()
def follow_lines():
    """The rows the armed follower draws, collected by `_run_follow`."""
    return []


_CHILD_ARMING = """\
import json
import pathlib
import sys
import traceback

from click.testing import CliRunner

from reckon import cli, crew_follow_commands
from reckon.crew import runs

payload_path = pathlib.Path(sys.argv[1])
entry_name, terminal, producer_live_hint = sys.argv[2], sys.argv[3] == "terminal", sys.argv[4] == "producer-live"
arguments = sys.argv[5:]

# An arming must not re-execute the process it runs in: a source edit landing
# under it replaces the image, and the arming's own lifetime then ends the
# replacement.
held = runs.follower_code_stamp()
runs.follower_code_stamp = lambda: held
if producer_live_hint:
    runs.producer_live = lambda project: True
if terminal:
    crew_follow_commands._follow_replay_visible = lambda: True

rows = []
crew_follow_commands._echo_follow_line = lambda line, *, stream=None: rows.append(line)
payload = {"rows": [], "exit_code": None, "output": "", "error": ""}
try:
    result = CliRunner().invoke(
        cli.crew if entry_name == "crew" else cli.main,
        arguments,
        catch_exceptions=False,
    )
    payload = {
        "rows": rows,
        "exit_code": result.exit_code,
        "output": result.output,
        "error": "",
    }
except BaseException:
    payload["error"] = traceback.format_exc()
    payload["rows"] = rows
payload_path.write_text(json.dumps(payload))
"""


def _armed_in_a_child(
    arguments: list[str],
    *,
    entry: str = "crew",
    terminal: bool = False,
    producer_live: bool = False,
) -> dict:
    """Run one arming in a child process of this test and bring its rows back.

    The follower command can end the process it runs in: when the content
    stamp over its source moves while it is attached it re-executes itself in
    place, and the arming's own lifetime then ends the replacement. A test
    runner hosting that arming is the process it ends, which is how a case
    here crashed an xdist worker and took every case still queued on it. The
    command therefore runs as a child process this test owns, with the case's
    patches applied inside the child, and a child that ends without returning
    its rows is reported as a failure of the case rather than a vanished
    runner.
    """
    workdir = Path(tempfile.mkdtemp(prefix="reckon-arming-"))
    try:
        payload_path = workdir / "payload.json"
        root = str(Path(cli.__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD_ARMING,
                str(payload_path),
                entry,
                "terminal" if terminal else "plain",
                "producer-live" if producer_live else "plain",
                *arguments,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if not payload_path.exists():
            raise AssertionError(
                "the arming ended the process it ran in without returning (the "
                "follower re-executed in place or exited); it runs in a child "
                "this test owns, so the runner survives it. Child stderr: "
                f"{completed.stderr[-2000:]}"
            )
        payload = json.loads(payload_path.read_text())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if payload["error"]:
        raise AssertionError(f"the arming failed in its child: {payload['error']}")
    if payload["exit_code"] is None:
        raise AssertionError(
            "the arming ended the process it ran in without returning (the "
            "follower re-executed in place or exited); it runs in a child this "
            "test owns, so the runner survives it"
        )
    return payload


def _run_follow(lines: list[str] | None = None, *, terminal: bool = False) -> list[str]:
    """Arm the real follower command once, to its own short lifetime.

    The command runs in a child of this test (see `_armed_in_a_child`), and
    the rows it drew are handed to the case: appended to ``lines`` when one is
    given, and otherwise replayed through whatever ``cli._echo_follow_line``
    the case installed, which is how a case capturing in the runner observes
    them. The rows are returned either way.
    """
    payload = _armed_in_a_child(
        [
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--lifetime",
            "1s",
            "--no-color",
            "--width",
            "200",
        ],
        terminal=terminal,
    )
    assert payload["exit_code"] == 0, payload["output"]
    rows = payload["rows"]
    if lines is None:
        for row in rows:
            crew_follow_commands._echo_follow_line(row)
    else:
        lines.extend(rows)
    return rows


def _fleet_lines(lines: list[str]) -> list[str]:
    """The drawn fleet rows, without the pane's framing or the follower's end."""
    return [
        line
        for line in lines
        if not line.startswith(HISTORY_HEADER)
        and HISTORY_SEPARATOR not in line
        and follow_checkpoint.FORMAT_CHANGED_TEXT not in line
        and "follower end" not in line
    ]


def test_a_rearm_bursts_the_history_only_to_a_terminal_and_in_one_write(
    home, follow_lines, monkeypatch
) -> None:
    """A pipe is handed no burst at all; a terminal gets it whole, in one write.

    A pipe has no scrollback to restore, so the stored rows are not its to
    receive: handing them over would make rows the reader already acted on
    arrive again as transitions. Only a terminal is handed the burst, and it
    receives the whole of it as a single write — the rows in order, oldest
    first, under one frame line naming them as earlier history. Order and the
    single write are asserted together because they are one property: the reader
    is handed its whole view as one event.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow(follow_lines)
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert len(stored) == 2, stored
        follow_lines.clear()

        # A pipe: no burst at all.
        _run_follow(follow_lines)
        pipe_writes = [line for line in follow_lines if "\n" in line]
        assert pipe_writes == [], (
            f"a re-arm to a pipe writes no history burst; got {pipe_writes!r}"
        )

        # A terminal: the whole replay in one write, under one frame line.
        follow_lines.clear()
        _run_follow(follow_lines, terminal=True)

    writes = [line for line in follow_lines if "\n" in line]
    assert len(writes) == 1, (
        f"the whole replay is one write, so exactly one multi-row line reaches "
        f"the reader; got {writes!r}"
    )
    parts = writes[0].split("\n")
    assert parts[0] == cli._HISTORY_FRAME.format(count=len(stored)), (
        f"the burst opens with one frame line naming its row count; got {parts[0]!r}"
    )
    assert parts[1:] == [row["text"] for row in stored], (
        f"the rows under the frame are the stored ones, in the order they were "
        f"drawn, under their own bytes; got {parts[1:]!r}"
    )


def test_a_terminal_replay_carries_one_frame_line_and_a_pipe_carries_none(
    home, follow_lines, monkeypatch
) -> None:
    """The frame line is the terminal's, and it appears exactly once.

    The restored rows carry their own clocks and chain as the split they were
    drawn under, so the frame line above them is the one mark that names them as
    earlier history — a reader skimming the pane cannot tell a restored row from
    a fresh one by its time alone. A terminal gets that frame exactly once, and
    a separator below the rows would be a second piece of furniture saying what
    the rows already say. A pipe gets no frame and no burst, because it has no
    pane for a frame to explain.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow(follow_lines)
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert stored, "the baseline leaves rows to replay"
        follow_lines.clear()

        # A pipe: no frame line, and no burst for a frame to head.
        _run_follow(follow_lines)
        assert not [line for line in follow_lines if HISTORY_HEADER in line], (
            f"a re-arm to a pipe carries no frame line; got {follow_lines!r}"
        )
        assert not [line for line in follow_lines if "\n" in line], (
            f"a re-arm to a pipe carries no burst; got {follow_lines!r}"
        )

        # A terminal: the frame line, exactly once.
        follow_lines.clear()
        _run_follow(follow_lines, terminal=True)

    frames = [line for line in follow_lines if HISTORY_HEADER in line]
    assert len(frames) == 1, (
        f"a terminal replay carries exactly one frame line; got {frames!r}"
    )
    assert frames[0].split("\n")[0] == cli._HISTORY_FRAME.format(count=len(stored)), (
        f"the frame line names the rows below it; got {frames[0]!r}"
    )
    assert not [line for line in follow_lines if HISTORY_SEPARATOR in line], (
        f"no separator follows the rows; got {follow_lines!r}"
    )


def test_a_first_arming_replays_no_history(home, follow_lines) -> None:
    """A pane that was never armed has nothing to restore, and says nothing.

    The framing lines are the pane's, not the fleet's: an arming that begins a
    session must draw its rows without a header above them.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow(follow_lines)

    assert _fleet_lines(follow_lines), "the first arming draws the baseline"
    assert not [line for line in follow_lines if line.startswith(HISTORY_HEADER)], (
        follow_lines
    )
    assert not [line for line in follow_lines if HISTORY_SEPARATOR in line], (
        follow_lines
    )


def test_a_pipe_rearm_draws_the_fleet_and_no_burst(home, follow_lines) -> None:
    """A pipe re-arm draws one row per live run, not a burst of history.

    A pipe has no pane, so the stored rows are not handed back: the re-arm draws
    the fleet afresh, one line per live run. Nothing that moved means nothing
    extra, and the history burst stays the pane's alone.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow(follow_lines)
        follow_lines.clear()
        _run_follow(follow_lines)

    assert not [line for line in follow_lines if "\n" in line], (
        f"a non-TTY re-arm writes no history burst; got {follow_lines!r}"
    )
    drawn = _fleet_lines(follow_lines)
    assert [line for line in drawn if "node-a" in line], (
        f"a pipe re-arm draws the fleet's rows; got {follow_lines!r}"
    )
    assert [line for line in drawn if "node-b" in line], (
        f"a pipe re-arm draws one row for each live run; got {follow_lines!r}"
    )


def test_a_reload_marks_the_format_switch_and_re_emits_nothing(
    home, follow_lines, monkeypatch
) -> None:
    """One dim line explains why the rows below differ, and no row returns.

    A follower that reloads onto new code mid-stream has its rows already on
    screen, so it re-emits nothing; the marker stands where the format changed.
    The rows drawn before it stay in the log above it, so a later re-arm replays
    them exactly as the format that drew them left them.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow(follow_lines)
        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record, "the first arming leaves a place behind"
        follow_lines.clear()

        monkeypatch.setenv(
            cli._FOLLOWER_CHECKPOINT_ENV,
            json.dumps({"project": PROJECT, "checkpoint": record}),
        )
        _run_follow(follow_lines)
        monkeypatch.delenv(cli._FOLLOWER_CHECKPOINT_ENV, raising=False)

        log = follow_checkpoint.read_history(PROJECT, SESSION)

    markers = [
        line for line in follow_lines if follow_checkpoint.FORMAT_CHANGED_TEXT in line
    ]
    assert len(markers) == 1, (
        f"exactly one line marks the format switch; got {markers!r}"
    )
    assert _fleet_lines(follow_lines) == [], (
        f"a reload re-emits nothing; got {follow_lines!r}"
    )
    assert not [line for line in follow_lines if line.startswith(HISTORY_HEADER)], (
        f"a reload already has the pane; it restores no history; got {follow_lines!r}"
    )
    assert [row["kind"] for row in log][-1] == follow_checkpoint.FORMAT_CHANGED_KIND, (
        f"the log records where the format changed, so a later re-arm keeps the "
        f"rows above the marker as the old format drew them; got {log!r}"
    )


def test_both_history_caps_apply_and_either_governs() -> None:
    """The log is bounded by rows and by age, whichever admits fewer. The
    window is applied first, so a burst written inside one second cannot carry a
    session past a window it has already aged out of."""
    now = 1_800_000_000.0
    rows = [
        {"kind": "row", "at": now - 600 + 60 * index, "text": f"t{index}"}
        for index in range(11)
    ]
    by_rows = follow_checkpoint.cap_history(
        rows, now=now, max_rows=3, max_seconds=10**9
    )
    assert [row["text"] for row in by_rows] == ["t8", "t9", "t10"]
    by_window = follow_checkpoint.cap_history(
        rows, now=now, max_rows=100, max_seconds=180
    )
    assert [row["text"] for row in by_window] == ["t7", "t8", "t9", "t10"]
    both = follow_checkpoint.cap_history(rows, now=now, max_rows=2, max_seconds=180)
    assert [row["text"] for row in both] == ["t9", "t10"]


def test_a_zero_row_cap_keeps_no_rows() -> None:
    """A cap of zero rows admits nothing; the count must not invert.

    ``fresh[-0:]`` is the whole list, so a zero — a configuration asking for no
    stored rows — used to return every row instead of none, the opposite of what
    it asked for. Zero is the boundary the slice gets wrong, so it is checked
    beside the caps that are merely smaller.
    """
    now = 1_800_000_000.0
    rows = [
        {"kind": "row", "at": now - 600 + 60 * index, "text": f"t{index}"}
        for index in range(6)
    ]
    assert (
        follow_checkpoint.cap_history(rows, now=now, max_rows=0, max_seconds=10**9)
        == []
    )
    assert (
        follow_checkpoint.cap_history(rows, now=now, max_rows=0, max_seconds=180) == []
    )


def test_consecutive_format_markers_collapse_to_one(home) -> None:
    """Two markers with no row between them are one marker.

    A follower records a format marker each time it reloads onto new code, so
    two reloads with no row delivered in between left two markers adjacent. A
    replay then drew the switch twice, saying the drawing style changed where it
    changed once. The collapse is adjacency-only: a marker after an intervening
    row is a second genuine switch and is kept.
    """
    moment = 1_800_000_000.0
    follow_checkpoint.append_history(
        PROJECT,
        SESSION,
        text=follow_checkpoint.FORMAT_CHANGED_TEXT,
        at=moment,
        kind=follow_checkpoint.FORMAT_CHANGED_KIND,
    )
    follow_checkpoint.append_history(
        PROJECT,
        SESSION,
        text=follow_checkpoint.FORMAT_CHANGED_TEXT,
        at=moment + 1,
        kind=follow_checkpoint.FORMAT_CHANGED_KIND,
    )
    rows = follow_checkpoint.read_history(PROJECT, SESSION)
    markers = [
        row for row in rows if row["kind"] == follow_checkpoint.FORMAT_CHANGED_KIND
    ]
    assert len(markers) == 1, f"adjacent markers are one; got {rows!r}"
    burst = cli._follow_history_burst(rows, dim=str)
    assert burst.count(follow_checkpoint.FORMAT_CHANGED_TEXT) == 1, burst

    # A row between them makes the second marker a real second switch.
    follow_checkpoint.append_history(
        PROJECT, SESSION, text="a drawn row", at=moment + 2
    )
    follow_checkpoint.append_history(
        PROJECT,
        SESSION,
        text=follow_checkpoint.FORMAT_CHANGED_TEXT,
        at=moment + 3,
        kind=follow_checkpoint.FORMAT_CHANGED_KIND,
    )
    rows = follow_checkpoint.read_history(PROJECT, SESSION)
    markers = [
        row for row in rows if row["kind"] == follow_checkpoint.FORMAT_CHANGED_KIND
    ]
    assert len(markers) == 2, f"a marker after a row is a second switch; got {rows!r}"


def test_adjacent_format_markers_already_in_the_log_collapse_on_replay(home) -> None:
    """A pair of markers already on disk replays as one.

    The append collapses a *new* marker that lands on another, but a log written
    before that guard — or by another arity of the writer — can hold two markers
    already adjacent. A replay reads them back, so the collapse has to happen on
    the read as well, or the pane draws a switch that happened once as two. The
    file is written directly here, because the append is exactly the path that
    would have prevented the pair.
    """
    moment = 1_800_000_000.0
    path = follow_checkpoint.history_path(PROJECT, SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "kind": follow_checkpoint.FORMAT_CHANGED_KIND,
                    "at": moment + index,
                    "text": follow_checkpoint.FORMAT_CHANGED_TEXT,
                    "run_id": "",
                    "state": "",
                },
                sort_keys=True,
            )
            + "\n"
            for index in range(2)
        ),
        encoding="utf-8",
    )

    rows = follow_checkpoint.read_history(PROJECT, SESSION)
    markers = [
        row for row in rows if row["kind"] == follow_checkpoint.FORMAT_CHANGED_KIND
    ]
    assert len(markers) == 1, (
        f"a pair already in the log is one on a replay; got {rows!r}"
    )
    burst = cli._follow_history_burst(rows, dim=str)
    assert burst.count(follow_checkpoint.FORMAT_CHANGED_TEXT) == 1, burst


def test_the_history_caps_come_from_flight_config(monkeypatch) -> None:
    """A configured pane overrides both caps; an unreadable config falls back."""

    class Resolved:
        def __init__(self) -> None:
            self.config = {"ticker": {"history_rows": 7, "history_window": "2h"}}

    monkeypatch.setattr("reckon.flight.resolve", lambda *args, **kwargs: Resolved())
    assert cli._follow_history_caps(PROJECT) == (7, 2 * 60 * 60)

    def explode(*args, **kwargs):
        raise RuntimeError("no configuration layer is readable here")

    monkeypatch.setattr("reckon.flight.resolve", explode)
    assert cli._follow_history_caps(PROJECT) == (
        follow_checkpoint.DEFAULT_HISTORY_ROWS,
        follow_checkpoint.DEFAULT_HISTORY_SECONDS,
    )


def test_the_replayed_rows_are_dimmed_with_the_tickers_own_escape() -> None:
    """A restored row is marked dim, and the mark is the ticker's own pair.

    The mark is applied around the row's bytes rather than woven into them, so a
    replayed row carries exactly the bytes it was first drawn with, and the two
    escapes cannot drift from the ones the ticker paints with.
    """
    assert cli.HISTORY_DIM == ticker_module._DIM
    assert cli.HISTORY_RESET == ticker_module._RESET
    assert cli._dim_history_line("plain row") == (
        f"{ticker_module._DIM}plain row{ticker_module._RESET}"
    )
    block = cli._follow_history_burst(
        [{"kind": "row", "at": 0.0, "text": "plain row"}], dim=cli._dim_history_line
    )
    assert f"{cli.HISTORY_DIM}plain row{cli.HISTORY_RESET}" in block, block


def test_the_pane_memory_does_not_suppress_a_replayed_row(home) -> None:
    """A replay draws the fleet even at a state a checkpoint already named.

    The checkpoint's memory of what the pane last drew says RUN_A is at
    ``working``, and the stream records RUN_A at ``working`` too. Under the
    replay contract the row is still drawn: a re-arm shows the whole fleet, and
    a checkpoint cannot subtract a live run from it. The row carries the run's
    recorded state and time, and RUN_B is drawn beside it.
    """
    a_stamp = _iso(time.time() - 30 * 60)
    b_stamp = _iso(time.time() - 20 * 60)
    stream_events = [
        _event(
            RUN_A,
            "node-a",
            state="complete",
            observed_at=a_stamp,
            previous="working",
        ),
        _event(
            RUN_B,
            "node-b",
            state="blocked",
            observed_at=b_stamp,
            previous="working",
        ),
    ]

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=0,
            reported={RUN_A: "complete"},
        )
        _two_live_runs(home)
        _append_stream(stream_path, stream_events)

        drawn = _arm(resume=None)

    assert [str(event["run_id"]) for event in drawn] == [RUN_A, RUN_B], (
        f"a replayed row is not suppressed by the checkpoint's memory; got {drawn!r}"
    )
    by_run = {str(event["run_id"]): event for event in drawn}
    assert str(by_run[RUN_A]["to_state"]) == "complete", by_run[RUN_A]
    assert str(by_run[RUN_A]["observed_at"]) == a_stamp, by_run[RUN_A]
    assert str(by_run[RUN_B]["observed_at"]) == b_stamp, by_run[RUN_B]


def test_the_checkpoint_carries_the_identity_of_the_file_it_read(home) -> None:
    """A checkpoint pairs its offset with the file that offset was read from.

    The identity is taken from the open handle, never re-derived from the path
    at write time: a replacement landing in that window would record the new
    file's inode beside the old file's offset, and the next arming would seek
    into a stream that offset never named. The replacement here is empty, so a
    continuation would find nothing and the run would look quiet.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        stream_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
        with stream_path.open(encoding="utf-8") as stream:
            stream.readline()
            offset = stream.tell()
            identity = follow_checkpoint.identity_of(stream)
            # The path now names a different file than the handle does.
            _replace_stream(stream_path, [])
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=offset,
            reported={RUN_A: "working"},
            identity=identity,
        )
        record = follow_checkpoint.read(PROJECT, SESSION)
        replacement_identity = follow_checkpoint.stream_identity(stream_path)

    assert record["stream_identity"] == {
        "dev": identity["dev"],
        "ino": identity["ino"],
    }, f"the record must pair the offset with the file it came from; got {record!r}"
    assert record["stream_identity"] != replacement_identity, (
        "the replacement is a different file, so its identity must not be recorded"
    )
    assert follow_checkpoint.continues(record, stream_path) is False, (
        "a replaced stream cannot be continued, so the next arming restarts"
    )


# ── A reload marks the format switch only when the grid moved ───────────────

_STALE_STAMP = "0" * 64


def _plant_seat(*, code_stamp: str) -> None:
    """Leave a seat record on disk, as an external arming would."""
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "started_at": runs._utc_now(),
                "stream_path": str(runs.watch_stream_path(PROJECT)),
                "log_path": str(runs.watch_log_path(PROJECT)),
                "reckon_version": runs.__version__,
                "code_stamp": code_stamp,
            }
        )
    )


class _Clock:
    """A monotonic stand-in whose value only the wait passes advance."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _pin_follower_to_this_process(monkeypatch) -> None:
    """Judge the seat by its stamp and keep the arming alive for a live owner."""
    monkeypatch.setattr(runs, "producer_live", lambda project: True)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )


def _reload_events(monkeypatch, *, layout, stop_after: int = 2) -> list[dict]:
    """Drive one in-place reload and return every event it yields.

    The resume payload carries an offset that still names the stream, so the
    reload continues from its recorded place rather than restarting -- the mode
    whose first pass emits the format marker. ``layout`` is the grid signature
    the departed image handed to this one.
    """
    _pin_follower_to_this_process(monkeypatch)
    stream_path = runs.watch_stream_path(PROJECT)
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    stream_path.write_text("", encoding="utf-8")
    stop = threading.Event()
    sleeps = {"count": 0}

    def sleeper(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] >= stop_after:
            stop.set()

    return list(
        cli._follow_watch_lines(
            PROJECT,
            session=SESSION,
            resume={
                "offset": 0,
                "stream_path": str(stream_path),
                "reported": {},
                "layout": layout,
            },
            reloaded_in_place=True,
            poll_interval=0.001,
            sleeper=sleeper,
            stop=stop,
            on_poll=None,
            sweep=None,
        )
    )


def _format_events(events: list[dict]) -> list[dict]:
    return [
        event for event in events if event.get("event") == cli.FOLLOWER_FORMAT_EVENT
    ]


def test_a_reload_whose_grid_did_not_move_keeps_the_pane_quiet(
    home, monkeypatch
) -> None:
    """A reload that left every column where it was is not news on the pane.

    The follower reloads onto new code many times an hour; the marker exists to
    tell a reader that the rows below it were drawn differently from the rows
    above. Two images that lay a row out in the same columns drew it
    identically, so the event still reaches the JSON consumer, but the pane is
    handed no marker.
    """
    _plant_seat(code_stamp=runs.follower_code_stamp())
    events = _reload_events(monkeypatch, layout=cli._ticker_layout_signature())

    marks = _format_events(events)
    assert marks, "the JSON stream still receives the format event"
    assert marks[0]["layout_changed"] is False
    assert marks[0]["pane_line"] is False, (
        "a reload whose grid did not move printed the format marker"
    )


def test_a_reload_whose_grid_moved_marks_the_switch_once(home, monkeypatch) -> None:
    """A reload onto a different grid marks the switch, exactly once.

    The replacement's grid differs from the one the rows on screen were drawn
    with, so the reader is owed the one marker standing between old and new
    rows -- the case the marker exists for.
    """
    _plant_seat(code_stamp=runs.follower_code_stamp())
    events = _reload_events(monkeypatch, layout="a-grid-this-image-does-not-draw")

    marks = _format_events(events)
    assert len(marks) == 1, marks
    assert marks[0]["layout_changed"] is True
    assert marks[0]["pane_line"] is True


def _producer_reload_events(
    monkeypatch, *, window: float, catch_up_on=None, stop_after: int = 3
) -> list[dict]:
    """Drive a reload against a stale seat and return its events.

    ``catch_up_on`` names the wait pass at which the seat is rewritten to the
    follower's own stamp, standing in for a producer that reloaded itself
    inside its window. The clock advances one step per pass, so the window's
    edge is decided by the wait passes rather than by how fast the test runs.
    """
    _plant_seat(code_stamp=_STALE_STAMP)
    _pin_follower_to_this_process(monkeypatch)
    stream_path = runs.watch_stream_path(PROJECT)
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    stream_path.write_text("", encoding="utf-8")
    clock = _Clock()
    stop = threading.Event()
    sleeps = {"count": 0}

    def sleeper(_seconds: float) -> None:
        sleeps["count"] += 1
        if catch_up_on is not None and sleeps["count"] == catch_up_on:
            _plant_seat(code_stamp=runs.follower_code_stamp())
        clock.value += 1.0
        if sleeps["count"] >= stop_after:
            stop.set()

    return list(
        cli._follow_watch_lines(
            PROJECT,
            session=SESSION,
            reloaded_in_place=True,
            producer_reload_window=window,
            poll_interval=0.001,
            sleeper=sleeper,
            clock=clock,
            stop=stop,
            on_poll=None,
            sweep=None,
        )
    )


def _kinds(events: list[dict]) -> list[str]:
    return [str(event.get("event")) for event in events]


def test_a_producer_reload_inside_its_window_keeps_the_pane_quiet(
    home, monkeypatch
) -> None:
    """A producer that catches up inside its window keeps the pane silent.

    A follower that reloads onto new code often reads the seat before the
    producer has reloaded itself; warning then sends an operator to cycle a
    seat that would catch up on its own. The mismatch is deferred for the
    reload window, so the reloading note reaches the JSON stream but is not
    echoed, and a producer that catches up inside that window never earns the
    cycle advice at all.
    """
    events = _producer_reload_events(monkeypatch, window=1000.0, catch_up_on=1)

    kinds = _kinds(events)
    assert cli.FOLLOWER_PRODUCER_RELOADING_EVENT in kinds, kinds
    assert cli.FOLLOWER_STALE_PRODUCER_EVENT not in kinds, (
        "a producer that reloaded inside its window was reported as stale"
    )
    reloading = [
        event
        for event in events
        if event.get("event") == cli.FOLLOWER_PRODUCER_RELOADING_EVENT
    ]
    assert all(event["pane_line"] is False for event in reloading), (
        "a producer still catching up inside its window printed to the pane"
    )


def test_a_producer_reload_past_its_window_names_the_reload_then_the_remedy(
    home, monkeypatch
) -> None:
    """A producer still behind past the window is named once, then cycled.

    Once the window has passed with the mismatch still standing, the producer
    is not going to catch up on its own: the reloading note reaches the pane
    exactly once -- the earlier in-window note was JSON-only -- and the cycle
    advice follows it.
    """
    events = _producer_reload_events(monkeypatch, window=0.0, catch_up_on=None)

    kinds = _kinds(events)
    assert kinds.count(cli.FOLLOWER_STALE_PRODUCER_EVENT) == 1, kinds
    reloading = [
        event
        for event in events
        if event.get("event") == cli.FOLLOWER_PRODUCER_RELOADING_EVENT
    ]
    echoed = [event for event in reloading if event["pane_line"]]
    assert len(echoed) == 1, (
        f"an overrun prints the reloading line exactly once; got {reloading!r}"
    )
    note_index = events.index(echoed[0])
    advance_index = kinds.index(cli.FOLLOWER_STALE_PRODUCER_EVENT)
    assert note_index < advance_index, "the reloading note precedes the remedy"
