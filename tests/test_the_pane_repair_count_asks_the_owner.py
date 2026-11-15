"""The pane and the obligations view share the repair-round verdict."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery_classification, recovery_watch, runs
from tests.helpers.git import git

PROJECT = "repair-count-fixture"
SESSION = "repair-count-session"


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Give the views an isolated mounted project and crew home."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))

    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed fixture"),
    ):
        git(root, *args)

    (config / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root, git(root, "rev-parse", "HEAD")


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _pointer(
    root: Path,
    head: str,
    run_id: str,
    *,
    pid: int,
    has_round_id: bool,
    now: datetime,
) -> dict:
    """Write one delivered run whose repair attempt has the requested facts."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = root.parent / f"{run_id}.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    stream = directory / "stream.jsonl"
    stream.write_text("", encoding="utf-8")
    os.utime(stream, (now.timestamp(), now.timestamp()))

    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(root),
        "worktree": str(root),
        "base_sha": head,
        "phase": "working",
        "launch": "spawned",
        "launcher_host": socket.gethostname(),
        "pid": pid,
        "role": "implement",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "status": "running",
        "created_at": now.isoformat(),
        "attempt_started_at": now.isoformat(),
        "resume_superseded_head": {"head": head, "at": now.isoformat()},
        "node": {
            "id": run_id,
            "plan": "fixture",
            "section": "fixture",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
        "repair_dispatch": {
            "status": "resumed",
            "round_id": f"{run_id}@{head}" if has_round_id else "",
            "at": now.isoformat(),
            "attempt": 1,
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


@pytest.mark.parametrize(
    ("label", "has_round_id", "live", "expected_repair", "expected_working"),
    [
        ("dead-attempt", True, False, False, 0),
        ("live-attempt", True, True, True, 1),
        ("missing-round-id", False, True, False, 1),
    ],
    ids=["dead-attempt", "live-attempt", "missing-round-id"],
)
def test_pane_repair_count_uses_the_owner_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    has_round_id: bool,
    live: bool,
    expected_repair: bool,
    expected_working: int,
) -> None:
    """Only an identified round with a live process is counted as repair."""
    root, head = _environment(tmp_path, monkeypatch)
    now = datetime.now(tz=UTC)
    monkeypatch.setattr(obligations, "_utc_now", lambda: now)
    monkeypatch.setattr(recovery_classification, "_utc_seconds", now.timestamp)
    recovery_watch._SNAPSHOT_CACHE.clear()
    run_id = f"r-{label}"
    pointer = _pointer(
        root,
        head,
        run_id,
        pid=os.getpid() if live else _dead_pid(),
        has_round_id=has_round_id,
        now=now,
    )

    snapshot = recovery_watch._compute_watch_snapshot(
        pointer, moment=now.timestamp(), stall_seconds=60
    )
    pane_counts = recovery_watch._fleet_counts({run_id: snapshot})
    owner_rows = obligations.obligations(PROJECT, SESSION)["obligations"]
    owner_says_repair = any(
        row["run_id"] == run_id and row["kind"] == obligations.REPAIR_ROUND_DUTY_KIND
        for row in owner_rows
    )

    assert snapshot["repair_in_flight"] is expected_repair, snapshot
    assert owner_says_repair is expected_repair, owner_rows
    assert snapshot["repair_in_flight"] is owner_says_repair, snapshot
    assert pane_counts["working"] == expected_working, pane_counts
