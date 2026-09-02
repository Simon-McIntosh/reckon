"""Hermetic recovery and project-watch stream contracts."""

from __future__ import annotations

import importlib
import json
import subprocess
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


def _terminal_pointer(home: Path, tmp_path: Path, run_id: str) -> dict:
    worktree = tmp_path / run_id
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    (worktree / "result.txt").write_text("base\n")
    subprocess.run(["git", "add", "result.txt"], cwd=worktree, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test: establish fixture",
        ],
        cwd=worktree,
        check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "complete",
        "created_at": crew._utc_now(),
        "manifest_path": str(home / "runs" / run_id / "manifest.md"),
        "log_path": str(home / "runs" / run_id / "stream.jsonl"),
        "stderr_path": str(home / "runs" / run_id / "stderr.log"),
        "worktree": str(worktree),
        "base_sha": base,
        "final_message": None,
        "process_alive": False,
    }
    crew._write_json(crew.pointer_path(run_id), pointer)
    return pointer


def test_recover_derives_repair_evidence_without_promotion_advice(
    home, tmp_path, monkeypatch
) -> None:
    pointer = _terminal_pointer(home, tmp_path, "r-repairable")
    (Path(pointer["worktree"]) / "result.txt").write_text("correct work\n")
    pointer["final_message"] = "Implemented the requested behavior and tests passed."
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(
        project="proj", config={"fences": {"manifest_required": True}}
    )

    row = result["runs"][0]
    manifest = Path(pointer["manifest_path"])
    text = manifest.read_text()
    assert "derived: true" in text
    assert "changed_paths: result.txt" in text
    assert "Implemented the requested behavior" in text
    assert row["classification"] == "abandoned"
    assert row["manifest_derived"] is True
    assert row["manifest_present"] is False
    assert "reckon crew complete" not in row["next_action"]
    assert row["next_action"].startswith("reckon crew resume --run r-repairable")
    gap = crew.read_pointer(pointer["run_id"])["delivery_gap"]
    assert gap["kind"] == "missing-worker-manifest"
    assert gap["derived_manifest_path"] == str(manifest)
    assert gap["final_message_present"] is True
    assert gap["changed_paths"] == ["result.txt"]


def test_recover_leaves_an_empty_terminal_run_abandoned(
    home, tmp_path, monkeypatch
) -> None:
    pointer = _terminal_pointer(home, tmp_path, "r-empty")
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(project="proj")

    row = result["runs"][0]
    assert row["classification"] == "abandoned"
    assert row["manifest_derived"] is False
    assert not Path(pointer["manifest_path"]).exists()
    assert "delivery_gap" not in crew.read_pointer(pointer["run_id"])


def test_recover_never_overwrites_a_worker_manifest(
    home, tmp_path, monkeypatch
) -> None:
    pointer = _terminal_pointer(home, tmp_path, "r-delivered")
    manifest = Path(pointer["manifest_path"])
    manifest.parent.mkdir(parents=True)
    delivered = "node: r-delivered\nstatus: complete\ncommits: abc123\n"
    manifest.write_text(delivered)
    pointer["manifest_baseline_mtime_ns"] = 0
    pointer["final_message"] = "A final message that must not replace delivery."
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(project="proj")

    assert manifest.read_text() == delivered
    assert result["runs"][0]["classification"] == "completed_unpromoted"
    assert result["runs"][0]["manifest_derived"] is False
    assert "delivery_gap" not in crew.read_pointer(pointer["run_id"])


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
