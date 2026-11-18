"""A completed resume supersedes its predecessor's request for help."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.obligations import obligations
from reckon.crew.ticker import PANE_STATES

HELP_TEXT = """NEEDS-HELP: choose the accepted input spelling
tried: the supplied spelling was refused
options: accept the documented spelling; or change the input
leaning: accept the documented spelling
cost-if-wrong: repeat the focused gate
"""


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.fixture
def run(tmp_path: Path, home: Path) -> dict:
    tree = tmp_path / "repository"
    (tree / "docs").mkdir(parents=True)
    (home / "mounts.json").write_text(json.dumps({"fixture": str(tree / "docs")}))
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "test@example.invalid")
    _git(tree, "config", "user.name", "Test")
    _git(
        tree,
        "commit",
        "--allow-empty",
        "-m",
        "test: seed repository\n\nEstablish the dispatch base.",
    )
    base = _git(tree, "rev-parse", "HEAD")
    (tree / "reckon").mkdir()
    (tree / "reckon" / "result.py").write_text("delivered = True\n")
    _git(tree, "add", "reckon/result.py")
    _git(tree, "commit", "-m", "fix: deliver result\n\nRecord the completed work.")
    head = _git(tree, "rev-parse", "HEAD")
    directory = runs.run_dir("resumed-delivery")
    directory.mkdir(parents=True)
    now = time.time()
    final = directory / "cli.final.txt"
    final.write_text(HELP_TEXT)
    stream = directory / "stream.jsonl"
    stream.write_text(json.dumps({"type": "result", "result": HELP_TEXT}) + "\n")
    os.utime(final, (now - 600, now - 600))
    os.utime(stream, (now - 600, now - 600))
    pointer = {
        "run_id": "resumed-delivery",
        "project": "fixture",
        "session": "fixture-session",
        "launch": "cli",
        "backend": "codex",
        "phase": "blocked",
        "process_alive": False,
        "created_at": _at(now - 900),
        "attempt": 1,
        "attempt_started_at": _at(now - 900),
        "manifest_path": str(directory / "manifest.md"),
        "manifest_baseline_mtime_ns": 0,
        "final_message_path": str(final),
        "final_message": HELP_TEXT,
        "log_path": str(stream),
        "repo": str(tree),
        "worktree": str(tree),
        "base_sha": base,
        "node": {
            "id": "resumed-delivery",
            "role": "implement",
            "write_paths": ["reckon/result.py"],
        },
    }
    manifest = Path(pointer["manifest_path"])
    manifest.write_text(
        "status: blocked\nblockers: |\n"
        + "".join(f"  {line}\n" for line in HELP_TEXT.splitlines())
    )
    os.utime(manifest, (now - 600, now - 600))
    for name, payload in {
        "attempt.json": {"attempt": 1, "attempt_started_at": _at(now - 900)},
        "attempt-1-worker.json": {
            "attempt": 1,
            "argv": ["codex", "exec", "--json", "-o", str(final)],
        },
        "exit.json": {"attempt": 1, "exit_code": 0, "exited_at": _at(now - 600)},
    }.items():
        (directory / name).write_text(
            json.dumps({"run_id": pointer["run_id"], **payload})
        )
    pointer["delivered_head"] = head
    return pointer


def _finish_resume(pointer: dict) -> None:
    directory = Path(pointer["manifest_path"]).parent
    now = time.time()
    (directory / "attempt.json").write_text(
        json.dumps(
            {
                "run_id": pointer["run_id"],
                "attempt": 2,
                "attempt_started_at": _at(now - 300),
                "attempt_kind": "resume",
            }
        )
    )
    (directory / "exit.json").write_text(
        json.dumps(
            {
                "run_id": pointer["run_id"],
                "attempt": 2,
                "exit_code": 0,
                "exited_at": _at(now - 60),
            }
        )
    )
    manifest = Path(pointer["manifest_path"])
    manifest.write_text(
        f"status: complete\ncommits: {json.dumps([pointer['delivered_head']])}\nblockers: none\n"
    )
    os.utime(manifest, (now - 90, now - 90))


def test_the_latest_complete_manifest_outranks_earlier_help(run: dict) -> None:
    _finish_resume(run)

    row = recovery.classify_pointer(run)
    snapshot = recovery._watch_snapshot(run, moment=time.time(), stall_seconds=3600)

    assert row["classification"] == "scoring", row
    assert row["recovery_classification"] == "scoring", row
    assert row["needs_help_complete"] is None
    assert snapshot["state"] in PANE_STATES["delivered"], snapshot
    assert snapshot["state"] not in PANE_STATES["help"], snapshot
    assert Path(run["final_message_path"]).read_text() == HELP_TEXT
    receipt = json.loads(
        (Path(run["manifest_path"]).parent / "attempt-1-worker.json").read_text()
    )
    assert receipt["attempt"] == 1
    assert Path(receipt["argv"][-1]).read_text() == HELP_TEXT


def test_the_only_attempts_help_still_needs_an_answer(run: dict) -> None:
    row = recovery.classify_pointer(run)

    assert row["classification"] == "blocked", row
    assert row["recovery_classification"] == "needs-help", row
    assert row["needs_help_complete"] is True
    assert "choose the accepted input spelling" in row["detail"]


def test_a_delivered_resume_replaces_the_help_obligation(run: dict) -> None:
    runs._write_json(runs.pointer_path(run["run_id"]), run)
    first = obligations("fixture", "fixture-session")["obligations"]
    assert any(item["kind"] == "needs-help" for item in first), first

    _finish_resume(run)
    latest = obligations("fixture", "fixture-session")["obligations"]

    assert not any(item["kind"] in {"needs-help", "blocked"} for item in latest), latest
    assert any(item["kind"] == "review-missing" for item in latest), latest
