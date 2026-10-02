"""The record keeps the reading the worker actually reached.

Three readings a run's record could not state before, each measured from
evidence the run already holds. A worker that exits after committing its work
but before replacing its working status leaves a delivery no verdict word
claims, and the harness refuses a hand edit of the worker's manifest — so the
run reads as its own state and the coordinator gets a sanctioned way to replace
the missing verdict while keeping the delivered file. And a worker that parks
on an external condition publishes a declaration the fleet's own reader can
refuse; the stop hook accepts exactly the declarations that reader honours,
rather than forcing a parked worker to write a status it cannot honestly
claim.

Every case builds its own run directory under a temporary configuration home
and its own git worktree under the case's temporary directory, so nothing here
reads or writes the machine's live fleet.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import recovery, runs
from reckon.hooks import worker_stop as stop_hook

HOST = socket.gethostname()

IN_PROGRESS_MANIFEST = (
    "node: the-node\n"
    "status: in-progress\n"
    "checkpoint: work committed, verdict word not yet replaced\n"
)

WAITING_MANIFEST = (
    "node: the-node\n"
    "status: waiting\n"
    "wait_condition: scheduler jobs 1279884 and 1279885 leave the queue\n"
    'wait_probe: ["squeue", "-h", "-j", "1279884"]\n'
    'wait_terminal: ["COMPLETED", "FAILED"]\n'
    "resume_brief: collect the scheduler result and carry the node to its gate\n"
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
    """A real worktree carrying one commit past the revision it records as its base."""
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


def _exited_pointer(tmp_path: Path, run_id: str, repo: Path, base: str) -> dict:
    """A run whose supervisor recorded the worker's own exit after a result.

    The pointer names this host and a pid the process table has answered for,
    so liveness is a reading rather than a carried answer, and the run directory
    holds the supervisor's account of the end.
    """
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
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": 1,
                "signal": None,
                "signal_name": None,
                "exit_code": 0,
                "ended_during": "working",
                "last_record_type": "result",
                "recorded_by": "supervisor",
                "exited_at": "2026-10-01T22:22:33Z",
                "stream_records_seen": 7314,
                "worker_pid": 4084534,
            }
        ),
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
        "pid": _reaped_pid(),
        "process_alive": False,
        "manifest_path": str(manifest),
        "log_path": str(log),
        "stderr_path": str(directory / "stderr.log"),
        "worktree": str(repo),
        "base_sha": base,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def test_an_exited_worker_with_committed_work_and_an_open_status_reads_its_own_state(
    tmp_path: Path,
) -> None:
    """The measured defect: this run read as a block, not as what it is: the
    worker exited with its work committed and its manifest's status word never
    replaced, so the record's own state names that rather than a mode of death.
    """
    repo, base = _worktree_with_commit(tmp_path)
    run_id = "r-exited-unfinished"
    pointer = _exited_pointer(tmp_path, run_id, repo, base)

    row = recovery.classify_pointer(pointer)
    assert row["process_alive"] is False
    assert row["commits_beyond_base"] == 1
    assert row["classification"] == "exited-unfinished", row["detail"]
    assert row["classification"] not in {"blocked", "unreadable"}
    assert row["recovery"] == recovery.RECOVERY_VERBS["exited-unfinished"]
    assert "repair-status" in row["next_action"]

    snapshot = recovery._watch_snapshot(
        pointer, moment=1_800_000_000.0, stall_seconds=900
    )
    assert snapshot["state"] == "exited-unfinished"
    assert snapshot["recovery_classification"] == "exited-unfinished"
    counts = recovery._fleet_counts({run_id: snapshot})
    assert sum(counts.values()) == 1
    assert counts.get("blocked") == 1


def test_repair_status_replaces_the_word_and_keeps_the_delivered_manifest(
    tmp_path: Path,
) -> None:
    """The coordinator's remedy for the state above, end to end through the CLI."""
    run_id = "r-repair-status"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(IN_PROGRESS_MANIFEST, encoding="utf-8")
    reason = "the worker exited with the work committed"

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "repair-status",
            "--run",
            run_id,
            "--status",
            "complete",
            "--reason",
            reason,
        ],
    )
    assert result.exit_code == 0, result.output

    assert manifest.read_text(encoding="utf-8") == (
        IN_PROGRESS_MANIFEST.replace("status: in-progress", "status: complete")
    )
    as_delivered = directory / "manifest.md.asdelivered"
    assert as_delivered.read_text(encoding="utf-8") == IN_PROGRESS_MANIFEST
    record = json.loads((directory / "status-repair.json").read_text(encoding="utf-8"))
    assert record["reason"] == reason
    assert record["status"] == "complete"
    assert record["previous_status"] == "in-progress"


def test_repair_status_refuses_a_manifest_with_no_status_line(tmp_path: Path) -> None:
    """The repair shows its refusal: a manifest it cannot resolve is never half-edited."""
    run_id = "r-repair-no-status"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.md").write_text("node: the-node\n", encoding="utf-8")

    with pytest.raises(runs.CrewError) as refusal:
        runs.repair_manifest_status(run_id, "complete", "repair the open word")

    assert "status line" in str(refusal.value)
    assert not (directory / "manifest.md.asdelivered").exists()


def test_the_stop_hook_accepts_a_waiting_manifest_with_a_complete_wait_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked turn ends on its record; the declaration is honoured, not refused."""
    manifest = tmp_path / "manifest.md"
    manifest.write_text(WAITING_MANIFEST, encoding="utf-8")
    monkeypatch.setenv("RECKON_MANIFEST", str(manifest))

    blocked, reason = stop_hook.decide({"cwd": str(tmp_path)})

    assert (blocked, reason) == (False, None)
    assert not (tmp_path / stop_hook.COUNTER_NAME).exists()


def test_the_stop_hook_refuses_a_waiting_manifest_missing_its_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarded thing happens: a declaration the fleet would refuse is refused."""
    manifest = tmp_path / "manifest.md"
    without_probe = "\n".join(
        line
        for line in WAITING_MANIFEST.splitlines()
        if not line.startswith("wait_probe:")
    )
    manifest.write_text(without_probe + "\n", encoding="utf-8")
    monkeypatch.setenv("RECKON_MANIFEST", str(manifest))

    blocked, reason = stop_hook.decide({"cwd": str(tmp_path)})

    assert blocked is True
    assert reason is not None
    assert "wait" in reason
