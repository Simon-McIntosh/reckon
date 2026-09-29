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
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _write_pointer(home, RUN_B, "node-b", phase="working")


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
                    state="working",
                    observed_at=a_stamp,
                    previous="dispatched",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="working",
                    observed_at=b_stamp,
                    previous="dispatched",
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
                    state="working",
                    observed_at=a_late,
                    previous="dispatched",
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="working",
                    observed_at=b_early,
                    previous="dispatched",
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
        _run_follow()
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert len(stored) == 2, stored
        follow_lines.clear()

        # A pipe: no burst at all.
        _run_follow()
        pipe_writes = [line for line in follow_lines if "\n" in line]
        assert pipe_writes == [], (
            f"a re-arm to a pipe writes no history burst; got {pipe_writes!r}"
        )

        # A terminal: the whole replay in one write, under one frame line.
        follow_lines.clear()
        monkeypatch.setattr(cli, "_follow_replay_visible", lambda: True)
        _run_follow()

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
        _run_follow()
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert stored, "the baseline leaves rows to replay"
        follow_lines.clear()

        # A pipe: no frame line, and no burst for a frame to head.
        _run_follow()
        assert not [line for line in follow_lines if HISTORY_HEADER in line], (
            f"a re-arm to a pipe carries no frame line; got {follow_lines!r}"
        )
        assert not [line for line in follow_lines if "\n" in line], (
            f"a re-arm to a pipe carries no burst; got {follow_lines!r}"
        )

        # A terminal: the frame line, exactly once.
        follow_lines.clear()
        monkeypatch.setattr(cli, "_follow_replay_visible", lambda: True)
        _run_follow()

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
        _run_follow()

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
        _run_follow()
        follow_lines.clear()
        _run_follow()

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
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=0,
            reported={RUN_A: "working"},
        )
        _two_live_runs(home)
        _append_stream(stream_path, stream_events)

        drawn = _arm(resume=None)

    assert [str(event["run_id"]) for event in drawn] == [RUN_A, RUN_B], (
        f"a replayed row is not suppressed by the checkpoint's memory; got {drawn!r}"
    )
    by_run = {str(event["run_id"]): event for event in drawn}
    assert str(by_run[RUN_A]["to_state"]) == "working", by_run[RUN_A]
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
