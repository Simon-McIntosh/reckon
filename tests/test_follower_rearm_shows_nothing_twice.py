"""A re-armed follower shows no transition it has already shown.

A follower persists the rows it drew, so a re-arm can restore the pane a reader
was watching. Two defects made that restore read as news.

The first is the same, every arming. A re-arm draws the fleet itself — one row
per live run, at the recorded time that run entered its current state — and on
top of that had been printing the stored history unframed, so a consumer that
reads the stream line by line was handed rows it had already acted on and could
not tell them from fresh transitions. The fix is that the stored history is
handed only to a terminal, under one dim frame line that names it as earlier
history; there is no scrollback to fill when the reader is a pipe, which is
handed only the fleet rows and the gap transitions.

The second is cross-session and much longer. A stream written by an older
producer carries rendered text lines rather than transition objects. Such a
line names no session and no run, and the follower admitted it to every reader
regardless of ``--session``; it then recorded those rows against its own
session, stamped with the moment of the read rather than the clock the row
carried, so the four-hour window that bounds the log chose the read time and the
rows never aged out. Every later re-arm replayed them — hours of another
session's transitions, under ``--session`` that asked for none of them.

The cases below drive the real command and the follower's own generator against
a config home rooted in ``tmp_path``, and prove the isolation at the end rather
than assuming it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from reckon.crew import follow_checkpoint, runs

PROJECT = "rearm-replay-proj"
SESSION = "s-replay"
OTHER_SESSION = "s-other"
RUN_A = "r-replay-a"
RUN_C = "r-replay-c"

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})

# Long enough for an arming to reach the stream and write its place on a loaded
# host, short enough that a suite of them stays quick.
ARM_LIFETIME = 0.75


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture(autouse=True)
def _a_live_owner(monkeypatch):
    """Isolate every arming from an owner stamped into the ambient environment.

    A review, or any other process a follower launches, inherits
    ``RECKON_FOLLOWER_OWNER`` naming the follower's own pid and outlives it once
    the follower exits on its lifetime. An arming that read that owner as its
    own ends at its first wait pass and never sees a row written after it, so a
    file that passes under a plain shell fails under a follower. The variable is
    removed before each case, and the resolved owner cleared with it: the
    identity is cached on the module after its first read, so a value read by an
    earlier case would otherwise decide every later one. The previous cache is
    restored after, so nothing here leaks into another file in the same process.
    """
    previous = runs._RESOLVED_FOLLOWER_OWNER.resolved
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    runs._RESOLVED_FOLLOWER_OWNER.resolved = None
    yield
    runs._RESOLVED_FOLLOWER_OWNER.resolved = previous


@pytest.fixture()
def follow_lines(monkeypatch):
    """Capture the follower command's own lines instead of writing them out."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(cli, "_echo_follow_line", capture)
    return lines


def _write_pointer(home: Path, run_id: str, node: str, *, session: str, phase: str):
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _discard_pointer(run_id: str) -> None:
    path = crew.pointer_path(run_id)
    if path.exists():
        path.unlink()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _event(
    run_id: str,
    node: str,
    *,
    session: str,
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
        "session": session,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": observed_at,
        "legacy": False,
    }


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _append_legacy_line(stream_path: Path, text: str) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


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


def _fleet_rows(lines: list[str]) -> list[str]:
    """The pane's drawn rows, without the follower's own end marker."""
    return [line for line in lines if "follower end" not in line and line.strip()]


def _arm_generator(*, stop: threading.Event | None = None, **kwargs) -> list[dict]:
    """Run one follower generator and collect the fleet events it draws."""
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=ARM_LIFETIME,
        stop=stop,
        **kwargs,
    )
    deadline = time.monotonic() + 10
    collected: list[dict] = []
    for event in generator:
        if event.get("event") in _FLEET_EVENTS:
            collected.append(event)
        if stop is not None and collected:
            stop.set()
        if time.monotonic() > deadline:
            break
    return collected


def _dead_owner() -> str:
    """An owner identity the arming must read as gone.

    The pid belongs to a child that has exited and been reaped, which is exactly
    what a follower's owner becomes once the follower has exited on its
    lifetime. The start time is one no live process can carry, so a pid the
    kernel has since reused is still read as gone and the check cannot pass by
    luck.
    """
    child = subprocess.Popen([sys.executable, "-c", ""])
    pid = child.pid
    child.wait()
    return runs._format_follower_owner((pid, "0"))


def _count_polls() -> int:
    """Arm once and count the wait passes it reaches."""
    polls = 0

    def on_poll(checkpoint=None):
        nonlocal polls
        polls += 1

    _arm_generator(on_poll=on_poll)
    return polls


# ── Case 1: a re-arm draws the fleet, whether or not anything moved ─────────


