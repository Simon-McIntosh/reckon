"""A resumed run still working is a working run, not delivered work.

``crew resume`` reuses a run's directory and the manifest a previous turn left,
so seconds into the resumed turn the manifest can still read ``complete`` while
the new worker is streaming. Read from that status alone the run classifies as
delivered work awaiting a gate: the follower counts it under ``u`` rather than
``w`` and the obligation hook offers a ``crew complete`` that would delete the
live pointer. The worker's own record separates the two — a worker launched
after the manifest was last written cannot have written it — so the stale
verdict is not read as delivery while that worker is alive.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "resumed-fixture"
SESSION = "coordinator-fixture"

# A pid nothing runs under, for the finished case.
DEAD_PID = 99999999


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary, fully environment-resolved crew home and project mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("commit", "-q", "--allow-empty", "-m", "test: seed resumed fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return config_home


def _write_resumed_run(
    *,
    run_id: str,
    status: str,
    worker_launched_after_manifest: bool,
    worker_live: bool,
) -> dict:
    """Write one resumed run's manifest, worker record and live pointer.

    The manifest carries a terminal ``status`` from the superseded attempt and
    is left older than the worker's recorded launch, so only the launch time
    can tell the two apart. The pointer's own process reading is left unproven
    (no launching host, no recorded liveness) exactly as a resumed pointer
    carries it, so the classification cannot lean on the pointer's pid.
    """
    run_directory = runs.run_dir(run_id)
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest = run_directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: {status}\n", encoding="utf-8")
    now = time.time()
    if worker_launched_after_manifest:
        written, launched = now - 600.0, now - 30.0
    else:
        written, launched = now - 30.0, now - 600.0
    os.utime(manifest, (written, written))
    pid = os.getpid() if worker_live else DEAD_PID
    (run_directory / "worker.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": 2,
                "pid": pid,
                "pid_start_time": recovery._process_start_time(pid),
                "launched_at": datetime.fromtimestamp(launched, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
        "phase": "working",
        "attempt": 2,
        "attempt_kind": "resume",
        "pid": DEAD_PID,
        "launcher_host": None,
        "process_alive": None,
        "manifest_path": str(manifest),
        "log_path": str(run_directory / "resume-1.jsonl"),
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _stub_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )


def test_a_resumed_run_with_a_live_worker_counts_as_working(home: Path) -> None:
    """A stale ``complete`` manifest behind a live worker is not delivery."""
    run_id = "run-resumed-complete"
    pointer = _write_resumed_run(
        run_id=run_id,
        status="complete",
        worker_launched_after_manifest=True,
        worker_live=True,
    )

    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "running"

    snapshot = _snapshot(pointer)
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES
    counts = recovery._fleet_counts({run_id: snapshot})
    assert counts["working"] == 1
    assert counts["unpromoted"] == 0


def test_a_resumed_run_with_a_stale_blocked_manifest_counts_as_working(
    home: Path,
) -> None:
    """A stale ``blocked`` manifest behind a live worker reports no block."""
    run_id = "run-resumed-blocked"
    pointer = _write_resumed_run(
        run_id=run_id,
        status="blocked",
        worker_launched_after_manifest=True,
        worker_live=True,
    )

    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "running"

    snapshot = _snapshot(pointer)
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES


def test_a_finished_run_with_an_exited_worker_still_reads_as_delivered(
    home: Path,
) -> None:
    """Launch time alone does not defer a verdict whose worker has stopped."""
    run_id = "run-finished"
    pointer = _write_resumed_run(
        run_id=run_id,
        status="complete",
        worker_launched_after_manifest=True,
        worker_live=False,
    )

    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] != "running"

    snapshot = _snapshot(pointer)
    assert snapshot["state"] not in recovery.FLEET_WORKING_STATES
    counts = recovery._fleet_counts({run_id: snapshot})
    assert counts["working"] == 0


def test_a_resumed_run_owes_no_promotion_obligation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The obligation hook does not offer a completion for a working run."""
    run_id = "run-resumed-obligation"
    _write_resumed_run(
        run_id=run_id,
        status="complete",
        worker_launched_after_manifest=True,
        worker_live=True,
    )
    _stub_drain(monkeypatch)

    result = obligations_module.obligations(PROJECT, SESSION)

    owed = [item for item in result["obligations"] if item["run_id"] == run_id]
    assert owed == []
