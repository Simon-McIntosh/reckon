"""A run mid-repair raises no sweep failure, and one failure is raised once.

A delivered run can be resumed for a repair round. While that resumed attempt is
still working the review pass composes nothing, because the head it would review
is about to move — and the census then saw a delivered run with no request after
its deadline and raised a ``sweep-failed`` duty for it. Withholding the review
was correct; the duty asked a coordinator to inspect a run with nothing wrong,
and every stop of the session repeated it.

The same shape recurring: the stored failure record is keyed by the session whose
sweep recorded it, so three sweep authors storing one run's failure raised the
same row three times. One failure is one duty.

Three facts are measured here, each through the code a sweep and a station take:

- a delivered run whose resumed attempt is still working, and whose review pass
  therefore composes nothing, yields no ``sweep-failed`` row;
- a run whose resumed attempt ended without delivering still yields one;
- one failure stored by three sweep authors for one run yields one row.

The negative control is this module against the base revision: the first case
raises a ``sweep-failed`` row, and the third raises three rows for one run.
"""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    recovery_review_dispatch,
    stage_receipts,
    station_obligations,
    station_store,
)
from reckon.crew import (
    runs as runs_module,
)
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    _release_armed_producer,
    _scoring_pointer,
    isolated_project,  # noqa: F401 - fixture
)

PROJECT = "sample"
SESSION = "session-orchestrating"
RUN_ID = "r-mid-repair"
ERROR = "ValueError: unreadable ledger row"


