"""Attaching draws the fleet once; a re-arm prints only what moved since.

The contract has two halves, and they are easy to confuse. A session's *first*
arming has no checkpoint to continue, so it opens with one line per live run —
a reader attaching for the first time is never looking at a blank pane. Every
*later* arming for that session continues from where the previous one stopped:
it prints what moved while nothing was attached, and it re-announces nothing it
has already shown. A re-arm with nothing new is therefore silent, and
deliberately so — the fleet as it stands is a live read, not a replay.

The cases below drive the follower's own generator against a config home rooted
in ``tmp_path``, so the rows counted are the rows the pane prints. The first-
arming case goes through the real command, because what it measures is the
attach experience a Monitor actually receives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from reckon.crew import runs

PROJECT = "attach-shows-proj"
SESSION = "s-attach"
RUN_A = "r-attach-a"
RUN_B = "r-attach-b"

RUNS = (RUN_A, RUN_B)

# Long enough for an arming to reach the stream and write its place on a loaded
# host, short enough that a suite of them stays quick.
ARM_LIFETIME = 0.75

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def follow_lines(monkeypatch):
    """Capture the follower command's own rendered lines instead of writing them."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(cli, "_echo_follow_line", capture)
    return lines


def _write_pointer(home: Path, run_id: str, *, session: str, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": f"node-{run_id}", "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _live_runs(home: Path, *run_ids: str) -> None:
    for run_id in run_ids:
        _write_pointer(home, run_id, session=SESSION, phase="working")


def _arm(**kwargs) -> list[dict]:
    """Run one arming to its own lifetime and collect the fleet rows it drew."""
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=ARM_LIFETIME,
        **kwargs,
    )
    return [event for event in generator if event.get("event") in _FLEET_EVENTS]


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
    """Put a fresh stream at the same path, so its identity no longer matches.

    A replaced file has a new inode, which is what the checkpoint compares, so
    the recorded place names no boundary and the arm cannot continue the stream.
    """
    replacement = stream_path.with_name(stream_path.name + ".replacement")
    replacement.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8"
    )
    os.replace(replacement, stream_path)


def _rendered_rows(lines: list[str]) -> list[str]:
    """The fleet lines a pane would show, without the follower's own trailer."""
    return [line for line in lines if line.strip() and "follower end" not in line]


# ── The first arming draws the fleet ────────────────────────────────────────


def test_a_first_arming_under_a_monitor_draws_the_fleet(home, follow_lines) -> None:
    """A session's first attach opens with one row per live run, no history.

    Driven through the real command, because the measure is the attach
    experience rather than the generator's yield: a Monitor reads the follower's
    stdout as a pipe and has no scrollback to restore, so it must receive the
    fleet and no frame of stored history. With no checkpoint to continue, the
    arm derives the fleet from the live pointers.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)

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

    rows = _rendered_rows(follow_lines)
    for run_id in RUNS:
        drawn = [line for line in rows if f"node-{run_id}" in line]
        assert len(drawn) == 1, (
            f"a first arming draws one row per live run; {run_id} got {drawn!r}"
        )
        assert "working" in drawn[0], drawn[0]
        assert "→" not in drawn[0], (
            f"a first attach draws the fleet as it stands, not a transition; got {drawn[0]!r}"
        )

    assert "── history" not in "\n".join(follow_lines), (
        f"a Monitor is handed no history burst; got {follow_lines!r}"
    )


# ── A re-arm prints only what moved ─────────────────────────────────────────


def test_a_rearm_that_cannot_continue_emits_only_what_moved(home) -> None:
    """An arm that cannot continue decides by state, never by re-derivation.

    The stream is replaced between the two arms, so the recorded place names no
    boundary. The follower falls back to state: it emits the run that differs
    from its checkpoint and stays quiet about the unchanged one. No run that is
    merely running is re-announced with a baseline.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        first = _arm()
        assert {event["run_id"] for event in first} == set(RUNS), first

        _replace_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    f"node-{RUN_A}",
                    state="working",
                    observed_at="2026-01-02T03:04:05+00:00",
                    event="baseline",
                ),
                _event(
                    RUN_B,
                    f"node-{RUN_B}",
                    state="complete",
                    observed_at="2026-01-02T03:14:15+00:00",
                    previous="working",
                ),
            ],
        )
        second = _arm(resume=None)

    moved = [str(event["run_id"]) for event in second]
    assert moved == [RUN_B], f"only the moved run may print; got {second!r}"
    assert all(event["event"] != "baseline" for event in second), second


def test_a_mid_arming_re_derivation_does_not_reprint_a_shown_run(home) -> None:
    """A re-derivation carrying states the pane already showed is not news.

    The pane drew the fleet on its first read. A reload that re-derives the
    baseline carries the states it has already shown, and the re-derivation must
    reach no run twice.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        shown = _arm()
        assert {event["run_id"] for event in shown} == set(RUNS), (
            f"this arm must draw the fleet: {shown!r}"
        )

        # The reload's continuation names a stream that is no longer the one at
        # this path, so the plan re-derives the baseline — carrying the states
        # the pane already showed.
        again = _arm(
            resume={
                "reported": dict.fromkeys(RUNS, "working"),
                "stream_path": str(stream_path.with_name("stream.gone")),
                "offset": 0,
            }
        )

    assert again == [], (
        f"no run already shown this arming may be printed twice; got {again!r}"
    )
