"""An arm opens with one row per live run, and shows none of them twice.

The orchestrator contract is that attaching is never a blank pane: a follower
that arms while work exists draws one row per live run, so a reader sees the
fleet as it stands before the first transition arrives. A re-arm that cannot
continue its stream used to break that promise — the run policy suppressed each
baseline row against the state a checkpoint remembered for the run, so the pane
stayed empty until some run next moved. The inventory is not a re-derivation: a
state carried in from a checkpoint was never put on this pane, so it cannot
suppress the row that puts it there. Only a baseline re-derived for a run this
same arming has already drawn is a repeat, and that is what the policy drops.

The cases below drive the follower's own generator against a config home rooted
in ``tmp_path``, so the rows counted are the rows the pane prints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import follow_checkpoint, runs

PROJECT = "attach-shows-proj"
SESSION = "s-attach"
RUN_A = "r-attach-a"
RUN_B = "r-attach-b"
RUN_C = "r-attach-c"

RUNS = (RUN_A, RUN_B, RUN_C)

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


def _baseline_row(run_id: str, state: str) -> dict:
    return {
        "project": PROJECT,
        "event": "baseline",
        "run_id": run_id,
        "node": f"node-{run_id}",
        "session": SESSION,
        "from_state": None,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": runs._utc_now(),
        "legacy": False,
    }


def _stale_checkpoint(stream_path: Path, reported: dict[str, str]) -> None:
    """A checkpoint that names the runs but cannot continue this stream."""
    follow_checkpoint.write(
        PROJECT,
        SESSION,
        stream_path=stream_path,
        offset=0,
        identity={"dev": 1, "ino": 999999},
        reported=reported,
    )


# ── The attach inventory ────────────────────────────────────────────────────


def test_an_attach_with_live_runs_draws_one_baseline_each(home) -> None:
    """A re-arm that cannot continue its stream still opens with the fleet.

    The checkpoint remembers each run's state, so the arm cannot resume and the
    policy's state memory is fully populated. The inventory still prints: one
    baseline per live run, whatever the policy holds, because a state the pane
    was never shown cannot stand in for the row that shows it.
    """
    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        _stale_checkpoint(stream_path, dict.fromkeys(RUNS, "working"))

        rows = _arm()

    assert len(rows) == len(RUNS), (
        f"an attach with {len(RUNS)} live runs draws one baseline each; got {rows!r}"
    )
    assert {str(row["run_id"]) for row in rows} == set(RUNS), rows
    assert all(row["event"] == "baseline" for row in rows), rows
    assert all(row["from_state"] is None for row in rows), rows
    assert all(row["to_state"] == "working" for row in rows), rows


def test_a_mid_arming_re_derivation_does_not_reprint_a_shown_run(home) -> None:
    """A baseline re-derived for a run this arming drew is a repeat, not news.

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
        assert len(shown) == len(RUNS), f"this arm must draw the fleet: {shown!r}"

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


def test_a_reseed_keeps_the_drawn_memory(home) -> None:
    """A continuation restores the checkpoint's states without un-drawing rows.

    A reseed replaces the pane's state memory with the checkpoint's. It must not
    also clear what the pane has drawn: a baseline re-derived for a run already
    on screen is a repeat, and a reseed that forgot it would re-announce the
    fleet on every continuation.
    """
    path = cli.follower_row_path(session=SESSION, run_ids=[RUN_A])
    baseline = _baseline_row(RUN_A, "working")
    assert path.feed(baseline, now=1.0) == [baseline]
    assert path.reported.get(RUN_A) == "working"

    path.reseed({RUN_A: "working"})

    assert path.feed(baseline, now=2.0) == [], (
        "a reseed must not let a drawn run's baseline print a second time"
    )


def test_a_baseline_for_a_run_never_shown_still_prints_after_a_reseed(home) -> None:
    """The checkpoint's memory is not the drawn memory.

    A run the checkpoint names, and this pane has not drawn, must still receive
    its baseline: the restored state cannot stand in for a row the reader has
    not seen.
    """
    path = cli.follower_row_path(session=SESSION, run_ids=[RUN_B])
    path.reseed({RUN_B: "working"})
    baseline = _baseline_row(RUN_B, "working")
    assert path.feed(baseline, now=1.0) == [baseline]


# ── The Monitor is handed no history burst ──────────────────────────────────


def test_a_monitor_arm_draws_the_fleet_and_no_history_burst(home, follow_lines) -> None:
    """The fleet report is the attach; the pane history is not a Monitor's.

    A Monitor reads the follower's stdout as a pipe, so it has no scrollback to
    restore and the stored history would arrive as fresh transitions. The arm
    that re-derives the fleet must draw the report and nothing framed as
    history.
    """
    from click.testing import CliRunner

    _live_runs(home, *RUNS)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _stale_checkpoint(Path(seat["stream_path"]), dict.fromkeys(RUNS, "working"))

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

    # The follower captures its own lines through ``_echo_follow_line``; the
    # rendered fleet rows are the non-empty ones that name a node.
    rows = [line for line in follow_lines if line.strip()]
    assert any(f"node-{RUN_A}" in line for line in rows), (
        f"the fleet report must reach the Monitor; got {follow_lines!r}"
    )
    assert "── history" not in "\n".join(follow_lines), (
        f"a Monitor is handed no history burst; got {follow_lines!r}"
    )
