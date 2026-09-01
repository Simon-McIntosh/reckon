"""Hermetic state-transition ticker contracts for a watched fleet."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import recovery


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move all pointers and watcher records into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(stream),
            "process_alive": None,
        },
    )


def _set_phase(run_id: str, phase: str) -> None:
    pointer = crew.read_pointer(run_id)
    pointer["phase"] = phase
    crew._write_json(crew.pointer_path(run_id), pointer)


def _deliver(home: Path, run_id: str, status: str, *, blocker: str = "") -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: ticker-node\nstatus: {status}\ncommits: HEAD\n"
        f"blockers: {blocker or 'none'}\n"
    )


def _event(**overrides) -> dict:
    event = {
        "project": "proj",
        "event": "transition",
        "observed_at": "2026-08-24T19:35:45Z",
        "run_id": "r-ticker",
        "node": "ticker-node",
        "from_state": "working",
        "to_state": "blocked",
        "working": 3,
        "blocked": 1,
        "unpromoted": 0,
        "reason": "first clause",
    }
    event.update(overrides)
    return event


def test_ticker_opens_with_one_baseline_per_live_run(home) -> None:
    _write_pointer(home, "r-first", "first-node", phase="starting")
    _write_pointer(home, "r-second", "second-node", phase="working")
    stream = recovery.watch_ticker("proj", stall_window="1h")

    try:
        baseline = [next(stream), next(stream)]
    finally:
        stream.close()

    assert [event["event"] for event in baseline] == ["baseline", "baseline"]
    assert [event["node"] for event in baseline] == ["first-node", "second-node"]
    assert [event["from_state"] for event in baseline] == [None, None]
    assert [event["to_state"] for event in baseline] == ["dispatched", "working"]
    assert {
        (event["working"], event["blocked"], event["unpromoted"]) for event in baseline
    } == {(2, 0, 0)}


def test_ticker_emits_only_changes_and_ends_after_the_last_promotion(home) -> None:
    _write_pointer(home, "r-existing", "existing-node", phase="starting")
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            return
        if sleeps == 2:
            _write_pointer(home, "r-new", "new-node", phase="starting")
        elif sleeps == 3:
            _set_phase("r-new", "working")
        elif sleeps == 4:
            _deliver(
                home,
                "r-new",
                "blocked",
                blocker="dependency unavailable; retry after configuration changes",
            )
        elif sleeps == 5:
            _deliver(home, "r-new", "complete")
        elif sleeps == 6:
            crew.pointer_path("r-new").unlink()
        elif sleeps == 7:
            crew.pointer_path("r-existing").unlink()
        else:
            pytest.fail("ticker did not end after the last pointer was reconciled")

    stream = recovery.watch_ticker(
        "proj", stall_window="1h", poll_interval=0, sleeper=advance
    )
    baseline = next(stream)
    transitions = list(stream)

    assert baseline["event"] == "baseline"
    assert [
        (event["node"], event["from_state"], event["to_state"]) for event in transitions
    ] == [
        ("new-node", None, "dispatched"),
        ("new-node", "dispatched", "working"),
        ("new-node", "working", "blocked"),
        ("new-node", "blocked", "complete"),
        ("new-node", "complete", "promoted"),
        ("existing-node", "dispatched", "promoted"),
    ]
    # Each triple is the fleet after that transition, and the three buckets
    # partition it — a blocked or delivered run leaves the working count, which
    # is what a reader takes "working" to mean.
    assert [
        (event["working"], event["blocked"], event["unpromoted"])
        for event in transitions
    ] == [
        (2, 0, 0),
        (2, 0, 0),
        (1, 1, 0),
        (1, 0, 1),
        (1, 0, 0),
        (0, 0, 0),
    ]
    assert transitions[2]["reason"] == "dependency unavailable"
    assert sleeps == 7


def test_ticker_line_is_compact_and_bounds_free_text_to_one_clause() -> None:
    line = recovery.format_watch_transition(
        _event(reason="first clause; second clause that must not reach the terminal")
    )

    # The stamp is stored UTC and rendered in the reader's own zone, because
    # the pane sits beside a harness that timestamps locally.
    # clock, then what ran it, then which node — the agent column sits between,
    # because a reader scanning a wave compares agents down a column.
    assert line.startswith(recovery.local_clock(_event()["observed_at"]))
    assert line.index("ticker-node") < line.index("working → blocked")
    assert "working → blocked" in line
    assert "3 working · 1 blocked · 0 unpromoted" in line
    assert line.endswith("· first clause")
    assert "second clause" not in line
    assert "\n" not in line


def test_cli_follow_prints_compact_transition_lines_by_default(
    home, monkeypatch
) -> None:
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow"],
    )

    assert result.exit_code == 0
    assert result.output.count("\n") == 1
    assert "ticker-node" in result.output
    assert "working → blocked" in result.output
    assert not result.output.startswith("{")


def test_cli_follow_keeps_machine_objects_behind_json_flag(home, monkeypatch) -> None:
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow", "--json"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["node"] == "ticker-node"
    assert payload["from_state"] == "working"
    assert payload["to_state"] == "blocked"
    assert (payload["working"], payload["blocked"], payload["unpromoted"]) == (3, 1, 0)


def test_cli_watch_follows_without_being_asked(home, monkeypatch) -> None:
    """Following is the default, because the seat is what dispatch requires.

    A watcher that returns after one event releases the seat, so every landing
    would have to be followed by a re-arm before the next dispatch could run.
    """
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj"],
    )

    assert result.exit_code == 0
    assert "ticker-node" in result.output
    assert "working → blocked" in result.output


def test_cli_watch_returns_after_one_event_only_when_asked(home, monkeypatch) -> None:
    called: dict[str, object] = {}

    def _single(project, **kwargs):
        called["project"] = project
        called["kwargs"] = kwargs
        return {
            "project": project,
            "event": "empty",
            "run_id": None,
            "classification": "no_live_pointers",
            "next_action": "none",
        }

    monkeypatch.setattr(crew, "watch", _single)

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--once"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["event"] == "empty"
    assert called["project"] == "proj"


def test_cli_watch_treats_exit_on_empty_as_selecting_single_event(
    home, monkeypatch
) -> None:
    """The flag only means anything to the single-event mode.

    Ignoring it under the new default would silently follow forever for a
    caller that explicitly asked to be told about an empty fleet.
    """
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: pytest.fail("follow must not run here"),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--exit-on-empty"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["event"] == "empty"
    assert payload["classification"] == "no_live_pointers"


def test_the_ticker_states_the_model_and_effort_that_ran_the_node(home) -> None:
    """A reader judging progress needed this and had to leave the ticker for it.

    Read from the configuration persisted at dispatch, never from current flight
    config: a later config change must not silently restate what ran. A partial
    label is still useful, so an absent field is omitted rather than invented.
    """
    _write_pointer(home, "r-agent", "agent-node", phase="working")
    pointer = crew.read_pointer("r-agent")
    pointer["agent"] = {
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "launch": "cli",
    }
    crew._write_json(crew.pointer_path("r-agent"), pointer)

    snapshot = recovery._watch_snapshot(
        crew.read_pointer("r-agent"), moment=recovery._utc_seconds(), stall_seconds=900
    )
    assert snapshot["agent"] == "gpt-5.6-sol/high"

    line = recovery.format_watch_transition(
        _event(agent=snapshot["agent"], to_state="working", from_state="dispatched")
    )
    assert "gpt-5.6-sol/high" in line
    # Between the clock and the node: what ran it belongs with when, because a
    # reader scanning a wave compares agents down a column.
    assert line.index("gpt-5.6-sol/high") < line.index("ticker-node")
    assert line.index("gpt-5.6-sol/high") > line.index(
        recovery.local_clock(_event()["observed_at"])
    )

    # Half a label beats none; no label at all renders without a stray column.
    assert recovery.agent_label({"agent": {"model": "gpt-5.6-sol"}}) == "gpt-5.6-sol"
    assert recovery.agent_label({"agent": {"effort": "high"}}) == "high"
    assert recovery.agent_label({}) == ""
    assert "  ·" not in recovery.format_watch_transition(_event(agent=""))
