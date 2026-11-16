"""Supervisor stops carry their cause through classification, pane and duties."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery, runs, ticker
from tests.helpers.git import git


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "seed").write_text("seed\n")
    git(repo, "add", "seed")
    git(repo, "commit", "-qm", "test: seed the fixture")
    home = runs.crew_home()
    home.mkdir(parents=True)
    (home.parent / "mounts.json").write_text(
        json.dumps({"fixture": str(repo / "docs")})
    )
    (repo / "docs" / "state" / "fixture").mkdir(parents=True)
    directory = runs.run_dir("r-supervised")
    directory.mkdir(parents=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: supervised\nstatus: in-progress\ncheckpoint: work remains\n"
    )
    stream = directory / "stream.jsonl"
    stream.write_text('{"type":"turn.started"}\n')
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    pointer = {
        "run_id": directory.name,
        "project": "fixture",
        "session": "owner",
        "session_id": "surviving-session",
        "backend": "codex",
        "node": {"id": "supervised", "plan": "fixture-plan", "time_budget": "60m"},
        "phase": "complete",
        "attempt": 2,
        "created_at": runs._utc_now(),
        "launcher_host": socket.gethostname(),
        "pid": process.pid,
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "worktree": str(repo),
        "base_sha": git(repo, "rev-parse", "HEAD"),
    }
    (directory / "attempt.json").write_text(
        json.dumps({"run_id": directory.name, "attempt": 2})
    )
    receipt = {
        "run_id": directory.name,
        "attempt": 2,
        "worker_pid": process.pid,
        "recorded_by": "supervisor",
        "exit_code": 0,
        "signal": None,
        "ended_during": "working",
        "stream_records_seen": 4,
        "exited_at": runs._utc_now(),
        "forced_exit": {"phase": "complete", "reason": "terminal-run"},
    }
    (directory / "exit.json").write_text(json.dumps(receipt))
    runs._write_json(runs.pointer_path(directory.name), pointer)
    assert home.is_relative_to(tmp_path)
    return pointer


def _line(run: dict) -> tuple[dict, str]:
    snapshot = recovery._watch_snapshot(run, moment=time.time(), stall_seconds=900)
    event = {
        **snapshot,
        "event": "transition",
        "to_state": snapshot["state"],
        "from_state": "working",
    }
    return snapshot, ticker.Ticker(width=208, color=False).render(event)


@pytest.mark.parametrize("phase", ["complete", "working"])
def test_a_supervisor_stop_names_its_reason_in_the_pane(run: dict, phase: str) -> None:
    run["phase"] = phase
    row = recovery.classify_pointer(run)
    snapshot, line = _line(run)
    assert row["classification"] == "stopped", row["detail"]
    assert snapshot["state"] == "stopped", snapshot
    assert "→ stopped" in line and "terminal-run" in line, line
    assert "help" not in line, line
    assert len(line) <= 208


def test_a_supervisor_stop_owes_a_repair_then_resume(run: dict) -> None:
    result = obligations.obligations("fixture", "owner")
    duties = [item for item in result["obligations"] if item["run_id"] == run["run_id"]]
    stopped = [item for item in duties if item["kind"] == "force-stopped"]
    assert len(stopped) == 1, duties
    assert "terminal-run" in stopped[0]["reason"]
    command = stopped[0]["next_command"]
    assert f"reckon crew repair --run {run['run_id']} --status blocked" in command
    assert f"reckon crew resume --run {run['run_id']} --advice" in command
    assert "--gate passed" not in command


@pytest.mark.parametrize("status", ["in-progress", "blocked"])
def test_a_working_live_pid_never_reads_help(run: dict, status: str) -> None:
    run.update(phase="working", pid=os.getpid())
    Path(run["manifest_path"]).write_text(f"node: supervised\nstatus: {status}\n")
    directory = Path(run["manifest_path"]).parent
    receipt = json.loads((directory / "exit.json").read_text())
    receipt["worker_pid"] = os.getpid()
    (directory / "exit.json").write_text(json.dumps(receipt))
    (directory / "worker.json").write_text(
        json.dumps({"pid": os.getpid(), "attempt": 2})
    )
    snapshot, line = _line(run)
    assert snapshot["process_alive"] is True
    assert "→ working" in line and "help" not in line and "stopped" not in line, line


def test_a_blocked_delivery_still_reads_help(run: dict) -> None:
    Path(run["manifest_path"]).write_text(
        "node: supervised\nstatus: blocked\nblockers: [needs a decision]\n"
    )
    snapshot, line = _line(run)
    assert snapshot["state"] == "blocked", snapshot
    assert "→ help" in line and "stopped" not in line, line


def test_a_predecessor_stop_does_not_end_a_resumed_worker(run: dict) -> None:
    run.update(phase="working", attempt=3, pid=os.getpid())
    directory = Path(run["manifest_path"]).parent
    (directory / "attempt.json").write_text(json.dumps({"attempt": 3}))
    (directory / "worker.json").write_text(
        json.dumps({"pid": os.getpid(), "attempt": 3})
    )
    snapshot, line = _line(run)
    assert snapshot["process_alive"] is True
    assert "→ working" in line and "help" not in line and "stopped" not in line, line