def test_a_pipe_rearm_with_nothing_new_draws_the_fleet(home, follow_lines) -> None:
    """A re-arm draws each live run once, even when nothing moved.

    The first arming draws the baseline and leaves a place behind. Nothing moves
    after the stop, so no gap transition is waiting. The re-arm still draws the
    live run — one row, at the run's own recorded state time — so a reader that
    re-arms is never handed a blank pane. The run is drawn exactly once: it is
    not also re-delivered as a transition, and the stored history is not replayed
    for a pipe.
    """
    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)

        _run_follow()
        assert follow_checkpoint.read_history(PROJECT, SESSION), (
            "the first arming must leave rows to replay, or this check is vacuous"
        )
        follow_lines.clear()

        _run_follow()

    rows = _fleet_rows(follow_lines)
    assert len(rows) == 1, (
        f"a re-arm draws its one live run exactly once; got {rows!r}"
    )
    assert "node-a" in rows[0], rows[0]
    assert "working" in rows[0], rows[0]


# ── Case 2: a non-TTY re-arm delivers exactly the new transitions ───────────


def test_a_pipe_rearm_delivers_only_the_transitions_written_while_away(
    home, follow_lines
) -> None:
    """The gap is news; the history is not. A pipe gets one and not the other."""
    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        stream_path = Path(_seat["stream_path"])
        _run_follow()
        follow_lines.clear()

        stamp = _iso(time.time() - 15 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    session=SESSION,
                    state="abandoned",
                    observed_at=stamp,
                    previous="working",
                )
            ],
        )
        _run_follow()

    rows = _fleet_rows(follow_lines)
    assert len(rows) == 1, f"exactly the one gap transition; got {follow_lines!r}"
    assert "node-a" in rows[0]
    assert "── history" not in rows[0], f"a pipe is handed no burst; got {rows!r}"


def test_a_pipe_rearm_delivers_each_new_transition_once(home) -> None:
    """Past the checkpoint, each transition past it is delivered once."""
    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        stream_path = Path(_seat["stream_path"])
        _arm_generator()

        stamp = _iso(time.time() - 10 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    session=SESSION,
                    state="blocked",
                    observed_at=stamp,
                    previous="working",
                )
            ],
        )
        second = _arm_generator(resume=None)

    assert [str(event["run_id"]) for event in second] == [RUN_A], second
    assert [str(event["to_state"]) for event in second] == ["blocked"], second


# ── Case 3: a TTY re-arm restores the pane under one frame line ─────────────


def test_a_terminal_rearm_shows_the_burst_under_one_frame_line(
    home, follow_lines, monkeypatch
) -> None:
    """A terminal is the reader the replay is for, and the frame names it.

    The pane a reader watches has scrollback to restore, so the stored rows are
    the whole view when nothing moved. They arrive in one write, under a single
    dim frame line that says the rows below it are earlier history — a restored
    row is then never read as a fresh transition.
    """
    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        stored = follow_checkpoint.read_history(PROJECT, SESSION)
        assert stored, "the first arming leaves rows to restore"
        follow_lines.clear()

        monkeypatch.setattr(cli, "_follow_replay_visible", lambda: True)
        _run_follow()

    writes = [line for line in _fleet_rows(follow_lines) if "\n" in line]
    assert len(writes) == 1, (
        f"a terminal re-arm restores the pane in exactly one write; got "
        f"{follow_lines!r}"
    )
    parts = writes[0].split("\n")
    assert parts[0] == cli._HISTORY_FRAME.format(count=len(stored)), (
        f"the burst opens with one frame line naming its row count; got {parts[0]!r}"
    )
    assert [row["text"] for row in stored] == parts[1:], (
        f"the rows under the frame are the stored ones, in order and under "
        f"their own bytes; got {parts[1:]!r}"
    )


def test_the_replay_gate_reads_the_reader_that_is_watching(monkeypatch) -> None:
    """The gate is the reader's own stream: a pipe no, a terminal yes."""

    class Pipe:
        def isatty(self):
            return False

    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdout", Pipe())
    assert cli._follow_replay_visible() is False
    monkeypatch.setattr(cli.sys, "stdout", Terminal())
    assert cli._follow_replay_visible() is True


# ── Case 4: another session's rows never reach a scoped follower ────────────


def test_another_sessions_rows_and_legacy_lines_are_not_this_followers(home) -> None:
    """A scoped follower claims only rows that name its session.

    The stream holds three kinds of line: this session's own transition, another
    session's transition, and a rendered line an older producer wrote, which
    names neither. Only the first may be drawn. The own row is present, so the
    absence reported above is a filter doing its work and not a stream the
    follower never read.
    """
    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        stamp = _iso(time.time() - 30 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    session=SESSION,
                    state="working",
                    observed_at=stamp,
                    previous="dispatched",
                ),
                _event(
                    "r-other",
                    "other-node",
                    session=OTHER_SESSION,
                    state="working",
                    observed_at=stamp,
                    previous="dispatched",
                ),
            ],
        )
        _append_legacy_line(
            stream_path, "10:01:00  legacy-node  dispatched -> working  1 live"
        )
        follow_checkpoint.write(
            PROJECT,
            SESSION,
            stream_path=stream_path,
            offset=0,
            reported={},
        )
        drawn = _arm_generator(resume=None)

    run_ids = [str(event["run_id"]) for event in drawn]
    assert RUN_A in run_ids, (
        f"the own row must be drawn, or the absence is vacuous: {drawn!r}"
    )
    assert "r-other" not in run_ids, f"another session's row leaked; got {drawn!r}"
    assert all(not event.get("legacy") for event in drawn), (
        f"an unattributable legacy line reached a scoped follower; got {drawn!r}"
    )


