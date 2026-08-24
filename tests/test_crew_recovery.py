"""Hermetic recovery and project-watch stream contracts."""

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
    """Move every crew pointer and watcher claim into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, *, terminal: bool) -> None:
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    if terminal:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: {run_id}-commit\n"
        )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": "complete" if terminal else "working",
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "process_alive": None,
        },
    )


def _finish(home: Path, run_id: str) -> None:
    pointer = crew.read_pointer(run_id)
    manifest = Path(pointer["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {run_id}\nstatus: complete\ncommits: {run_id}-commit\n")
    pointer["phase"] = "complete"
    crew._write_json(crew.pointer_path(run_id), pointer)


def test_follow_watch_emits_three_terminal_runs_once_then_ends(home) -> None:
    _write_pointer(home, "r-first", terminal=True)
    _write_pointer(home, "r-second", terminal=False)
    _write_pointer(home, "r-third", terminal=False)
    sleeps = 0

    def advance_fleet(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _finish(home, "r-second")
        elif sleeps == 2:
            _finish(home, "r-third")
        elif sleeps == 3:
            for run_id in ("r-first", "r-second", "r-third"):
                crew.pointer_path(run_id).unlink()
        else:
            pytest.fail("follow watch did not end when the fleet emptied")

    events = list(
        recovery.watch_follow(
            "proj", stall_window="1h", poll_interval=0, sleeper=advance_fleet
        )
    )

    assert [event["run_id"] for event in events] == [
        "r-first",
        "r-second",
        "r-third",
    ]
    assert [event["event"] for event in events] == ["terminal"] * 3
    assert sleeps == 3


def test_unpromoted_run_does_not_repeat_or_mask_a_later_terminal_run(home) -> None:
    _write_pointer(home, "r-unpromoted", terminal=True)
    _write_pointer(home, "r-later", terminal=False)
    sleeps = 0

    def finish_later(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _finish(home, "r-later")
        elif sleeps == 2:
            assert crew.pointer_path("r-unpromoted").is_file()
            crew.pointer_path("r-unpromoted").unlink()
            crew.pointer_path("r-later").unlink()
        else:
            pytest.fail("an already reported pointer woke the watcher again")

    events = list(
        recovery.watch_follow(
            "proj", stall_window="1h", poll_interval=0, sleeper=finish_later
        )
    )

    assert [event["run_id"] for event in events] == ["r-unpromoted", "r-later"]
    assert sleeps == 2


def test_follow_watch_arms_on_empty_before_the_first_pointer(home) -> None:
    sleeps = 0

    def deliver_then_reconcile(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _write_pointer(home, "r-first", terminal=True)
        elif sleeps == 2:
            crew.pointer_path("r-first").unlink()
        else:
            pytest.fail("watch did not stop after reconciling the first fleet")

    events = list(
        recovery.watch_follow(
            "proj", stall_window="1h", poll_interval=0, sleeper=deliver_then_reconcile
        )
    )

    assert [event["run_id"] for event in events] == ["r-first"]
    assert sleeps == 2


def test_cli_follow_streams_each_event_as_one_json_document(home, monkeypatch) -> None:
    events = (
        {"project": "proj", "event": "terminal", "run_id": run_id}
        for run_id in ("r-one", "r-two")
    )
    monkeypatch.setattr(recovery, "watch_follow", lambda *_args, **_kwargs: events)

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow"],
    )

    payloads = [json.loads(line) for line in result.output.splitlines()]
    assert result.exit_code == 0
    assert [payload["run_id"] for payload in payloads] == ["r-one", "r-two"]
    assert all(payload["ok"] is True for payload in payloads)


def test_single_event_watch_still_returns_the_first_terminal_run(home) -> None:
    _write_pointer(home, "r-first", terminal=True)
    _write_pointer(home, "r-second", terminal=True)

    event = crew.watch("proj", stall_window="1h")

    assert event["event"] == "terminal"
    assert event["run_id"] == "r-first"
    assert crew.pointer_path("r-first").is_file()
    assert crew.pointer_path("r-second").is_file()
