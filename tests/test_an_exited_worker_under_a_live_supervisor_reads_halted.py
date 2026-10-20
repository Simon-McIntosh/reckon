"""A worker that exited under a live supervisor is read halted, not working.

The pid a pointer records is the supervisor's for a supervised launch, and a
supervisor outlives the worker it spawned: it stays alive after the worker
exits, so a process table answering "alive" for that pid answers about the
launcher rather than about the work. The run's own exit record is the account
of the worker's end, and the liveness reading must fold it in or the run sits
in the working column with its exit recorded and its work committed.

Every case builds its own run directory under a temporary configuration home
and its own git worktree under the case's temporary directory, so nothing here
reads or writes the machine's live fleet.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_liveness, runs

HOST = socket.gethostname()

IN_PROGRESS_MANIFEST = (
    "node: the-node\n"
    "status: in-progress\n"
    "checkpoint: work committed, verdict word not yet replaced\n"
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every case at a temporary configuration home, and prove it."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)


def _git(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *argv], capture_output=True, check=False, text=True
    )


def _worktree_with_commit(tmp_path: Path) -> tuple[Path, str]:
    """A real worktree carrying one commit past the revision it records as base."""
    repo = tmp_path / "tree"
    repo.mkdir(parents=True)
    assert _git("init", "-q", "-b", "main", cwd=repo).returncode == 0
    ident = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")
    (repo / "delivered.txt").write_text("base\n", encoding="utf-8")
    _git(*ident, "add", "delivered.txt", cwd=repo)
    assert _git(*ident, "commit", "-q", "-m", "base", cwd=repo).returncode == 0
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "delivered.txt").write_text("base\ndelivered\n", encoding="utf-8")
    assert _git(*ident, "commit", "-aqm", "work", cwd=repo).returncode == 0
    return repo, base


def _reaped_pid() -> int:
    """A pid this host has seen and reaped, so its absence is a measurement."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _run(
    tmp_path: Path,
    run_id: str,
    *,
    supervisor_pid: int,
    worker_pid: int,
    exit_record: dict | None,
    attempt_record: int | None = None,
) -> dict:
    """A run whose pointer records ``supervisor_pid`` and whose directory records
    the worker and, when given, the supervisor's account of its exit."""
    repo, base = _worktree_with_commit(tmp_path)
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(IN_PROGRESS_MANIFEST, encoding="utf-8")
    log = directory / "stream.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "assistant",
                "session_id": "fixture-session",
                "timestamp": "2026-10-01T22:00:00.000Z",
                "message": {"content": [{"type": "text", "text": "work committed"}]},
            }
        )
        + "\n"
        + json.dumps(
            {"type": "result", "subtype": "success", "session_id": "fixture-session"}
        )
        + "\n",
        encoding="utf-8",
    )
    if exit_record is not None:
        (directory / recovery.EXIT_RECORD_NAME).write_text(
            json.dumps(exit_record), encoding="utf-8"
        )
    if attempt_record is not None:
        (directory / recovery_liveness.ATTEMPT_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": attempt_record,
                    "attempt_started_at": "2026-10-01T21:07:53Z",
                }
            ),
            encoding="utf-8",
        )
    (directory / recovery_liveness.WORKER_RECORD_NAME).write_text(
        json.dumps({"pid": worker_pid, "attempt": attempt_record or 1}),
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": "state-fixture",
        "session": "s-fixture",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "60m"},
        "phase": "working",
        "created_at": "2026-10-01T21:07:53Z",
        "launcher_host": HOST,
        "pid": supervisor_pid,
        "process_alive": True,
        "manifest_path": str(manifest),
        "log_path": str(log),
        "stderr_path": str(directory / "stderr.log"),
        "worktree": str(repo),
        "base_sha": base,
    }
    if attempt_record is not None:
        pointer["attempt"] = attempt_record
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _exit_record(run_id: str, *, attempt: int, worker_pid: int) -> dict:
    return {
        "run_id": run_id,
        "attempt": attempt,
        "signal": None,
        "signal_name": None,
        "exit_code": 0,
        "ended_during": "working",
        "last_record_type": "result",
        "recorded_by": "supervisor",
        "exited_at": "2026-10-01T22:22:33Z",
        "stream_records_seen": 7314,
        "worker_pid": worker_pid,
    }


