"""A run whose recorded pid is alive never reads gone after its first commit.

A worker that reaches its first commit mid-turn must not flip the fleet row to a
process-gone transition while its process is still running. The pointer carries
the launcher's pid and, on this host, the live process table answers for it; a
reader that infers death from the worktree's commit count alone contradicts that
table. This test drives a stub worker whose pid stays alive across one commit
past the run's base, then reads the live classifier and folds the producer's
transition over the same pointer, requiring both to agree the worker lives.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

HOST = socket.gethostname()

ORIENTATION_BODY = (
    "orientation_worktree: fixture\n"
    "orientation_base_sha: 0000000000000000000000000000000000000000\n"
    'orientation_write_paths: ["reckon/crew/recovery.py"]\n'
    "node: a-stub-node\n"
    "status: in-progress\n"
    "checkpoint: first commit landed, worker still turning\n"
)


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))


@contextlib.contextmanager
def _live_child():
    """A genuinely running child whose pid the run can record and keep alive."""
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child branch
        os.close(ready_r)
        os.close(hold_w)
        try:
            os.write(ready_w, b"1")
            os.close(ready_w)
            os.read(hold_r, 1)
        finally:
            os._exit(0)
    os.close(ready_w)
    os.close(hold_r)
    os.read(ready_r, 1)
    os.close(ready_r)
    try:
        yield pid
    finally:
        with contextlib.suppress(OSError):
            os.close(hold_w)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, check=False
    )


def _commit(repo: Path, message: str) -> None:
    ident = ("-c", "user.name=gate", "-c", "user.email=gate@example.invalid")
    _git("add", "-A", cwd=repo)
    done = _git(*ident, "commit", "-q", "-m", message, cwd=repo)
    assert done.returncode == 0, done.stderr


def _worktree_with_one_commit(tmp_path: Path) -> tuple[Path, str]:
    """A real worktree carrying exactly one commit past a recorded base."""
    repo = tmp_path / "tree"
    repo.mkdir(parents=True, exist_ok=True)
    assert _git("init", "-q", "-b", "main", cwd=repo).returncode == 0
    (repo / "delivered.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip()
    (repo / "delivered.txt").write_text("work\n", encoding="utf-8")
    _commit(repo, "work")
    return repo, base


def _pointer(
    tmp_path: Path, run_id: str, *, pid: int, worktree: Path, base: str
) -> dict:
    """One stub run shaped as a live pointer on the reading host."""
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(ORIENTATION_BODY, encoding="utf-8")
    return {
        "run_id": run_id,
        "project": "liveness-fixture",
        "session": "s21-coord",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": HOST,
        "worktree": str(worktree),
        "base_sha": base,
    }


def test_a_live_worker_with_one_commit_never_reads_gone(tmp_path: Path) -> None:
    repo, base = _worktree_with_one_commit(tmp_path)
    with _live_child() as pid:
        pointer = _pointer(
            tmp_path, "r-live-first-commit", pid=pid, worktree=repo, base=base
        )
        moment = time.time()
        row = recovery.classify_pointer(
            pointer, now_seconds=moment, stale_after_seconds=3600
        )
        snapshot = recovery._watch_snapshot(pointer, moment=moment, stall_seconds=3600)
        assert runs.process_alive(pid) is True
        assert recovery._commits_beyond_base(pointer) == 1

        # The live classifier: the pid is alive, so no interruption is inferred.
        assert row["process_alive"] is True
        assert row["interruption"] is None
        assert row["classification"] == "running"

        # The producer's transition, folded from a prior observation so a
        # transition is emitted rather than suppressed.
        prior = {
            pointer["run_id"]: {
                **snapshot,
                "state": "dispatched",
                "recovery_classification": "dispatched",
            }
        }
        current = {pointer["run_id"]: snapshot}
        folded, _ = recovery.fleet_transitions(prior, current)
        assert len(folded) == 1
        observed, previous, state, counts = folded[0]
        event = recovery._watch_transition(
            "liveness-fixture",
            kind="transition",
            snapshot=observed,
            previous=previous,
            current=state,
            counts=counts,
            spend_runs=[],
            rate_statuses={},
        )

        # No emitted transition calls a live worker gone or blocked for resume.
        assert "the worker process is gone" not in event["detail"]
        assert event["to_state"] != "blocked"
        assert event["recovery"] != "resume"
        assert event.get("process_alive") is True, "transition does not carry liveness"
        assert event.get("classification") == row["classification"]
        assert event["to_state"] == snapshot["state"]
    assert runs.process_alive(pid) is False
