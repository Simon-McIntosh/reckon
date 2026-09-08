"""Manifest freshness belongs to a run chain, not to one resumed attempt."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption

FIRST_DISPATCH_NS = 1_788_874_500_000_000_000
MANIFEST_NS = 1_788_874_593_998_173_197
RESUME_START_NS = MANIFEST_NS + 1_000_000_000
TERMINAL_CLASSIFICATIONS = {
    "complete": "completed_unpromoted",
    "blocked": "blocked",
    "failed": "failed",
}


def _timestamp(nanoseconds: int) -> str:
    return datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=UTC).isoformat()


def _manifest_text(status: str) -> str:
    text = f"status: {status}\n"
    if status == "waiting":
        text += (
            "wait_condition: scheduler job 42\n"
            'wait_probe: ["scheduler-status", "--job", "42"]\n'
            'wait_terminal: ["COMPLETED", "FAILED"]\n'
            f"wait_started_at: {_timestamp(MANIFEST_NS)}\n"
            "wait_expected: 59m\n"
            "resume_brief: collect the scheduler result\n"
        )
    return text


@pytest.fixture()
def pointer_factory(tmp_path: Path):
    """Build one hermetic pointer varying only attempt number and status."""

    def build(*, attempt: int, status: str) -> dict:
        run_id = f"r-attempt-{attempt}-{status}"
        manifest = tmp_path / f"{run_id}.md"
        manifest.write_text(_manifest_text(status), encoding="utf-8")
        os.utime(manifest, ns=(MANIFEST_NS, MANIFEST_NS))
        worktree = tmp_path / f"{run_id}-worktree"
        worktree.mkdir()
        resumed = attempt > 1
        return {
            "run_id": run_id,
            "project": "fixture-project",
            "process_alive": False,
            "phase": "working",
            "attempt": attempt,
            "attempt_kind": "resume" if resumed else "dispatch",
            "created_at": _timestamp(FIRST_DISPATCH_NS),
            "attempt_started_at": _timestamp(
                RESUME_START_NS if resumed else FIRST_DISPATCH_NS
            ),
            # This reproduces the fault: resume captured the inherited
            # manifest itself as the new attempt's freshness baseline.
            "manifest_baseline_mtime_ns": MANIFEST_NS if resumed else 0,
            "manifest_path": str(manifest),
            "log_path": str(tmp_path / f"{run_id}.jsonl"),
            "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
            "worktree": str(worktree),
            "backend": "fixture-lane",
            "session_id": "fixture-session",
            "node": {
                "id": run_id,
                "role": "implement",
                "time_budget": "20m",
                "write_paths": [],
            },
        }

    return build


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("status", ["complete", "blocked", "failed", "waiting"])
def test_every_attempt_reads_every_manifest_status(
    pointer_factory, attempt: int, status: str
) -> None:
    pointer = pointer_factory(attempt=attempt, status=status)

    row = recovery.classify_pointer(
        pointer,
        now_seconds=MANIFEST_NS / 1_000_000_000 + 60,
        condition_test=lambda _pointer, _wait: {
            "state": "pending",
            "observed": "RUNNING",
        },
    )

    assert row["manifest_file_present"] is True
    assert row["manifest_present"] is True
    assert row["manifest_status"] == status


def test_a_resume_reads_a_manifest_equal_to_its_recorded_baseline(
    pointer_factory,
) -> None:
    pointer = pointer_factory(attempt=2, status="complete")
    manifest = Path(pointer["manifest_path"])

    assert pointer["manifest_baseline_mtime_ns"] == manifest.stat().st_mtime_ns
    row = recovery.classify_pointer(pointer)

    assert row["manifest_present"] is True
    assert row["manifest_status"] == "complete"
    assert row["classification"] == "completed_unpromoted"


def test_a_manifest_from_before_the_run_is_not_a_current_outcome(
    pointer_factory,
) -> None:
    pointer = pointer_factory(attempt=2, status="complete")
    manifest = Path(pointer["manifest_path"])
    stale_ns = FIRST_DISPATCH_NS - 1_000_000_000
    os.utime(manifest, ns=(stale_ns, stale_ns))
    # Reproduces the same equality as an inherited handover, but on the other
    # side of the run's first-dispatch boundary. Equality alone cannot decide
    # freshness without admitting a terminal artifact from before this run.
    pointer["manifest_baseline_mtime_ns"] = stale_ns

    row = recovery.classify_pointer(pointer)

    assert row["manifest_file_present"] is True
    assert row["manifest_present"] is False
    assert row["manifest_status"] is None
    assert row["classification"] != "completed_unpromoted"


@pytest.mark.parametrize(("status", "classification"), TERMINAL_CLASSIFICATIONS.items())
def test_terminal_statuses_come_from_the_inherited_manifest(
    pointer_factory, status: str, classification: str
) -> None:
    pointer = pointer_factory(attempt=2, status=status)

    row = recovery.classify_pointer(pointer)

    assert row["process_alive"] is False
    assert row["manifest_status"] == status
    assert row["classification"] == classification


def test_a_parked_resume_stays_in_the_resume_sweep(
    pointer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = pointer_factory(attempt=2, status="waiting")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])

    classified = recovery.classify_pointer(
        pointer,
        condition_test=lambda _pointer, _wait: {
            "state": "met",
            "observed": "COMPLETED",
        },
    )
    report = resumption.sweep(
        "fixture-project",
        dry_run=True,
        condition_test=lambda _pointer, _wait: {
            "terminal": True,
            "observed": "COMPLETED",
        },
    )

    assert classified["classification"] == recovery.WAITING_STATUS
    assert classified["recovery_classification"] == "ready"
    assert report["checked"] == 1
    assert [row["run_id"] for row in report["resumed"]] == [pointer["run_id"]]
    assert report["resumed"][0]["would_resume"] is True