@pytest.fixture()
def controlled_clock(monkeypatch: pytest.MonkeyPatch):
    """Hold the receipts' clock still, so a delivery has the instant a case sets."""
    reading = {"now": datetime(2026, 1, 1, tzinfo=UTC) - timedelta(seconds=601)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return reading["now"]

    monkeypatch.setattr(stage_receipts, "datetime", Clock)
    return reading


def _delivered(run_id: str) -> str:
    """Record the delivery the way the watcher records it; return its instant."""
    stage_receipts.note_delivery({"run_id": run_id, "to_state": "completed_unpromoted"})
    instants = recovery_review_dispatch.review_stage_instants(run_id)
    assert instants["delivered_at"], instants
    return str(instants["delivered_at"])


def _live_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _dead_pid() -> int:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _resumed_pointer(
    config_home: Path,
    repo: Path,
    run_id: str,
    *,
    pid: int | None,
) -> dict:
    """A delivered run whose live pointer is a resumed attempt.

    ``attempt_kind`` is ``resume`` and ``attempt`` names the second attempt, the
    attempt a repair round starts. ``pid`` is the worker's process: a live one
    still works the attempt, a dead one does not.
    """
    record = _scoring_pointer(config_home, repo, run_id)
    record["attempt_kind"] = "resume"
    record["attempt"] = 2
    record["pid"] = pid
    if pid is not None:
        record["pid_start_time"] = runs_module._process_start_time(pid)
    runs_module._write_json(runs_module.pointer_path(run_id), record)
    return record


def _retire_attempt(run_id: str, attempt: int) -> None:
    """Write the immutable exit receipt an ended attempt leaves behind."""
    directory = runs_module.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    runs_module._write_json(
        directory / f"attempt-{attempt}-exit.json",
        {
            "run_id": run_id,
            "attempt": attempt,
            "exit_code": 0,
            "detail": "the attempt ended without delivering",
        },
    )
    return directory


def _sweep() -> dict:
    """Run one review sweep the way an armed follower's pass runs it."""
    from reckon.crew import resumption

    with runs_module.follower_claim(PROJECT, SESSION, delivery="stream"):
        return resumption.sweep(
            PROJECT, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )


def _failed_rows(report: dict, run_id: str) -> list[dict]:
    """The rows this sweep raised as failures, for one run."""
    return [
        row
        for row in report["reviews"].get("failed") or ()
        if str(row.get("run_id") or "") == run_id
    ]


def _station_rows(tmp_path: Path, report: dict) -> list[dict]:
    """The sweep-failed rows a station derives from this sweep's stored record."""
    store = station_store.StationStore(tmp_path / "station")
    try:
        station_obligations.record_sweep_report(store, PROJECT, SESSION, report)
        aggregated = [
            failure
            for failures in station_obligations.prune_sweep_failures(
                store, PROJECT
            ).values()
            for failure in failures
        ]
        return station_obligations._with_sweep_failures(
            {"project": PROJECT, "obligations": [], "summary": {}},
            aggregated,
        )["obligations"]
    finally:
        store.close()


def test_a_working_resumed_attempt_raises_no_sweep_failure(
    isolated_project: tuple[Path, Path],
    tmp_path: Path,
    controlled_clock: dict,
) -> None:
    """The review is withheld because the run works; no duty follows from it."""
    config_home, repo = isolated_project
    worker = _live_child()
    try:
        _resumed_pointer(config_home, repo, RUN_ID, pid=worker.pid)
        _delivered(RUN_ID)

        report = _sweep()

        # The pass composed nothing for this run: the attempt is still working.
        assert not any(
            str(row.get("run_id") or "") == RUN_ID and row.get("dispatched")
            for row in report["reviews"].get("reports") or ()
        ), report["reviews"]
        assert report["review_requests"].get("in_flight"), report.get("review_requests")
        assert _failed_rows(report, RUN_ID) == [], _failed_rows(report, RUN_ID)
        rows = [
            row
            for row in _station_rows(tmp_path, report)
            if str(row.get("kind") or "") == "sweep-failed"
        ]
        assert rows == [], rows
    finally:
        _stop(worker)
        _release_armed_producer()


def test_a_resumed_attempt_that_ended_without_delivering_still_raises(
    isolated_project: tuple[Path, Path],
    tmp_path: Path,
    controlled_clock: dict,
) -> None:
    """Nothing is working on it, so the absent review is a failure again."""
    config_home, repo = isolated_project
    _resumed = _resumed_pointer(config_home, repo, RUN_ID, pid=_dead_pid())
    assert _resumed["attempt"] == 2
    _retire_attempt(RUN_ID, 2)
    _delivered(RUN_ID)

    try:
        report = _sweep()
    finally:
        _release_armed_producer()

    failures = _failed_rows(report, RUN_ID)
    assert len(failures) == 1, failures
    assert "resumed attempt has not delivered" in failures[0]["error"], failures[0]
    rows = [
        row
        for row in _station_rows(tmp_path, report)
        if str(row.get("kind") or "") == "sweep-failed"
    ]
    assert len(rows) == 1, rows


def _failure_record(store, session: str, run_id: str) -> None:
    """Store one run's failure the way that session's completed sweep reports it."""
    station_obligations.record_sweep_report(
        store,
        PROJECT,
        session,
        {"reviews": {"failed": [{"run_id": run_id, "error": ERROR}]}},
    )


@pytest.fixture()
def mounted_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root the mount registry resolves, so a live run is a duty."""
    config = tmp_path / "config"
    config.mkdir()
    mounts = config / "mounts.json"
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for args in (
        ("init", "-q"),
        (
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "landed",
        ),
    ):
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, (args, done.stderr)
    mounts.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    return root


def test_three_authors_of_one_failure_raise_one_row(
    mounted_project: Path, tmp_path: Path
) -> None:
    """The stored record is per author; the duty is per run."""
    path = runs_module.pointer_path(RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": RUN_ID, "project": PROJECT, "session": SESSION}),
        encoding="utf-8",
    )
    store = station_store.StationStore(tmp_path / "station")
    store.connect()
    try:
        for author in ("sweep-author-one", "sweep-author-two", "sweep-author-three"):
            _failure_record(store, author, RUN_ID)
        stored = station_obligations.prune_sweep_failures(store, PROJECT)
        assert len(stored) == 3, stored
        aggregated = [failure for failures in stored.values() for failure in failures]
        rows = station_obligations._with_sweep_failures(
            {"project": PROJECT, "obligations": [], "summary": {}},
            aggregated,
        )["obligations"]
        named = [row for row in rows if str(row.get("run_id") or "") == RUN_ID]
        assert len(named) == 1, named
    finally:
        store.close()