# ── Case 5: a flapping run discarded before the stop is not re-emitted ──────


def test_a_flapping_run_discarded_before_the_stop_is_not_re_emitted(
    home, follow_lines
) -> None:
    """Rows the pane already drew are not news after the run is gone.

    A run flaps stalled then blocked and is then discarded, drawing its
    withdrawal — all while an arming is still running. The pane drew every one
    of those rows, and the run no longer exists for a baseline to re-derive. The
    re-arm that follows the stop must hand the reader none of it: not a
    re-derived state, because the run is gone from the live fleet, and not the
    stored history, which is not a pipe's to replay.
    """
    _write_pointer(home, RUN_C, "node-c", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _run_follow()
        assert any(
            "node-c" in row["text"]
            for row in follow_checkpoint.read_history(PROJECT, SESSION)
        )

        a_stamp = _iso(time.time() - 12 * 60)
        b_stamp = _iso(time.time() - 11 * 60)
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_C,
                    "node-c",
                    session=SESSION,
                    state="stalled",
                    observed_at=a_stamp,
                    previous="working",
                ),
                _event(
                    RUN_C,
                    "node-c",
                    session=SESSION,
                    state="blocked",
                    observed_at=b_stamp,
                    previous="stalled",
                ),
            ],
        )
        _run_follow()
        assert [line for line in follow_lines if "node-c" in line], (
            f"the flapping rows must be drawn before the discard: {follow_lines!r}"
        )

        # The discard and its withdrawal row are drawn while an arming is still
        # running, so they are news the reader has already had. The stop comes
        # after, and the re-arm must show nothing for the run that is now gone.
        _discard_pointer(RUN_C)
        crew.list_live(project=PROJECT)
        _run_follow()
        assert [line for line in follow_lines if "withdrawn" in line], (
            f"the withdrawal is drawn before the stop: {follow_lines!r}"
        )
        follow_lines.clear()

        _run_follow()

    assert [line for line in _fleet_rows(follow_lines) if "node-c" in line] == [], (
        f"a discarded run's rows must not be re-emitted; got {follow_lines!r}"
    )


# ── A dead owner in the environment is isolated, as this file requires ──────


def test_a_dead_owner_in_the_environment_is_isolated(home, monkeypatch) -> None:
    """The file holds under the environment a follower hands its children.

    A follower stamps its own pid into ``RECKON_FOLLOWER_OWNER`` for the
    processes it launches, and one it launched outlives it once the follower
    exits on its lifetime. An arming that read that owner as its own ends at its
    first wait pass and never sees a row written after it, which is how the
    withdrawal case failed under a follower. The variable is set to a reaped pid
    here, so the hazard is live rather than assumed, and the isolation every
    case relies on is then applied by hand: the arming reports to a live process
    and idles for its whole lifetime, exactly as it does under a plain shell.
    """
    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)

        monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, _dead_owner())
        runs._RESOLVED_FOLLOWER_OWNER.resolved = None
        ended = _count_polls()

        monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
        runs._RESOLVED_FOLLOWER_OWNER.resolved = None
        isolated = _count_polls()

    assert ended <= 1, (
        f"a dead owner must end the arming at its first wait pass, or this "
        f"control is vacuous; got {ended} polls"
    )
    assert isolated >= 12, (
        f"with the owner isolated the arming idles for its whole lifetime; "
        f"got {isolated} polls"
    )


# ── Isolation: no write reaches the real watch directory ────────────────────


def _real_watch_artifacts() -> list[Path]:
    saved = os.environ.pop("RECKON_HOME", None)
    try:
        return [
            runs.follower_dir(PROJECT),
            follow_checkpoint.checkpoint_path(PROJECT, SESSION),
            follow_checkpoint.history_path(PROJECT, SESSION),
            runs.watch_stream_path(PROJECT),
        ]
    finally:
        if saved is not None:
            os.environ["RECKON_HOME"] = saved


def test_the_real_watch_directory_is_untouched(home, follow_lines, monkeypatch) -> None:
    """Every crew directory resolves into tmp_path; the real one gains nothing."""
    artifacts = _real_watch_artifacts()
    for artifact in artifacts:
        assert not artifact.exists(), f"a real-home artifact pre-exists: {artifact}"

    _write_pointer(home, RUN_A, "node-a", session=SESSION, phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        monkeypatch.setattr(cli, "_follow_replay_visible", lambda: True)
        _run_follow()

    for artifact in artifacts:
        assert not artifact.exists(), f"the real home gained a file: {artifact}"

    # The temp home did receive the artifacts, so the absence above is not a
    # run that wrote nowhere at all.
    assert follow_checkpoint.checkpoint_path(PROJECT, SESSION).exists()
    assert follow_checkpoint.history_path(PROJECT, SESSION).exists()
