"""Obligations describe the current review and admission hold for each run."""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "obligation-fixture"
SESSION = "obligation-session"
NOW = datetime.now(tz=UTC)
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


def _delivered(home: tuple[Path, str], run_id: str) -> dict:
    root, head = home
    manifest = root.parent / f"{run_id}.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    old = (NOW - timedelta(minutes=50)).timestamp()
    os.utime(manifest, (old, old))
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
    return pointer


def _review(run_id: str, head: str, *, findings: list[dict] | None = None) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
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


def _row(run_id: str) -> dict:
    matches = [
        item
        for item in obligations.obligations(PROJECT, SESSION)["obligations"]
        if item["run_id"] == run_id
    ]
    assert matches, run_id
    return matches[0]


def test_open_blocking_finding_owes_repair_instead_of_promotion(
    home: tuple[Path, str],
) -> None:
    run_id = "r-open-finding"
    _delivered(home, run_id)
    _review(run_id, home[1], findings=[FINDING])

    row = _row(run_id)

    assert row["kind"] != "promotable-stale", row
    assert "crew complete" not in row["next_command"], row
    assert "repair" in row["kind"], row


def test_advice_hold_names_the_reviewed_runs_resume(home: tuple[Path, str]) -> None:
    run_id = "r-advice-held"
    pointer = _delivered(home, run_id)
    _review(run_id, home[1], findings=[FINDING])
    pointer["repair_dispatch"] = {
        "status": "advice-hold",
        "reason": "repair held for coordinator advice",
        "at": NOW.isoformat(),
    }
    runs._write_json(runs.pointer_path(run_id), pointer)

    row = _row(run_id)

    assert "advice" in f"{row['reason']} {row['next_command']}", row
    assert f"reckon crew resume --run {run_id}" in row["next_command"], row
    assert "crew complete" not in row["next_command"], row


def test_overdue_review_duty_reports_age_without_a_dispatch_command(
    home: tuple[Path, str],
) -> None:
    run_id = "r-overdue-review"
    _delivered(home, run_id)
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        row = _row(run_id)

    assert row["kind"] == "review-missing", row
    assert row["age_seconds"] > 120, row
    assert "overdue" in row["next_command"], row
    assert "50m" in row["next_command"], row
    assert "within about 120 s" not in row["next_command"], row
    assert "reckon crew dispatch" not in row["next_command"], row


def test_working_review_names_its_run(home: tuple[Path, str]) -> None:
    run_id = "r-review-subject"
    review_run_id = "r-review-worker"
    pointer = _delivered(home, run_id)
    fields = recovery._review_dispatch_fields(pointer)
    review_pointer = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "working",
        "role": "review",
        "node": {
            "id": review_run_id,
            "plan": "fixture",
            "section": "fixture",
            "role": "review",
            "write_paths": fields["write_paths"],
        },
    }
    runs._write_json(runs.pointer_path(review_run_id), review_pointer)
    row = _row(run_id)

    assert row["kind"] != "review-missing", row
    assert review_run_id in f"{row.get('reason', '')} {row['next_command']}", row
    assert "working" in f"{row.get('reason', '')} {row['next_command']}", row


def test_resolved_admission_refusal_yields_current_queue_hold(
    home: tuple[Path, str],
) -> None:
    run_id = "r-queued"
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "queued",
        "queued_at": (NOW - timedelta(minutes=3)).isoformat(),
        "failure": (
            "UnreconciledRuns: run r-answered needed reconciliation; "
            "reckon crew complete --run r-answered"
        ),
        "node": {"id": run_id, "plan": "fixture", "section": "fixture"},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    row = _row(run_id)

    assert row["kind"] == "run-queued", row
    assert "r-answered" not in row["next_command"], row
    assert "crew complete" not in row["next_command"], row


def test_admission_held_row_names_only_its_run(home: tuple[Path, str]) -> None:
    run_id = "r-held-own"
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "held",
        "queue_hold": {
            "started_at": (NOW - timedelta(hours=25)).isoformat(),
            "reason": "admission allowance pending",
        },
        "node": {"id": run_id, "plan": "fixture", "section": "fixture"},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    row = _row(run_id)

    assert row["run_id"] == run_id, row
    assert run_id in row["next_command"], row
    assert "r-answered" not in row["next_command"], row
