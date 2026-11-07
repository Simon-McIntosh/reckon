"""A run whose remedy is a repair reaches its session as a duty.

Two recovery readings carry the ``repair`` verb: ``unreadable`` — the run's
manifest declares an external wait the reader refused, so the declaration is
what is wrong — and ``exited-unfinished`` — the worker exited with work
committed and a manifest still carrying a non-terminal verdict. Both are the
session's to fix, and neither reached the obligations view at all, because the
recovery duty map listed only ``needs-help`` and ``launch-failed``. A refused
wait therefore left its session idle with the coordinator told nothing while
the run sat there declaring a condition no sweep could read.

These cases pin the route: a run whose wait the probe reader refuses appears as
a duty of its session and carries the classifier's own refusal text as its
``reason``, an exited-unfinished run appears the same way, and a run with a
valid probe — one the sweep will lift on its own — owes nothing.

Every fixture synthesises its run under the temporary configuration home the
suite isolates, and the workstation's own pointer directory is asserted
untouched, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import reckon.crew.obligations as obligations_module
from reckon.crew import recovery, recovery_liveness
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "repair-duty-fixture"
SESSION = "s-repair-duty"
REFUSED_RUN_ID = "r-refused-wait"
VALID_RUN_ID = "r-valid-wait"
EXITED_RUN_ID = "r-exited-unfinished"
RUN_IDS = (REFUSED_RUN_ID, VALID_RUN_ID, EXITED_RUN_ID)

#: The probe §1 reported: an interpreter string the reader refused as naming no
#: reference. Kept verbatim so the case covers the reading it complains about.
REFUSED_PROBE = ["python3", "-c", "print('pending')"]


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _git(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *argv], capture_output=True, check=False, text=True
    )


def _worktree_with_commit(directory: Path) -> tuple[Path, str]:
    """A real worktree carrying one commit past the revision it records as base."""
    repo = directory / "tree"
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


def _pointer(
    run_id: str,
    directory: Path,
    *,
    manifest: Path,
    phase: str = "waiting",
    **extra: object,
) -> dict:
    """One live pointer for ``SESSION`` over the run directory's manifest."""
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
        "created_at": "2026-11-07T08:00:00Z",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stream.err"),
        "manifest_path": str(manifest),
        "phase": phase,
        "process_alive": False,
        "node": {
            "id": run_id,
            "plan": "a-broken-wait-reaches-its-coordinator",
            "section": "s3",
            "time_budget": "30m",
        },
    }
    record.update(extra)
    _write_json(pointer_path(run_id), record)
    return record


def _waiting_run(run_id: str, *, probe: list[str]) -> dict:
    """A run parked on a declared wait whose worker no process answers for."""
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                "wait_condition: scheduler job 1306915 has left the queue",
                f"wait_probe: {json.dumps(probe)}",
                'wait_terminal: ["COMPLETED", "FAILED"]',
                "resume_brief: collect the scheduler result and finish the report",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return _pointer(run_id, directory, manifest=manifest)


def _exited_unfinished_run(run_id: str) -> dict:
    """A run whose worker exited with a commit and a non-terminal manifest."""
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    repo, base = _worktree_with_commit(directory)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: the-node\nstatus: in-progress\ncheckpoint: work committed\n",
        encoding="utf-8",
    )
    worker_pid = _reaped_pid()
    _write_json(
        directory / recovery_liveness.ATTEMPT_RECORD_NAME,
        {"run_id": run_id, "attempt": 1, "attempt_started_at": "2026-11-07T08:00:00Z"},
    )
    _write_json(
        directory / recovery_liveness.WORKER_RECORD_NAME,
        {"pid": worker_pid, "attempt": 1},
    )
    _write_json(
        directory / recovery.EXIT_RECORD_NAME,
        {
            "run_id": run_id,
            "attempt": 1,
            "signal": None,
            "signal_name": None,
            "exit_code": 0,
            "ended_during": "working",
            "last_record_type": "result",
            "recorded_by": "supervisor",
            "exited_at": "2026-11-07T08:20:00Z",
            "stream_records_seen": 40,
            "worker_pid": worker_pid,
        },
    )
    return _pointer(
        run_id,
        directory,
        manifest=manifest,
        phase="working",
        base_sha=base,
        attempt=1,
        worktree=str(repo),
        pid=worker_pid,
        process_alive=True,
    )


def _rows(run_id: str) -> list[dict]:
    payload = obligations_module.obligations(PROJECT, SESSION)
    return [
        row for row in payload["obligations"] if str(row.get("run_id") or "") == run_id
    ]


def test_a_refused_wait_is_a_duty_carrying_the_refusal() -> None:
    """The reading the session owes: the run is listed, and named as refused.

    A refused probe is the case §1 reported — the run classified ``unreadable``,
    the classifier's next action was "repair the waiting declaration", and the
    session's obligations listed nothing at all. The row's ``reason`` is the
    classifier's own account, so the coordinator reads what to repair rather
    than only that something is halted.
    """
    run = _waiting_run(REFUSED_RUN_ID, probe=REFUSED_PROBE)

    classified = recovery.classify_pointer(run)
    assert classified["classification"] == "unreadable", classified["detail"]
    assert classified["recovery"] == "repair", classified["recovery"]

    rows = _rows(REFUSED_RUN_ID)

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "unreadable", row
    assert row["reason"] == classified["detail"], row
    # The refusal text itself, not a rewording of it: the reader refused this
    # probe and the obligation repeats the reader's sentence.
    assert "python3" in row["reason"], row
    assert "incomplete" in row["reason"], row
    assert "repair the waiting declaration" in row["next_command"], row


def test_a_wait_the_sweep_can_lift_owes_no_duty() -> None:
    """A valid probe is a wait at work: the sweep lifts it, the session owes none."""
    directory = runs_dir() / VALID_RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / "state.txt"
    state_file.write_text("RUNNING\n", encoding="utf-8")

    run = _waiting_run(VALID_RUN_ID, probe=["cat", str(state_file)])

    classified = recovery.classify_pointer(run)
    assert classified["classification"] == "waiting", classified["detail"]

    assert _rows(VALID_RUN_ID) == []


def test_an_exited_unfinished_run_is_a_duty() -> None:
    """The worker's work is committed and nothing says so: that is a repair too."""
    run = _exited_unfinished_run(EXITED_RUN_ID)

    classified = recovery.classify_pointer(run)
    assert classified["classification"] == "exited-unfinished", classified["detail"]
    assert classified["recovery"] == "repair", classified["recovery"]

    rows = _rows(EXITED_RUN_ID)

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "exited-unfinished", row
    assert row["reason"] == classified["detail"], row
    assert "in-progress" in row["reason"], row
    assert "--status complete" in row["next_command"], row
