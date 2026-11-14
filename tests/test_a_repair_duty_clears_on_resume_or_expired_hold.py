"""A repair duty yields while a coordinator's own resume is answering it.

A review raises a blocking finding, the reflex holds the repair for advice, and
the coordinator answers the hold by resuming the reviewed run by hand. That
resume continues the run the finding is about, so while the resumed attempt
runs the reflex cannot open a second repair for it: the dispatch is refused for
the live worker. The refused dispatch then sits on the pointer beside the stale
hold, and the duty is reported as ``repair-owed`` for as long as the attempt
runs — a duty nobody can act on, which holds the stop hook on every turn end.

So a run with a live resume attempt is not reported as owing a repair for the
round that attempt is answering. The suppression is scoped to the live
attempt: once it ends the duty is read again from what the run delivered, so a
repair that landed a new head leaves the run owing its review and nothing that
reads ``repair-owed``.

The declared mutation, applied verbatim in a scratch copy: keeping the refused
dispatch as the duty's state fails the live-attempt case.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery_classification, runs
from reckon.crew import review as review_module
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")

PROJECT = "repair-resume-fixture"
SESSION = "repair-resume-session"
HOST = socket.gethostname()
RUN_ID = "r-repair-resumed-by-coordinator"
# A duty ages a run against the clock it is handed, and the fixture stamps its
# manifest from that same instant. Pinning the clock past import makes the age
# the fixture states a fact about the fixture, not a reading of how long the
# suite took to reach this test.
IMPORTED_AT = datetime.now(tz=UTC)
NOW = IMPORTED_AT + timedelta(minutes=2)
RESUMED_AT = NOW - timedelta(minutes=10)
FINDING = {
    "file": "src/guard.py",
    "line": "8",
    "text": "the guard allows the blocked path",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setattr(obligations, "_utc_now", lambda: NOW)
    # The duty's rows are classified without a caller-supplied instant, so the
    # classifier reads its own clock; pin that boundary too, or the age is read
    # from the wall clock this fixture never advanced.
    monkeypatch.setattr(recovery_classification, "_utc_seconds", NOW.timestamp)
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed fixture"),
    ):
        git(root, *args)
    (config / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root, git(root, "rev-parse", "HEAD")


def _start_time(pid: int) -> str | None:
    """The kernel start tick a worker record carries, read by its own owner."""
    from reckon.crew.recovery import _process_start_time

    return _process_start_time(pid)


def _live_worker():
    """A real, running child process, reaped by pid on the way out."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _round_id(run_id: str, head: str) -> str:
    return f"{run_id}@{head}"


def _manifest(root: Path, run_id: str, head: str, *, status: str) -> Path:
    manifest = root.parent / f"{run_id}.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    age = (NOW - IMPORTED_AT).total_seconds()
    os.utime(manifest, (NOW.timestamp() - age, NOW.timestamp() - age))
    return manifest


def _review(run_id: str, head: str, *, findings: list[dict] | None = None) -> None:
    record = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
            "findings": findings or [],
        }
    )
    review_module.store_review(record)


def _delivered_run(home: tuple[Path, str], run_id: str) -> dict:
    """A delivered run whose review raised one blocking finding.

    The reflex held the repair for coordinator advice, the coordinator answered
    it by resuming the run, and the reflex then recorded its refusal for the
    live worker. That is the state the incident left on the pointer: a stale
    hold and a refused repair the reflex will not retry while the attempt runs.
    """
    root, head = home
    manifest = _manifest(root, run_id, head, status="complete")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(root),
        "worktree": str(root),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture",
            "section": "fixture",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    _review(run_id, head, findings=[FINDING])
    return pointer


def _resume_in_flight(pointer: dict, pid: int) -> None:
    """Record the coordinator's resume of the run, its worker still running.

    The attempt's own receipt names the running worker, while the pointer still
    carries the delivered reading it held when the resume began. The pair is
    what makes the duty survive: everything that classifies the run for a duty
    reads the pointer, and only the attempt's receipt knows the worker is
    alive.
    """
    run_id = str(pointer["run_id"])
    head = str(pointer["base_sha"])
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    runs._write_json(
        directory / "attempt-2-worker.json",
        {
            "run_id": run_id,
            "attempt": 2,
            "pid": pid,
            "pid_start_time": _start_time(pid),
            "launched_at": RESUMED_AT.isoformat(),
        },
    )
    runs._write_json(
        directory / "attempt.json",
        {
            "run_id": run_id,
            "attempt": 2,
            "attempt_kind": "resume",
            "attempt_started_at": RESUMED_AT.isoformat(),
        },
    )
    pointer.update(
        {
            "launcher_host": HOST,
            "attempt": 2,
            "attempt_kind": "resume",
            "attempt_started_at": RESUMED_AT.isoformat(),
            # The resume left the delivered head behind, and the resumed
            # attempt has since delivered its own manifest at that head while
            # its worker keeps running, so the superseded-head window is closed
            # and the stored review is the one in hand.
            "resume_superseded_head": {"head": head, "at": RESUMED_AT.isoformat()},
            "repair_dispatch": {
                "status": "refused",
                "reason": "the run's worker is live; the repair cannot dispatch",
                "round_id": _round_id(run_id, head),
                "at": NOW.isoformat(),
                "attempt": 2,
            },
        }
    )
    runs._write_json(runs.pointer_path(run_id), pointer)


def _kinds(run_id: str) -> list[str]:
    return [
        str(item["kind"])
        for item in obligations.obligations(PROJECT, SESSION)["obligations"]
        if item.get("run_id") == run_id
    ]


def test_a_live_coordinator_resume_is_not_reported_as_owing_repair(
    home: tuple[Path, str],
) -> None:
    child = _live_worker()
    try:
        pointer = _delivered_run(home, RUN_ID)
        _resume_in_flight(pointer, child.pid)

        kinds = _kinds(RUN_ID)
    finally:
        child.kill()
        child.wait()

    assert "repair-owed" not in kinds, kinds


def test_the_duty_does_not_survive_the_landed_repair(
    home: tuple[Path, str],
) -> None:
    root, head = home
    child = _live_worker()
    pointer = _delivered_run(home, RUN_ID)
    _resume_in_flight(pointer, child.pid)
    child.kill()
    child.wait()

    (root / "seed.txt").write_text("seed\nrepair\n", encoding="utf-8")
    git(root, "add", "seed.txt")
    git(root, "commit", "-q", "-m", "fix: answer the blocking finding")
    repaired = git(root, "rev-parse", "HEAD")
    assert repaired != head
    _manifest(root, RUN_ID, repaired, status="complete")
    pointer = runs.read_pointer(RUN_ID)
    pointer["base_sha"] = repaired
    pointer["phase"] = "done"
    runs._write_json(runs.pointer_path(RUN_ID), pointer)

    kinds = _kinds(RUN_ID)

    assert "repair-owed" not in kinds, kinds
