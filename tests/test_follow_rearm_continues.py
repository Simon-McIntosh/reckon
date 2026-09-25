"""A re-armed follower continues the stream it left instead of replaying it.

A follower's checkpoint used to travel only through the environment an in-place
process reload hands to its replacement, so a re-arm — a new process, no such
environment — began from nothing. It replayed a baseline row for every live run,
each stamped with the moment it attached, and it started reading at the stream's
current end, so every transition written while nothing was attached was skipped.
The reader saw a batch of rows under one timestamp and then a gap.

These tests drive the follower's own generator against a synthetic config home
and a real producer, because the property is about what a *second* arming emits
after a *first* one has ended. The stream's own recorded timestamps are read
into the expected rows before the second arming runs, so a row stamped with the
attach time cannot pass by matching a time the test itself chose.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import follow_checkpoint, recovery, runs
from reckon.crew import ticker as ticker_module

PROJECT = "rearm-proj"
SESSION = "s1"
RUN_A = "r-rearm-a"
RUN_B = "r-rearm-b"

# The follower's own lifetime for each arming, short enough to keep the test
# quick and long enough for its first wait passes to deliver and record. The
# stream poll is tighter than the host's, so an arming reaches its deadline
# within a poll of granting it.
ARM_LIFETIME = 0.35

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


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


def _replace_stream(stream_path: Path, events: list[dict]) -> None:
    """Put a fresh stream at the same path, so its identity changes with it."""
    replacement = stream_path.with_name(stream_path.name + ".replacement")
    replacement.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8"
    )
    os.replace(replacement, stream_path)


def _two_live_runs(home: Path) -> None:
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _write_pointer(home, RUN_B, "node-b", phase="working")


# ── The gap is delivered, and only the gap ──────────────────────────────────


def test_a_rearm_delivers_the_gap_with_the_stream_timestamps_and_no_baseline(
    home,
) -> None:
    """The second arming continues: exactly the transitions written while away.

    The first arming delivers two baseline rows. It ends at its lifetime; two
    more transitions are written with nothing attached; the second arming starts
    with no environment checkpoint, as a fresh process does. It must emit
    exactly those gap transitions, in order and carrying the stream's own
    timestamps, and no row at all for a run whose state did not move.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)

        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first
        before = len(_read_stream(stream_path))

        _deliver(home, RUN_A, "complete")
        crew.list_live(project=PROJECT)
        gap = _gap_events(stream_path, before)
        assert [event["run_id"] for event in gap] == [RUN_A], gap
        recorded_at = str(gap[0]["observed_at"])

        second = _arm(resume=None)

    assert [event["run_id"] for event in second] == [RUN_A], (
        f"only the run whose state moved may be re-announced; got {second!r}"
    )
    assert all(event.get("event") != "baseline" for event in second), (
        f"a continuation emits no baseline; got {second!r}"
    )
    row = second[0]
    assert str(row["observed_at"]) == recorded_at, (
        f"the replayed row is stamped with the stream's own time, not the "
        f"attach time; recorded {recorded_at!r}, got {row['observed_at']!r}"
    )
    # The stamp is the row's rendered clock, not merely a confusingly equal
    # field: the line a reader sees carries the recorded time.
    line = recovery.format_watch_transition(row)
    assert line.startswith(ticker_module.local_clock(recorded_at)), (
        f"the rendered row does not carry the recorded time; {line!r}"
    )
    # The state is the one the stream recorded, not a re-derived verdict.
    assert str(row["to_state"]) == str(gap[0]["to_state"]), row


def test_a_rearm_with_nothing_new_prints_nothing(home) -> None:
    """A continuation with no intervening transition is silent, not a replay."""
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first

        second = _arm(resume=None)

    assert second == [], f"a re-arm with nothing new must print nothing; got {second!r}"


def test_a_replaced_stream_emits_only_moved_runs_with_recorded_times(home) -> None:
    """When the stream cannot be continued, state decides, never re-derivation.

    The file is replaced between the two arms, so the recorded offset no longer
    names a boundary. The follower falls back to state: it emits the run whose
    state differs from its checkpoint and stays quiet about the unchanged one,
    each row carrying the stream's own recorded transition time. No run that is
    merely running renders dispatched.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        first = _arm()
        assert {event["run_id"] for event in first} == {RUN_A, RUN_B}, first

        a_stamp = "2026-01-02T03:04:05+00:00"
        b_stamp = "2026-01-02T03:14:15+00:00"
        _replace_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="working",
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

    assert [event["run_id"] for event in second] == [RUN_B], (
        f"only the run whose state differs from the checkpoint is emitted; "
        f"got {second!r}"
    )
    row = second[0]
    assert str(row["observed_at"]) == b_stamp, (
        f"the row carries the run's recorded transition time, never the attach "
        f"time; got {row['observed_at']!r}"
    )
    assert str(row["to_state"]) == "complete", row
    assert all(str(event.get("to_state")) != "dispatched" for event in second), (
        f"no already-running run renders dispatched; got {second!r}"
    )


def test_a_rearm_records_its_own_place_for_the_next(home) -> None:
    """The durable checkpoint advances as the follower delivers.

    After a continuation the checkpoint's offset must sit at the stream's end,
    so a third arming is quiet. This asserts the record itself rather than a
    third arming, because the offset is the fact the next arming resumes from.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _arm()
        _deliver(home, RUN_A, "complete")
        crew.list_live(project=PROJECT)

        second = _arm(resume=None)
        assert [event["run_id"] for event in second] == [RUN_A], second

        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record, "a continuation leaves a durable checkpoint behind"
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
