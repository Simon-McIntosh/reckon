"""A review waiting in the lane's queue is queued, not missing.

A review dispatched into the lane's queue lives as a queued run pointer — no
review record is stored and no live review pointer exists — so the run it was
composed for had no review the obligations view could find and read
``review-missing``, naming a dispatch the coordinator had already made. The
queued pointer names the run and head it was composed for, so it is the second
source of the same ``review-queued`` kind the refused-attempt reading already
feeds, carrying its place in the admission order.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "queued-review-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed queued review fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )
    return root


def _write_delivered_run(
    repository: Path, tmp_path: Path, *, run_id: str, node_id: str, head: str
) -> dict:
    """Record one complete, unreviewed run whose tree is at ``head``."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _write_queued_review(
    *,
    review_run_id: str,
    reviewed_run_id: str,
    head: str,
    queued_at: datetime,
) -> dict:
    """Record one review run the lane accepted into the queue and has not launched."""
    pointer = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "queued",
        "backend": "local",
        "queued_at": queued_at.isoformat(),
        "role": "review",
        "node": {
            "id": f"review-{reviewed_run_id}",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "role": "review",
            "time_budget": "20m",
            "write_paths": [
                str(
                    review_module.review_path(
                        PROJECT, reviewed_run_id, reviewed_head_sha=head
                    )
                )
            ],
        },
    }
    runs._write_json(runs.pointer_path(review_run_id), pointer)
    return pointer


def _score_row(repository: Path, run_id: str) -> dict:
    return recovery.classify_pointer(
        runs.read_pointer(run_id), now_seconds=OBSERVED_AT.timestamp()
    )


def _obligations_for_review_duty(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict]
) -> dict:
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: rows)
    return obligations_module.obligations(PROJECT, SESSION)


def _review_duty(result: dict) -> dict:
    duties = [item for item in result["obligations"] if "head" in item]
    assert len(duties) == 1, result["obligations"]
    return duties[0]


def test_a_queued_review_reads_review_queued_not_review_missing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-awaiting-review"
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, run_id=run_id, node_id="src", head=head)
    row = _score_row(repository, run_id)
    assert row["classification"] == "scoring"

    queued_absent = _obligations_for_review_duty(monkeypatch, [row])
    assert _review_duty(queued_absent)["kind"] == "review-missing"

    _write_queued_review(
        review_run_id="r-queued-review",
        reviewed_run_id=run_id,
        head=head,
        queued_at=OBSERVED_AT - timedelta(seconds=30),
    )
    queued = _obligations_for_review_duty(monkeypatch, [row])
    duty = _review_duty(queued)
    assert duty["kind"] == "review-queued"
    assert duty["position"] == 1
    assert duty["rank"] == 1


def test_removing_the_queued_review_returns_review_missing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-awaiting-review"
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, run_id=run_id, node_id="src", head=head)
    row = _score_row(repository, run_id)
    _write_queued_review(
        review_run_id="r-queued-review",
        reviewed_run_id=run_id,
        head=head,
        queued_at=OBSERVED_AT - timedelta(seconds=30),
    )
    assert (
        _review_duty(_obligations_for_review_duty(monkeypatch, [row]))["kind"]
        == "review-queued"
    )

    runs.pointer_path("r-queued-review").unlink()

    duty = _review_duty(_obligations_for_review_duty(monkeypatch, [row]))
    assert duty["kind"] == "review-missing"


def test_a_queued_review_of_another_run_leaves_this_run_missing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-awaiting-review"
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, run_id=run_id, node_id="src", head=head)
    row = _score_row(repository, run_id)
    _write_queued_review(
        review_run_id="r-queued-review",
        reviewed_run_id="some-other-run",
        head=head,
        queued_at=OBSERVED_AT - timedelta(seconds=30),
    )

    duty = _review_duty(_obligations_for_review_duty(monkeypatch, [row]))
    assert duty["kind"] == "review-missing"
