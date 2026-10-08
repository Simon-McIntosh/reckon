"""An arming draws the fleet's action rows, each under its run's recorded time.

The pane withholds observer context, so a run in a progress state is not drawn
at all: an arming over a working fleet opens quiet rather than announcing runs
that ask the coordinator for nothing. A run that has reached an action state is
still drawn once, stamped with the time its own stream recorded the state and
ordered by it, so a reader attaching to a fleet with work waiting sees it.

The cases below drive the follower's own generator against a config home rooted
in ``tmp_path``, so the rows counted are the rows the pane prints. The first-
arming case goes through the real command, because what it measures is the
attach experience a Monitor actually receives.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_follow_commands
from reckon.crew import runs
from reckon.crew import ticker as ticker_module

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

    monkeypatch.setattr(crew_follow_commands, "_echo_follow_line", capture)
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


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    """Append transitions to the arming's own stream, as the producer would."""
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _rendered_rows(lines: list[str]) -> list[str]:
    """The fleet lines a pane would show, without the follower's own trailer."""
    return [line for line in lines if line.strip() and "follower end" not in line]


# ── The first arming draws the fleet ────────────────────────────────────────


def test_a_first_arming_over_a_progress_state_fleet_draws_no_row(
    home, follow_lines
) -> None:
    """A first attach of working runs opens with no fleet row and no history.

    Driven through the real command, because the measure is the attach
    experience rather than the generator's yield. A run in a progress state is
    observer context — its row asks the coordinator for nothing — so the pane
    holds it back and a fleet of working runs reaches the reader as no row at
    all. A Monitor reads the follower's stdout as a pipe with no scrollback, so
    an attach row that printed would be exactly the wake this surface exists to
    remove; what it receives here is the fleet's action rows, and there are
    none.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        crew.list_live(project=PROJECT)

        # Each run's own recorded state time, written before the arm so the rows
        # carry the run's clock rather than the attach's second.
        stamps = {RUN_A: _iso(time.time() - 40 * 60), RUN_B: _iso(time.time() - 20 * 60)}
        assert ticker_module.local_clock(stamps[RUN_A]) != ticker_module.local_clock(
            stamps[RUN_B]
        )
        _append_stream(
            Path(seat["stream_path"]),
            [
                _event(
                    RUN_A,
                    f"node-{RUN_A}",
                    state="working",
                    observed_at=stamps[RUN_A],
                ),
                _event(
                    RUN_B,
                    f"node-{RUN_B}",
                    state="working",
                    observed_at=stamps[RUN_B],
                ),
            ],
        )

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
        assert drawn == [], (
            f"a progress-state run is held back on the coordinator pane; "
            f"{run_id} got {drawn!r}"
        )

    assert "── history" not in "\n".join(follow_lines), (
        f"a Monitor is handed no history burst; got {follow_lines!r}"
    )


# ── A re-arm draws the whole fleet, under its recorded times ────────────────


def test_a_rearm_that_cannot_continue_draws_each_live_run_with_recorded_times(
    home,
) -> None:
    """An arm that cannot continue the stream draws the fleet's action rows.

    The stream is replaced between the two arms, so the recorded place names no
    boundary. The arm holds back the runs in a progress state and draws the one
    that moved into an action state, carrying the recorded time the stream gave
    it — here the run that reached complete carries its transition time, and the
    run still working is observer context the pane withholds.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        first = _arm()
        assert first == [], (
            f"a first arm over progress-state runs draws no row; got {first!r}"
        )

        a_stamp = "2026-01-02T03:04:05+00:00"
        b_stamp = "2026-01-02T03:14:15+00:00"
        _replace_stream(
            stream_path,
            [
                _event(
                    RUN_A,
                    f"node-{RUN_A}",
                    state="working",
                    observed_at=a_stamp,
                    event="baseline",
                ),
                _event(
                    RUN_B,
                    f"node-{RUN_B}",
                    state="complete",
                    observed_at=b_stamp,
                    previous="working",
                ),
            ],
        )
        second = _arm(resume=None)

    assert [str(event["run_id"]) for event in second] == [RUN_B], (
        f"only the run that moved into an action state is drawn; got {second!r}"
    )
    by_run = {str(event["run_id"]): event for event in second}
    assert str(by_run[RUN_B]["observed_at"]) == b_stamp, by_run[RUN_B]
    assert str(by_run[RUN_B]["to_state"]) == "complete", by_run[RUN_B]


def test_a_mid_arming_re_derivation_does_not_reprint_a_shown_run(home) -> None:
    """A re-derivation carrying states the pane already showed is not news.

    The pane's first read holds back every progress-state run, so nothing is
    drawn for the fleet and nothing is left to reprint. A reload that re-derives
    the baseline carries the states it has already shown, and the re-derivation
    must reach no run twice — here, no run at all.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        shown = _arm()
        assert shown == [], (
            f"progress-state runs are held back, so none is shown: {shown!r}"
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
