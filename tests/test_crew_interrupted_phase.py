"""Classification of workers whose process ended without completing the run."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import node as node_module
from reckon.crew import recovery, runs


def _run(*argv: str, cwd: Path) -> str:
    result = subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _worktree_with_committed_work(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _run("git", "init", "--quiet", cwd=path)
    _run("git", "config", "user.name", "Fixture", cwd=path)
    _run("git", "config", "user.email", "fixture@example.invalid", cwd=path)
    artifact = path / "artifact.txt"
    artifact.write_text("base\n", encoding="utf-8")
    _run("git", "add", "artifact.txt", cwd=path)
    _run("git", "commit", "--quiet", "-m", "fixture base", cwd=path)
    base = _run("git", "rev-parse", "HEAD", cwd=path)
    artifact.write_text("base\ndelivered work\n", encoding="utf-8")
    _run("git", "add", "artifact.txt", cwd=path)
    _run("git", "commit", "--quiet", "-m", "fixture delivery", cwd=path)
    return path, base


def _reaped_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    phase: str = "working",
    pid: int | None = None,
    worktree: Path | None = None,
    base_sha: str = "",
    wait_status: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    if pid is None:
        pid = _reaped_pid()
    pointer = {
        "run_id": run_id,
        "project": "interruption-fixture",
        "node": {"id": run_id, "plan": "worker-recovery", "time_budget": "20m"},
        "phase": phase,
        "created_at": "2026-09-23T00:00:00Z",
        "manifest_path": str(tmp_path / f"{run_id}-manifest.md"),
        "log_path": str(tmp_path / f"{run_id}-stream.jsonl"),
        "stderr_path": str(tmp_path / f"{run_id}-stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": socket.gethostname(),
        "worktree": str(worktree or tmp_path),
        "repo": str(tmp_path),
        "base_sha": base_sha,
    }
    if wait_status is not None:
        pointer["wait_status"] = wait_status
    if session_id:
        pointer["session_id"] = session_id
    return pointer


def _sigterm_wait_status() -> dict[str, Any]:
    return {"exit_code": None, "signal": signal.SIGTERM, "signal_name": "SIGTERM"}


def test_interrupted_is_a_terminal_run_phase() -> None:
    assert "interrupted" in node_module._TERMINAL_RUN_PHASES


def test_a_signalled_run_is_interrupted_with_its_signal_and_resume_route(
    tmp_path: Path,
) -> None:
    # The liveness probe first sees this process, so its later absence is a
    # measured result rather than an instrument that returns False for every pid.
    assert runs.process_alive(os.getpid()) is True
    pointer = _pointer(
        tmp_path,
        "r-signalled",
        wait_status=_sigterm_wait_status(),
        session_id="session-survives",
    )
    assert runs.record_process_alive(pointer) is False

    row = recovery.classify_pointer(pointer)

    assert row["classification"] == "interrupted"
    assert row["interruption"]["signal"] == signal.SIGTERM
    assert row["interruption"]["signal_name"] == "SIGTERM"
    assert "SIGTERM" in row["detail"]
    assert row["recovery"] == "resume"
    assert (
        row["next_action"] == "reckon crew resume --run r-signalled --advice continue"
    )


def test_a_working_pointer_with_committed_work_and_a_dead_pid_is_interrupted(
    tmp_path: Path,
) -> None:
    worktree, base = _worktree_with_committed_work(tmp_path / "worktree")
    pointer = _pointer(
        tmp_path,
        "r-working-with-work",
        worktree=worktree,
        base_sha=base,
        session_id="session-survives",
    )

    row = recovery.classify_pointer(pointer)

    assert row["process_alive"] is False
    assert row["commits_beyond_base"] == 1
    assert row["classification"] == "interrupted"
    assert row["recovery"] == "resume"


def test_an_orphaned_pointer_with_no_recorded_exit_is_interrupted(
    tmp_path: Path,
) -> None:
    pointer = _pointer(tmp_path, "r-orphaned", phase="orphaned")

    row = recovery.classify_pointer(pointer)

    assert row["process_alive"] is False
    assert row["classification"] == "interrupted"
    assert row["interruption"]["signal_name"] is None
    assert row["recovery"] == "redispatch"
    assert "redispatch" in row["next_action"]


def test_a_stopped_pointer_stays_stopped_even_when_sigterm_ended_it(
    tmp_path: Path,
) -> None:
    row = recovery.classify_pointer(
        _pointer(
            tmp_path,
            "r-stopped",
            phase="stopped",
            wait_status=_sigterm_wait_status(),
        )
    )

    assert row["classification"] == "stopped"
    assert row["classification"] != "interrupted"


@pytest.mark.parametrize("phase", ["complete", "promoted"])
def test_a_completed_or_promoted_run_is_never_relabelled_interrupted(
    tmp_path: Path, phase: str
) -> None:
    row = recovery.classify_pointer(
        _pointer(
            tmp_path,
            f"r-{phase}",
            phase=phase,
            wait_status=_sigterm_wait_status(),
        )
    )

    assert row["classification"] != "interrupted"


def test_a_live_pid_with_its_matching_start_time_stays_working(tmp_path: Path) -> None:
    pid = os.getpid()
    pointer = _pointer(tmp_path, "r-live", pid=pid)
    expected_start = recovery._process_start_time(pid)
    assert expected_start is not None
    pointer["pid_start_time"] = expected_start

    row = recovery.classify_pointer(pointer)

    assert row["process_alive"] is True
    assert row["classification"] == "running"
    assert row["classification"] != "interrupted"
