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
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
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

        early = time.time() - 25 * 60
        later = time.time() - 12 * 60
        a_stamp = _iso(early)
        b_stamp = _iso(later)
        assert ticker_module.local_clock(a_stamp) != ticker_module.local_clock(b_stamp)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="abandoned",
                    observed_at=a_stamp,
                    previous="dispatched",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="blocked",
                    observed_at=b_stamp,
                    previous="working",
                ),
            ],
        )
        gap = _gap_events(stream_path, before)
        assert [str(event["run_id"]) for event in gap] == [RUN_A, RUN_B], gap

        second = _arm(resume=None)

    assert [event["run_id"] for event in second] == [RUN_A, RUN_B], (
        f"the gap is delivered in order; got {second!r}"
    )
    assert all(event.get("event") != "baseline" for event in second), (
        f"a continuation emits no baseline; got {second!r}"
    )
    assert [str(event["observed_at"]) for event in second] == [a_stamp, b_stamp], (
        f"the replayed rows carry the stream's own past stamps, not the attach "
        f"time; wanted {[a_stamp, b_stamp]!r}, got "
        f"{[event['observed_at'] for event in second]!r}"
    )
    # The state is the one the stream recorded, not a re-derived verdict.
    assert [str(event["to_state"]) for event in second] == ["abandoned", "blocked"], (
        second
    )
    # The stamp reaches the reader as the line's own clock, not merely as a field
    # beside it: a row stamped with the attach time would render today's second.
    for row, stamp in zip(second, (a_stamp, b_stamp), strict=True):
        line = recovery.format_watch_transition(row)
        assert line.startswith(ticker_module.local_clock(stamp)), (
            f"the rendered row does not carry the run's recorded time; {line!r}"
        )


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


def test_a_rearm_after_a_quiet_baseline_arming_prints_nothing(home) -> None:
    """A baseline arming against a quiet stream still leaves a place behind.

    The baseline rows are derived from the live fleet, not read from the stream,
    so an arming can emit them having read no line at all. Here the producer
    holds its claim but has not written a line, so the stream this arming would
    read does not exist and the read loop that advances the place is never
    entered. If the place were written only as lines are delivered — or only
    from a wait pass inside that read loop — the arming would leave nothing
    behind, and the next arming would find no checkpoint and replay the
    baseline: the same replay this section removes, on the quiet path.
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

    assert second == [], (
        f"a re-arm after a quiet baseline arming must print nothing; got {second!r}"
    )


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


# ── The pane's memory across a re-arm ───────────────────────────────────────
#
# A re-arm starts with an empty pane, so without a log of what was already drawn
# the reader's view empties every time the host re-arms it. The log holds each
# rendered row's own bytes, the stamp it carried and the run and state it drew;
# the re-arm replays those rows above its own fresh ones, in one write. The rows
# carry their own clocks and fall straight onto the split they restored, so the
# replay is framed by nothing: a header above it and a separator below it each
# cost a line on every re-arm and said only what the rows already say.

HISTORY_HEADER = "── history"
HISTORY_SEPARATOR = "── re-armed"


@pytest.fixture()
def follow_lines(monkeypatch):
    """Capture the follower command's own lines instead of writing them out."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(cli, "_echo_follow_line", capture)
    return lines


