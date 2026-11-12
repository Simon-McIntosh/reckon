"""A delivered review awaiting promotion reads as awaiting promotion.

A run whose review has been dispatched, whose review run has delivered, and
whose review record is not yet landed, is waiting on a promotion — not on a
follower. The duty read ``review-missing`` in that window, telling the
coordinator the reflex had not dispatched a review that had already completed,
so the wait named was a follower inspection while the real wait was a review
promotion.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "delivered-review-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 11, 12, 10, 50, tzinfo=UTC)
REVIEW_RUN_ID = "r-20261112T071728958783-review-of-the-delivered-run"


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
        ("commit", "-q", "-m", "test: seed delivered review fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )
    return root


def _write_delivered_run(
    repository: Path, tmp_path: Path, *, run_id: str, head: str
) -> dict:
    """Record one complete, unreviewed run whose tree is at ``head``."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: src\nstatus: complete\ncommits: [{head}]\n",
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
            "id": "src",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _record_review_dispatch(run_id: str, *, review_run_id: str, head: str) -> None:
    """Record on the run's pointer the review the reflex dispatched."""
    pointer = runs.read_pointer(run_id)
    pointer["review_dispatch"] = {
        "status": "dispatched",
        "run_id": review_run_id,
        "head": head,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)


def _write_review_pointer(
    *, review_run_id: str, reviewed_run_id: str, head: str, phase: str
) -> dict:
    """Record the review run itself at ``phase``."""
    pointer = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "backend": "local",
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


def _store_review(*, run_id: str, head: str) -> Path:
    """Store one complete review of ``run_id`` at ``head``."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": REVIEW_RUN_ID,
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _score_row(run_id: str) -> dict:
    return recovery.classify_pointer(
        runs.read_pointer(run_id), now_seconds=OBSERVED_AT.timestamp()
    )


def _duties(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> list[dict]:
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: rows)
    result = obligations_module.obligations(PROJECT, SESSION)
    return list(result["obligations"])


def _review_duty(duties: list[dict]) -> dict:
    review_duties = [item for item in duties if "head" in item]
    assert len(review_duties) == 1, duties
    return review_duties[0]


def test_a_delivered_review_reads_awaiting_promotion(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20261112T060543442651-delivered-run"
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, run_id=run_id, head=head)
    _record_review_dispatch(run_id, review_run_id=REVIEW_RUN_ID, head=head)
    row = _score_row(run_id)
    assert row["classification"] == "scoring"
    assert row["review_run_id"] is None

    # The review is dispatched but its run has delivered nothing yet: the duty
    # still names the dispatch.
    _write_review_pointer(
        review_run_id=REVIEW_RUN_ID,
        reviewed_run_id=run_id,
        head=head,
        phase="working",
    )
    working = _duties(monkeypatch, [_score_row(run_id)])
    assert [item["kind"] for item in working] == ["review-in-flight"]

    # The review delivers. Its record is not landed, so the run waits on a
    # promotion rather than on a review that does not exist.
    _write_review_pointer(
        review_run_id=REVIEW_RUN_ID,
        reviewed_run_id=run_id,
        head=head,
        phase="complete",
    )
    delivered_row = _score_row(run_id)
    assert delivered_row["review_run_id"] is None
    duty = _review_duty(_duties(monkeypatch, [delivered_row]))

    assert duty["kind"] == "review-awaiting-promotion"
    assert duty["kind"] != "review-missing"
    assert duty["review_run_id"] == REVIEW_RUN_ID
    assert REVIEW_RUN_ID in duty["next_command"]
    assert f"--run {REVIEW_RUN_ID}" in duty["next_command"]
    assert REVIEW_RUN_ID in duty["reason"]


def test_a_promoted_delivered_review_reads_ready(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the record lands the run is ready to promote, not awaiting it."""
    run_id = "r-20261112T060543442651-delivered-run"
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, run_id=run_id, head=head)
    _record_review_dispatch(run_id, review_run_id=REVIEW_RUN_ID, head=head)
    _write_review_pointer(
        review_run_id=REVIEW_RUN_ID,
        reviewed_run_id=run_id,
        head=head,
        phase="complete",
    )
    _store_review(run_id=run_id, head=head)

    duty = _review_duty(_duties(monkeypatch, [_score_row(run_id)]))

    assert duty["kind"] == "review-ready"


def test_a_delivered_review_of_a_superseded_head_owes_a_new_review(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered review of an older revision is not this run's promotion.

    Landing its record would land evidence for work the run has moved past, so
    the run still owes a review of the head it carries now.
    """
    run_id = "r-20261112T060543442651-delivered-run"
    head = _git(repository, "rev-parse", "HEAD")
    superseded_head = "0" * 40
    _write_delivered_run(repository, tmp_path, run_id=run_id, head=head)
    _record_review_dispatch(run_id, review_run_id=REVIEW_RUN_ID, head=superseded_head)
    _write_review_pointer(
        review_run_id=REVIEW_RUN_ID,
        reviewed_run_id=run_id,
        head=superseded_head,
        phase="complete",
    )

    duty = _review_duty(_duties(monkeypatch, [_score_row(run_id)]))

    assert duty["kind"] != "review-awaiting-promotion"
    assert duty["kind"] == "review-missing"


def test_a_completed_review_of_another_run_leaves_this_run_missing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the review the dispatch names counts as awaiting promotion."""
    run_id = "r-20261112T060543442651-delivered-run"
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, run_id=run_id, head=head)
    _record_review_dispatch(run_id, review_run_id=REVIEW_RUN_ID, head=head)
    _write_review_pointer(
        review_run_id="r-20261112T071728958783-review-of-another-run",
        reviewed_run_id="some-other-run",
        head=head,
        phase="complete",
    )

    duty = _review_duty(_duties(monkeypatch, [_score_row(run_id)]))

    assert duty["kind"] == "review-missing"