def test_a_worker_that_exited_under_a_live_supervisor_reads_halted(
    tmp_path: Path,
) -> None:
    """The measured defect: the supervisor's pid is alive, the worker's exit is
    recorded, and the run sat in the working column for ninety minutes.
    """
    run_id = "r-exited-under-live-supervisor"
    worker_pid = _reaped_pid()
    pointer = _run(
        tmp_path,
        run_id,
        supervisor_pid=os.getpid(),
        worker_pid=worker_pid,
        exit_record=_exit_record(run_id, attempt=1, worker_pid=worker_pid),
        attempt_record=1,
    )

    row = recovery.classify_pointer(pointer)
    assert row["process_alive"] is False
    assert row["exit_record"] is not None
    assert row["classification"] == "exited-unfinished", row["detail"]
    assert row["recovery"] == recovery.RECOVERY_VERBS["exited-unfinished"]
    # The composed command carries the run it repairs and the status to set —
    # `reckon crew repair --run <id> --status complete --reason <…>`. The
    # subcommand's own spelling is not this test's business; the flags are what
    # the reader is handed either way.
    assert f"--run {run_id}" in row["next_action"], row["next_action"]
    assert "--status complete" in row["next_action"], row["next_action"]

    snapshot = recovery._watch_snapshot(
        pointer, moment=1_800_000_000.0, stall_seconds=900
    )
    assert snapshot["state"] == "exited-unfinished"
    assert snapshot["recovery_classification"] == "exited-unfinished"
    # The obligation the coordinator owes: the run is in the pane's needs-action
    # vocabulary and counted in the blocked bucket, drawn halted.
    assert snapshot["state"] in recovery.NEEDS_ACTION
    counts = recovery._fleet_counts({run_id: snapshot})
    assert counts.get("blocked") == 1
    assert counts.get("working") == 0


def test_a_worker_that_still_runs_keeps_its_working_reading(tmp_path: Path) -> None:
    """The control in the other direction: no exit recorded and a live worker
    record, so the run is working and owes nothing.
    """
    run_id = "r-still-running"
    pointer = _run(
        tmp_path,
        run_id,
        supervisor_pid=os.getpid(),
        worker_pid=os.getpid(),
        exit_record=None,
    )

    row = recovery.classify_pointer(pointer)
    assert row["process_alive"] is True
    assert row["classification"] == "running", row["detail"]

    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=900)
    assert snapshot["state"] == "working"
    counts = recovery._fleet_counts({run_id: snapshot})
    assert counts.get("working") == 1


def test_a_record_naming_a_worker_that_still_runs_does_not_end_the_attempt(
    tmp_path: Path,
) -> None:
    """The record answers for the worker it names, and that worker is here.

    A supervisor that left an exit record beside a worker which went on running
    — the launcher that ended while the worker it adopted kept going — is the
    shape a recorded end cannot decide. The record names a live process, so the
    live reading stands and the run is working.
    """
    run_id = "r-record-names-a-live-worker"
    pointer = _run(
        tmp_path,
        run_id,
        supervisor_pid=os.getpid(),
        worker_pid=os.getpid(),
        exit_record=_exit_record(run_id, attempt=1, worker_pid=os.getpid()),
        attempt_record=1,
    )

    row = recovery.classify_pointer(pointer)
    assert row["process_alive"] is True
    assert row["exit_record"] is None
    assert row["classification"] == "running", row["detail"]

    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=900)
    assert snapshot["state"] == "working"


def test_the_record_decides_by_the_worker_it_names_not_by_the_worker_record(
    tmp_path: Path,
) -> None:
    """The record answers for the worker it names, not for whatever else is on
    the run directory.

    The two records can name different processes: the worker record is the
    attempt's own account of the process it spawned, and the exit record is the
    supervisor's account of the process it held the wait for. Where they
    disagree, the recorded end is the record's own worker that has left, and a
    live pid sitting on the worker record does not revive it.
    """
    run_id = "r-record-names-a-worker-other-than-the-worker-record"
    pointer = _run(
        tmp_path,
        run_id,
        supervisor_pid=os.getpid(),
        worker_pid=os.getpid(),
        exit_record=_exit_record(run_id, attempt=1, worker_pid=_reaped_pid()),
        attempt_record=1,
    )

    row = recovery.classify_pointer(pointer)
    assert row["process_alive"] is False
    assert row["exit_record"] is not None
    assert row["classification"] == "exited-unfinished", row["detail"]


def test_a_predecessor_attempts_exit_does_not_end_the_attempt_now_running(
    tmp_path: Path,
) -> None:
    """A resumed run reuses its run directory, so the earlier attempt's exit
    record must not call the worker now running dead.
    """
    run_id = "r-resumed-past-a-predecessor-exit"
    pointer = _run(
        tmp_path,
        run_id,
        supervisor_pid=os.getpid(),
        worker_pid=os.getpid(),
        exit_record=_exit_record(run_id, attempt=1, worker_pid=_reaped_pid()),
        attempt_record=2,
    )

    row = recovery.classify_pointer(pointer)
    assert row["classification"] == "running", row["detail"]
