"""A failed or vanished run costs only its own turn in a review sweep."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reckon import ledger
from reckon.crew import promotion, resumption, runs, station
from reckon.crew import recovery_review_acceptance as acceptance
from reckon.crew import review as review_module
from tests.test_a_review_run_promotes_itself import PROJECT
from tests.test_a_review_run_promotes_itself import (
    repository as repository,  # noqa: PLC0414 - register the shared fixture
)


@pytest.fixture
def review_runs(repository, tmp_path):
    pointers = []
    for name in ("first", "second", "third"):
        run_id = f"r-review-{name}"
        reviewed_id = f"r-source-{name}"
        head = "a" * 40
        record_path = review_module.review_path(
            PROJECT, reviewed_id, reviewed_head_sha=head
        )
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "status": "parsed",
                    "project": PROJECT,
                    "reviewed_run_id": reviewed_id,
                    "review_run_id": run_id,
                    "reviewed_head_sha": head,
                    "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
                    "total": 20 * len(review_module.REVIEW_DIMENSIONS),
                    "findings": [],
                }
            )
        )
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.md"
        manifest.write_text("status: complete\ncheckpoint: record stored\n")
        (directory / "exit.json").write_text(json.dumps({"exit_code": 0}))
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "session": "session-fixture",
            "repo": str(repository),
            "launch": "in-harness",
            "created_at": "2026-10-12T13:00:00Z",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{reviewed_id}",
                "role": "review",
                "write_paths": [str(record_path)],
            },
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        pointers.append(pointer)
    return pointers


@pytest.mark.parametrize("through_sweep", [False, True])
def test_ledger_failure_and_vanished_pointer_leave_the_third_promoted(
    review_runs, repository, monkeypatch, through_sweep
):
    complete = promotion.complete

    def fail_first(run_id, **kwargs):
        if run_id == "r-review-first":
            runs.pointer_path("r-review-second").unlink()
            raise ledger.LedgerError("could not stage review write: index.lock held")
        return complete(run_id, **kwargs)

    monkeypatch.setattr(promotion, "complete", fail_first)
    if through_sweep:
        monkeypatch.setattr(resumption, "external_wait", lambda *a, **k: None)
        monkeypatch.setattr(resumption, "_stream_refusal_block", lambda *a: None)
        monkeypatch.setattr(
            resumption.plan_review, "compose_owed_plan_reviews", lambda *a, **k: {}
        )
        # The vanished case is an ordering: the fixture removes a later run's
        # pointer from inside an earlier run's promotion, so this sweep runs
        # one promotion at a time. Beside a concurrent sweep that run's
        # promotion can start before the pointer is removed, and the run is
        # then promoted rather than reported vanished — a correct outcome the
        # fixture does not assert.
        report = resumption.sweep(
            PROJECT,
            session="session-fixture",
            pointers=review_runs,
            config={"promotion": {"width": 1, "wait_seconds": 60}},
            promotions=resumption.PromotionWorkers(width=1, wait_seconds=60),
        )["reviews"]
    else:
        report = acceptance.dispatch_awaiting_reviews(
            project=PROJECT, session="session-fixture", pointers=review_runs
        )

    assert report["self_promoted"] == ["r-review-third"], report
    [failure] = report["failed"]
    assert failure["run_id"] == "r-review-first"
    assert "LedgerError" in failure["error"]
    assert "index.lock held" in failure["error"]
    assert [entry["run_id"] for entry in report["vanished"]] == ["r-review-second"]
    assert runs.pointer_path("r-review-first").exists()
    assert not runs.pointer_path("r-review-third").exists()
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [
        "r-review-third"
    ]


def test_value_error_leaves_other_reviews_promoted(review_runs, monkeypatch):
    complete = promotion.complete

    def fail_first(run_id, **kwargs):
        if run_id == "r-review-first":
            raise ValueError("unreadable ledger row")
        return complete(run_id, **kwargs)

    monkeypatch.setattr(promotion, "complete", fail_first)
    report = acceptance.dispatch_awaiting_reviews(
        project=PROJECT, session="session-fixture", pointers=review_runs
    )
    assert report["self_promoted"] == ["r-review-second", "r-review-third"], report
    assert report["failed"] == [
        {"run_id": "r-review-first", "error": "ValueError: unreadable ledger row"}
    ]


@pytest.mark.parametrize("stage", ["review", "plan-review"])
def test_sweep_records_unexpected_pass_failure_and_finishes(monkeypatch, stage):
    calls = []

    def review(**kwargs):
        calls.append("review")
        if stage == "review":
            raise ValueError("review unavailable")
        return {}

    def plan_review(*args, **kwargs):
        calls.append("plan-review")
        if stage == "plan-review":
            raise ledger.LedgerError("plan review unavailable")
        return {}

    monkeypatch.setattr(resumption, "dispatch_awaiting_reviews", review)
    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", plan_review
    )
    report = resumption.sweep(PROJECT, pointers=[])
    key = "reviews" if stage == "review" else "plan_reviews"
    assert "unavailable" in report[key]["error"]
    assert calls == ["review", "plan-review"]
    assert report["swept_at"]


def test_station_names_each_failed_run_and_allows_next_pass(capsys):
    def sweep(*args, **kwargs):
        return {
            "reviews": {
                "failed": [
                    {"run_id": "r-first", "error": "LedgerError: index.lock held"},
                    {"run_id": "r-second", "error": "ValueError: unreadable row"},
                ]
            }
        }

    beat = station.SweepBeat(sweep=sweep, start=lambda action: action())
    for _ in range(2):
        assert beat.request(
            PROJECT, "session-fixture", moment=datetime.now(UTC), changed=True
        )
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 4
    assert all("station-sweep-run-failure" in line for line in lines)
    assert all(f"{PROJECT}/session-fixture" in line for line in lines)
    assert sum("r-first" in line and "index.lock held" in line for line in lines) == 2
    assert sum("r-second" in line and "unreadable row" in line for line in lines) == 2
