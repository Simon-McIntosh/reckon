"""A cli review that never launched does not read as an in-harness run.

A reflex-dispatched review writes its live pointer and its claim, and then the
launch stops before a supervisor exists. The pointer holds ``launch: cli`` and a
pre-spawn phase, the run directory holds ``claim.json`` and nothing else, and
no attempt was ever composed — so neither ``attempt_started_at`` nor an attempt
claim dates it. A pointer with no launch clock cannot age, and the classifier
fell through to the in-harness sentence, which reports it as running until a
manifest appears. A cli run is not in-harness, so a review that never started
read as running indefinitely and surfaced only as an overdue review.

The launch is dated from the claim its dispatch registered, and failing that
from the pointer's own creation, so past the stranded-launch bound it is read
as a launch failure naming the host that issued it. The fallback is confined to
a cli pointer: an in-harness pointer under the same conditions still reads as
running, because its liveness belongs to the calling harness.

The three cases run against a temporary crew home, so the operator's live
pointers and run directories are never read or written.

The declared negative control removes the claim-and-creation fallback from the
dating; the first case then reads as running, at the base revision and under
the mutation.
"""

from __future__ import annotations

import importlib
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery_liveness, runs
from reckon.crew.runs import _write_json, run_dir, runs_dir

classification_module = importlib.import_module("reckon.crew.recovery_classification")

# The declared mutation, verbatim: the string a promotion audit compares the red
# log against.
DECLARED_MUTATION = (
    "removing the claim-and-creation fallback from the dating makes the first "
    "case read as running"
)

MUTATION_ENV = "RECKON_NEVER_LAUNCHED_FALLBACK_NEGATIVE_CONTROL"

PROJECT = "never-launched-fixture"
SESSION = "never-launched-session"
STALE_RUN_ID = "r-never-launched-stale"
YOUNG_RUN_ID = "r-never-launched-young"
IN_HARNESS_RUN_ID = "r-never-launched-in-harness"
LAUNCHER_HOST = "98dci4-clu-2068"

BOUND = recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS
#: Well inside a launch that may still be spawning its supervisor.
YOUNG_SECONDS = 20
#: Well past the bound, so nothing about the launch can still be in flight.
STALE_SECONDS = BOUND + 120


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The crew home is a temporary directory; the operator's is never read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    if os.environ.get(MUTATION_ENV) == "1":
        # The declared mutation: a launch with no attempt clock is left undated,
        # exactly as the base revision reads it.
        monkeypatch.setattr(
            recovery_liveness,
            "_claim_or_creation_seconds",
            lambda record: None,
        )
    return config_home


def _never_launched_pointer(
    run_id: str,
    *,
    claimed_seconds_ago: float,
    launch: str = "cli",
    launcher_host: str = LAUNCHER_HOST,
) -> dict:
    """A launch that stopped between composing its claim and spawning a worker.

    Every trace a spawned worker would leave is absent by construction: no
    attempt record and no attempt claim date the run, the pointer names no pid,
    and the stream and stderr path it names were never written. The claim is the
    one record the launch left, and it carries the moment the run began.
    """
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    claimed = datetime.now(UTC) - timedelta(seconds=claimed_seconds_ago)
    # The claim a dispatch registers before it composes anything else. Its name
    # is written out rather than read from the module under test, so the case
    # reports the defect in the dating, not a name the base revision lacks.
    _write_json(
        directory / "claim.json",
        {
            "run_id": run_id,
            "claim_registered_at": claimed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(runs_dir()),
        "worktree": str(runs_dir() / f"{run_id}-tree"),
        "launch": launch,
        "launcher_host": launcher_host,
        "backend": "codex",
        "role": "review",
        "session": SESSION,
        "session_id": SESSION,
        "created_at": claimed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "starting",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(directory / "manifest.md"),
        "node": {
            "id": run_id,
            "plan": "a-dispatch-returns-without-its-worker",
            "section": "a-review-that-never-launched-is-not-running",
            "time_budget": "60m",
            "write_paths": [],
        },
        "brief": {"path": str(directory / "review-brief.md")},
    }
    _write_json(runs.pointer_path(run_id), record)
    return record


def _row(run_id: str) -> dict:
    return classification_module.classify_pointer(runs.read_pointer(run_id))


def test_a_stale_review_with_no_attempt_clock_is_a_launch_failure() -> None:
    """The launch was cut off; its claim dates it and no model was reached."""
    _never_launched_pointer(STALE_RUN_ID, claimed_seconds_ago=STALE_SECONDS)

    row = _row(STALE_RUN_ID)

    assert row["classification"] == "launch-failed"
    assert row["recovery_classification"] == "launch-failed"
    assert "stranded launch" in row["detail"]
    assert LAUNCHER_HOST in row["detail"]


def test_a_young_review_with_no_attempt_clock_still_reads_as_running() -> None:
    """A launch this young may still be spawning its supervisor."""
    _never_launched_pointer(YOUNG_RUN_ID, claimed_seconds_ago=YOUNG_SECONDS)

    row = _row(YOUNG_RUN_ID)

    assert row["classification"] == "running"


def test_an_in_harness_run_with_no_attempt_clock_still_reads_as_running() -> None:
    """In-harness liveness belongs to the calling harness, however old the claim."""
    _never_launched_pointer(
        IN_HARNESS_RUN_ID,
        claimed_seconds_ago=STALE_SECONDS,
        launch="in-harness",
        launcher_host=socket.gethostname(),
    )

    row = _row(IN_HARNESS_RUN_ID)

    assert row["classification"] == "running"
    assert "in-harness" in row["detail"]