def _run_follow() -> None:
    """Arm the real follower command once, to its own short lifetime."""
    result = CliRunner().invoke(
        cli.crew,
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
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


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


def test_a_rearm_replays_the_history_in_one_write(home, follow_lines) -> None:
    """A re-arm restores the pane: the rows in order, in a single write.

    Order and the single write are asserted together because they are one
    property: the reader is handed its whole view as one event, and the rows
    inside it are the ones it last saw, oldest first, under their own clocks.
    The write is located as the one line carrying a newline, so a header or a
    separator appearing above or below the rows is a second event rather than
    part of this one.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert len(stored) == 2, stored
        follow_lines.clear()

        _run_follow()

    writes = [line for line in follow_lines if "\n" in line]
    assert len(writes) == 1, (
        f"the whole replay is one write, so exactly one multi-row line reaches "
        f"the reader; got {writes!r}"
    )
    assert writes[0] == "\n".join(row["text"] for row in stored), (
        f"the replayed rows are the stored ones, in the order they were drawn, "
        f"under their own bytes; got {writes[0]!r}"
    )


def test_a_replay_emits_no_banner_line(home, follow_lines) -> None:
    """A replay is its rows and nothing else: no header, no separator.

    A banner above the restored rows and a separator below them each cost a
    line of furniture on every re-arm while telling the reader only what the
    rows' own clocks and their chaining already say. The replay must carry the
    rows alone, so the check is that neither mark appears anywhere in what a
    re-arm writes — measured on the follower's own output, not on the composer.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert stored, "the baseline leaves rows to replay"
        follow_lines.clear()

        _run_follow()

    assert any("\n" in line for line in follow_lines), (
        f"the re-arm replayed nothing, so the check would be vacuous; "
        f"got {follow_lines!r}"
    )
    bannered = [
        line
        for line in follow_lines
        if HISTORY_HEADER in line or HISTORY_SEPARATOR in line
    ]
    assert bannered == [], (
        f"a replay carries its rows alone, with no header above them and no "
        f"separator below; got {bannered!r}"
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
        _run_follow()

    assert _fleet_lines(follow_lines), "the first arming draws the baseline"
    assert not [line for line in follow_lines if line.startswith(HISTORY_HEADER)], (
        follow_lines
    )
    assert not [line for line in follow_lines if HISTORY_SEPARATOR in line], (
        follow_lines
    )


def test_a_rearm_with_nothing_new_replays_only_the_history(home, follow_lines) -> None:
    """The replayed pane is the whole view when the stream has not moved."""
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        follow_lines.clear()
        _run_follow()

    writes = [line for line in follow_lines if "\n" in line]
    assert len(writes) == 1, (
        f"a re-arm with no intervening transition replays the stored rows in "
        f"one write and draws nothing fresh; got {follow_lines!r}"
    )
    fresh = [line for line in follow_lines if "\n" not in line]
    assert all("follower end" in line for line in fresh), (
        f"the only single-row lines are the follower's own end marker; got {fresh!r}"
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
        _run_follow()
        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record, "the first arming leaves a place behind"
        follow_lines.clear()

        monkeypatch.setenv(
            cli._FOLLOWER_CHECKPOINT_ENV,
            json.dumps({"project": PROJECT, "checkpoint": record}),
        )
        _run_follow()
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


def test_the_pane_memory_suppresses_a_state_it_already_showed(home) -> None:
    """A run at the state the pane last drew is not a transition.

    The stream's own record says the run moved; the pane's memory of what it
    drew says the reader has already seen that state. The memory governs, which
    is what keeps a run's rows chaining across a re-arm rather than restarting
    from the producer's record.

    The same stream is armed twice, once with the memory present and once
    without, so the suppression is shown to be the memory's doing rather than
    the row never having been deliverable. Both arms start from the same
    checkpoint, so the checkpoint cannot be the thing doing the suppressing.
    """
    a_stamp = _iso(time.time() - 30 * 60)
    b_stamp = _iso(time.time() - 20 * 60)
    stream_events = [
        _event(
            RUN_A,
            "node-a",
            state="working",
            observed_at=a_stamp,
            previous="dispatched",
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
        _two_live_runs(home)
        _append_stream(stream_path, stream_events)
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=0,
            reported={RUN_A: "working"},
        )

        without_memory = _arm(resume=None)
        assert [str(event["run_id"]) for event in without_memory] == [RUN_B], (
            f"the control arm draws the run the checkpoint does not name; "
            f"got {without_memory!r}"
        )

        # The pane's memory: the row it drew for RUN_B at the state it holds now.
        follow_checkpoint.append_history(
            PROJECT,
            SESSION,
            text="drawn earlier",
            at=time.time(),
            run_id=RUN_B,
            state="blocked",
        )
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=0,
            reported={RUN_A: "working"},
        )
        with_memory = _arm(resume=None)

    assert with_memory == [], (
        f"a run at the state the pane last drew is not a transition; "
        f"got {with_memory!r}"
    )


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
