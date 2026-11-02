"""A launch that found no live pointer is told so, not told to fix its PATH.

An admitted launch can find its live pointer gone between the dispatch writing
it and the supervisor's first read. The supervisor records what it saw on the
run's exit record, under ``launch_failure``. The two causes the record can name
want opposite recoveries: a command that could not be run needs the command and
the PATH it was looked for on, while a missing pointer needs the path it was
expected at and a resume. These tests classify a run from each kind of record —
no pointer, a spawn failure, and a record carrying no failure block at all —
and read the next action back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.dispatch import observe as crew_observe

FOREIGN_HOST = "another-login-node"
MISSING_POINTER = "/fixture/config/crew/live/r-launch-failure-pointer-missing.json"


def _pid_the_kernel_cannot_issue() -> int:
    """A pid no process can hold, so the liveness probe answers the same anywhere."""
    try:
        ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    except (OSError, ValueError):
        ceiling = 2**22
    return ceiling + 1024


def _build_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    launch_failure: dict[str, object] | None,
) -> None:
    """Build one launch-failed run whose exit record carries ``launch_failure``.

    The pointer is left at ``working`` and names a host this machine is not, so
    the exit record — not the process table — is what decides how the run
    ended, which is the same reading a run launched on a peer login node gets.
    """
    home = tmp_path / run_id / "config"
    run_directory = home / "crew" / "runs" / run_id
    run_directory.mkdir(parents=True)
    exit_record: dict[str, object] = {
        "run_id": run_id,
        "attempt": 1,
        "recorded_by": "supervisor",
        "worker_pid": None,
        "launched_at": "2026-10-23T14:54:20Z",
        "exited_at": "2026-10-23T14:54:50Z",
        "exit_code": None,
        "signal": None,
        "signal_name": None,
        "stream_records_seen": 0,
        "ended_during": "launch",
        "detail": "worker did not spawn",
    }
    if launch_failure is not None:
        exit_record["launch_failure"] = launch_failure
    (run_directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(exit_record), encoding="utf-8"
    )
    worktree = tmp_path / run_id / "worktree"
    worktree.mkdir(parents=True)
    pointer = {
        "run_id": run_id,
        "project": "fixture-project",
        "phase": "working",
        "attempt": 1,
        "process_alive": True,
        "pid": _pid_the_kernel_cannot_issue(),
        "launcher_host": FOREIGN_HOST,
        "manifest_path": str(tmp_path / run_id / "manifest.md"),
        "log_path": str(run_directory / "stream.jsonl"),
        "stderr_path": str(run_directory / "stderr.log"),
        "worktree": str(worktree),
        "launch": "cli",
        "command": "claude",
        "argv": ["claude", "-p", "carry the node to its manifest"],
        "backend": "fixture-lane",
        "session_id": "fixture-session",
        "node": {
            "id": run_id,
            "project": "fixture-project",
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / f"{run_id}.json").write_text(json.dumps(pointer), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))


def _action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> dict:
    observed = crew_observe(run_id)
    return recovery.classify_pointer(observed)


def test_a_missing_pointer_names_the_pointer_it_looked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-launch-failure-pointer-missing"
    _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        launch_failure={
            "kind": "pointer-missing",
            "exception_type": "CrewError",
            "message": f"no live run {run_id!r}",
            "pointer_path": MISSING_POINTER,
            "pointer_present": False,
        },
    )

    row = _action(tmp_path, monkeypatch, run_id)

    assert row["classification"] == "launch-failed"
    action = row["next_action"]
    assert MISSING_POINTER in action
    assert "reckon crew resume" in action
    # A missing pointer is not a command or PATH fault, so neither remedy may
    # be offered for it.
    assert "fix the command" not in action
    assert "PATH" not in action


def test_a_spawn_failure_keeps_the_command_and_path_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-launch-failure-spawn"
    _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        launch_failure={
            "kind": "spawn",
            "exception_type": "FileNotFoundError",
            "message": "no such file or directory: 'claude'",
            "pointer_path": f"/fixture/config/crew/live/{run_id}.json",
            "pointer_present": True,
        },
    )

    row = _action(tmp_path, monkeypatch, run_id)

    action = row["next_action"]
    assert row["classification"] == "launch-failed"
    assert "fix the command" in action
    assert "PATH" in action
    assert MISSING_POINTER not in action


def test_a_record_with_no_failure_block_keeps_the_command_and_path_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-launch-failure-unclassified"
    _build_run(tmp_path, monkeypatch, run_id=run_id, launch_failure=None)

    row = _action(tmp_path, monkeypatch, run_id)

    action = row["next_action"]
    assert row["classification"] == "launch-failed"
    assert "fix the command" in action
    assert "PATH" in action
