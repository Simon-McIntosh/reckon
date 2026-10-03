"""A valid wait keeps its state, with an unrelated finding attached beside it.

A manifest declaring a well-formed external wait is a parked run, not a broken
one. The fleet's classifier reads such a run as waiting, and the worker stop
hook accepts the declaration — but the manifest audit judged the status word
itself, refusing ``waiting`` with "status 'waiting' is not complete, blocked or
failed" when its other checks (here, a gate log whose first line names no
revision) failed. That one finding replaced the wait state, so the reading a
coordinator acted on said the run was in no state at all, and the automatic
resume had nothing to resume. The audit now reads the declaration through the
same wait reader the classifier and the stop hook share: a declaration it
honours keeps its state and the audit's other findings are attachments beside
it, while a malformed wait block is still refused, by the reader's own error.

Each case builds its run directory under a temporary configuration home and its
own git worktree under the case's temporary directory, so nothing here reads or
writes the machine's live fleet.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import recovery, reports, runs

HOST = socket.gethostname()

# The wait block the stop hook already accepts, so the only variable between
# the two cases is the block's shape, not the runner's idea of a wait.
WAIT_BLOCK = (
    "wait_condition: scheduler jobs 1279884 and 1279885 leave the queue\n"
    'wait_probe: ["squeue", "-h", "-j", "1279884"]\n'
    'wait_terminal: ["COMPLETED", "FAILED"]\n'
    "resume_brief: collect the scheduler result and carry the node to its gate\n"
)

# A wait block the fleet's reader refuses: a printer names nothing outside the
# for the wait to catch, so the declaration cannot fail and reads the same on
# every sweep.
MALFORMED_WAIT_BLOCK = WAIT_BLOCK.replace(
    'wait_probe: ["squeue", "-h", "-j", "1279884"]',
    'wait_probe: ["echo", "pending"]',
)

GATE_LOG_FIRST_LINE = "a gate run whose header names no revision\n"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every case at a temporary configuration home, and prove it."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)


def _git(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *argv], capture_output=True, check=False, text=True
    )


def _worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "tree"
    repo.mkdir(parents=True)
    assert _git("init", "-q", "-b", "main", cwd=repo).returncode == 0
    return repo


def _reaped_pid() -> int:
    """A pid this host has seen and reaped, so its absence is a measurement."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _parked_run(tmp_path: Path, run_id: str, wait_block: str) -> tuple[dict, Path, str]:
    """A run parked on a declared wait whose gate log fails the header check."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    gate_log = directory / "gate.log"
    gate_log.write_text(GATE_LOG_FIRST_LINE + "EXIT=0\n", encoding="utf-8")
    manifest_text = "node: the-node\nstatus: waiting\n" + wait_block
    manifest_text += (
        "baseline_suite: "
        + json.dumps(
            {
                "revision": "0" * 40,
                "command": "pytest -q",
                "exit_status": 0,
                "completed": True,
                "failure_count": 0,
                "failure_ids": [],
                "log_path": str(gate_log),
            }
        )
        + "\n"
    )
    manifest = directory / "manifest.md"
    manifest.write_text(manifest_text, encoding="utf-8")
    (directory / "stream.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "session_id": "fixture-session",
                "timestamp": "2026-10-02T07:00:00.000Z",
                "message": {
                    "content": [{"type": "text", "text": "parked on the gate"}]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    repo = _worktree(tmp_path)
    pointer = {
        "run_id": run_id,
        "project": "state-fixture",
        "session": "s-fixture",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "60m"},
        "phase": "working",
        "created_at": "2026-10-02T06:00:00Z",
        "launcher_host": HOST,
        "pid": _reaped_pid(),
        "process_alive": False,
        "manifest_path": str(manifest),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "worktree": str(repo),
        "base_sha": "0" * 40,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer, manifest, manifest_text


def test_a_valid_wait_keeps_its_state_beside_an_unrelated_finding(
    tmp_path: Path,
) -> None:
    """The measured defect: the gate-log finding refused the waiting state itself."""
    pointer, manifest, manifest_text = _parked_run(tmp_path, "r-valid-wait", WAIT_BLOCK)

    row = recovery.classify_pointer(pointer)
    assert row["classification"] == "waiting", row["detail"]

    audit = reports.audit_manifest(manifest_text, manifest_path=manifest)
    findings = list(audit["findings"])
    assert any("first line" in finding for finding in findings), findings
    assert not any(
        "is not complete, blocked or failed" in finding for finding in findings
    ), findings


def test_a_malformed_wait_block_still_reads_unreadable(tmp_path: Path) -> None:
    """The guard on the relaxation: a refused declaration is never an accepted state."""
    pointer, manifest, manifest_text = _parked_run(
        tmp_path, "r-malformed-wait", MALFORMED_WAIT_BLOCK
    )

    row = recovery.classify_pointer(pointer)
    assert row["classification"] == "unreadable", row["detail"]

    audit = reports.audit_manifest(manifest_text, manifest_path=manifest)
    findings = list(audit["findings"])
    assert audit["ok"] is False
    assert any(
        "wait declaration" in finding or "is not complete, blocked or failed" in finding
        for finding in findings
    ), findings
