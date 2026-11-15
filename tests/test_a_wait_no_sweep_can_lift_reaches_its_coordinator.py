"""A refused wait declaration reaches its coordinator with a remedy.

A run can park on a wait declaration the reader refuses: the manifest carries
``wait_condition``, ``wait_terminal``, ``wait_expected``, ``resume_brief`` and
``wait_started_at`` but names no probe to answer the condition with. The
classifier reads that as ``unreadable`` and the obligations view already raises
it as a duty for the reader. What that row lacked is the remedy: the key the
declaration is missing, and the command that moves the run —
``reckon crew resume --run <id> --advice``. Without either, a coordinator is
told a run is halted and left to work out both the fault and the fix.

These cases pin the two facts the row now carries: it names the absent key, and
it offers the resume command. A run whose declaration the reader accepts owes
neither, so the second case proves the remedy is attached to the refusal rather
than to every waiting run.

Every fixture synthesises its run under the temporary configuration home the
suite isolates, and the workstation's own pointer directory is asserted
untouched, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import reckon.crew.obligations as obligations_module
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "wait-remedy-fixture"
SESSION = "s-wait-remedy"
INCOMPLETE_RUN_ID = "r-wait-incomplete"
VALID_RUN_ID = "r-wait-valid"
RUN_IDS = (INCOMPLETE_RUN_ID, VALID_RUN_ID)

#: A scheduler query the reader accepts: it names a job id, so it can report a
#: pending state and the job gone. A stub answers it with a running state, so a
#: valid wait stays healthy and not yet met.
SACCT_PROBE = ["sacct", "-n", "-X", "-P", "-j", "1307334", "-o", "State"]


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _reaped_pid() -> int:
    """A pid this host has seen and reaped, so its absence is a measurement."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _waiting_manifest(directory: Path, run_id: str, *, probe: bool) -> Path:
    """A waiting manifest, with or without the probe key the reader needs."""
    manifest = directory / "manifest.md"
    declared = datetime.now(tz=UTC) - timedelta(hours=13)
    lines = [
        f"node: {run_id}",
        "status: waiting",
        "wait_condition: scheduler job 1307334 has left the queue",
        f"wait_terminal: {json.dumps(['COMPLETED', 'FAILED'])}",
        "wait_expected: 1h",
        "resume_brief: collect the scheduler result and finish the report",
        f"wait_started_at: {declared.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    if probe:
        lines.insert(3, f"wait_probe: {json.dumps(SACCT_PROBE)}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _run(run_id: str, *, probe: bool) -> dict:
    """One live pointer whose worker no process on this host answers for."""
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = _waiting_manifest(directory, run_id, probe=probe)
    tree = directory / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(runs_dir()),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "codex",
        "role": "implement",
        "created_at": "2026-11-14T20:00:00Z",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stream.err"),
        "manifest_path": str(manifest),
        "phase": "working",
        "process_alive": False,
        "pid": _reaped_pid(),
        "node": {
            "id": run_id,
            "plan": "a-wait-no-sweep-can-lift-reaches-its-coordinator",
            "section": "s60",
            "time_budget": "30m",
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _rows(run_id: str) -> list[dict]:
    payload = obligations_module.obligations(PROJECT, SESSION)
    return [
        row for row in payload["obligations"] if str(row.get("run_id") or "") == run_id
    ]


def test_an_incomplete_wait_names_the_missing_key_and_the_resume_command() -> None:
    """The duty names what the declaration lacks and the command that moves it."""
    _run(INCOMPLETE_RUN_ID, probe=False)

    rows = _rows(INCOMPLETE_RUN_ID)

    assert len(rows) == 1, rows
    row = rows[0]
    assert "wait_probe" in row["reason"], row
    assert "reckon crew resume --run" in row["next_command"], row
    assert INCOMPLETE_RUN_ID in row["next_command"], row
    assert "--advice" in row["next_command"], row


def test_a_wait_the_reader_accepts_owes_no_such_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid probe is a wait at work: the sweep lifts it, the session owes none."""
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if Path(str(argv[0])).name == "sacct":
            return subprocess.CompletedProcess(argv, 0, stdout="RUNNING\n", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run(VALID_RUN_ID, probe=True)

    rows = _rows(VALID_RUN_ID)

    assert rows == [], rows
