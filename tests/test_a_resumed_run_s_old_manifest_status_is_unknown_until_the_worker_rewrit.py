"""A resumed run's blocked duty follows the manifest its new attempt writes.

``crew resume`` reuses a run's directory, so the manifest an earlier turn left
still sits beside the run with its status word on it, and the reset before a
resume clears only the supervisor's done words. While the new attempt lives and
moves, that word is that attempt's predecessor's report: the run reads as
working and the stop hook raises no duty to resolve a blocker the resume already
resolved. Once the new attempt writes the manifest itself, the word is that
attempt's own verdict and the blocked duty stands again.

Measured 2026-10-21: run ``r-20261021T235533197454-fdu-stray-companion`` was
resumed while its manifest still read ``blocked`` from the round before, and the
stop hook raised ``[blocked]`` four times over a morning although the checkpoint
said the blocker was gone.

The declared negative control reads the old manifest rather than dating the
attempt, and the stale blocked duty appears again.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from reckon.crew import recovery, recovery_liveness, runs
from tests.helpers.git import git as _git

classification_module = importlib.import_module("reckon.crew.recovery_classification")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "resumed-duty-fixture"
SESSION = "coordinator-fixture"

# A pid nothing runs under, on a launcher host that is not this one: the run is
# read here, so a pid this host could look up would answer for the attempt the
# resume replaced. The reported run was read the same way.
DEAD_PID = 99999999
FOREIGN_HOST = "some-other-host"

# The predecessor's manifest and stream are old, past the stall window. The
# current attempt is young: its own start caps the stream's silence, so the
# attempt is live and moving even though the stream it inherits is silent.
STALE_SECONDS = 1200.0
ATTEMPT_STARTED_SECONDS = 30.0
# A manifest the new attempt wrote itself, dated after that start.
RECENT_SECONDS = 5.0

MUTATION_ENV = "RECKON_RESUMED_DUTY_NEGATIVE_CONTROL"


def _reads_the_old_manifest(*_args: Any, **_kwargs: Any) -> bool:
    """The declared mutation: the predecessor's manifest status is kept."""
    return False


@pytest.fixture(autouse=True)
def _crew_home(tmp_path, monkeypatch):
    """A temporary crew home, a mounted project, and no fleet-wide drain read."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("commit", "-q", "--allow-empty", "-m", "seed"),
    ):
        _git(root, *args)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(runs, "drain", lambda *a, **k: {"unreconciled_runs": 0})


@pytest.fixture(autouse=True)
def _applied_mutation(monkeypatch):
    """Declared mutation: the predecessor manifest word is read."""
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            classification_module,
            "_manifest_is_the_predecessor_attempts_report",
            _reads_the_old_manifest,
        )


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _write_run(run_id, manifest_age_seconds, attempt_start_age_seconds):
    now = time.time()
    run_directory = runs.run_dir(run_id)
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest = run_directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: blocked\n", encoding="utf-8")
    written = now - manifest_age_seconds
    os.utime(manifest, (written, written))
    stream = run_directory / "attempt-1.jsonl"
    stream.write_text('{"type": "turn.started"}\n', encoding="utf-8")
    quiet = now - STALE_SECONDS
    os.utime(stream, (quiet, quiet))
    marker = run_directory / recovery_liveness.ATTEMPT_RECORD_NAME
    marker.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": 2,
                "attempt_started_at": _at(now - attempt_start_age_seconds),
            }
        ),
        encoding="utf-8",
    )
    worker = run_directory / recovery.WORKER_RECORD_NAME
    worker.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": 2,
                "pid": DEAD_PID,
                "pid_start_time": recovery._process_start_time(DEAD_PID),
                "launched_at": _at(now - attempt_start_age_seconds),
            }
        ),
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "working",
        "created_at": _at(now - manifest_age_seconds - 60),
        "attempt": 2,
        "attempt_kind": "resume",
        "pid": DEAD_PID,
        "launcher_host": FOREIGN_HOST,
        "process_alive": None,
        "manifest_path": str(manifest),
        "log_path": str(stream),
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _classify(pointer):
    return recovery.classify_pointer(
        pointer,
        now_seconds=time.time(),
        condition_test=lambda _record, _wait: "RUNNING",
    )


def _blocked_duties(run_id):
    result = obligations_module.obligations(PROJECT, SESSION)
    return [
        item
        for item in result["obligations"]
        if item["run_id"] == run_id and item["kind"] == "blocked"
    ]


def test_a_resumed_attempt_owes_no_blocked_duty() -> None:
    """The predecessor's blocked word raises no duty for the live attempt."""
    run_id = "run-resumed-blocked"
    pointer = _write_run(run_id, STALE_SECONDS, ATTEMPT_STARTED_SECONDS)

    row = _classify(pointer)

    duties = _blocked_duties(run_id)
    assert duties == [], duties
    assert row["classification"] == "running", row["detail"]
    assert row["manifest_status"] is None, row


def test_the_new_attempt_blocked_word_restores_the_duty() -> None:
    """A manifest the new attempt wrote is its own verdict, so the duty stands."""
    run_id = "run-rewritten-blocked"
    pointer = _write_run(run_id, RECENT_SECONDS, ATTEMPT_STARTED_SECONDS)

    row = _classify(pointer)

    assert row["classification"] == "blocked", row["detail"]
    assert row["manifest_status"] == "blocked", row
    assert _blocked_duties(run_id), row
