"""A request waiting on a queued plan review is a wait, not a duty.

A queued request the admission pass refused with ``plan-review-missing`` reads
as a duty that tells the coordinator to compose the review. When a review of
that plan is already live or queued for the same session, that command only
queues a duplicate review and the request is waiting on work already in flight,
which no action at this session shortens. Both producers of the session's
obligations — the derivation and the published snapshot the stop hook reads —
must read the request as a wait naming the review run, and both keep the duty
when no such review exists.

The two controls for this file mutate one producer at a time: dropping live plan
reviews from the derivation only reddens the derivation cases, and dropping them
from the snapshot alone reddens the snapshot cases while the two derivation
cases stay green.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, plan_review, runs
from reckon.crew.node import PlanReviewMissingError
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "queued-review-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
REQUEST_RUN_ID = "r-queued-request-refused"
REVIEW_RUN_ID = "r-plan-review-queued"
OBSERVED_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


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
        ("commit", "-q", "-m", "test: seed queued-review fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _plan_review_refusal() -> str:
    """The admission failure the dispatch gate records for a missing review.

    Spelled exactly as the gate raises it — the project, the plan and the
    compose command in backticks — so the remedy is read from the refusal the
    real pass would have stored rather than from a hand-written stand-in.
    """
    detail = (
        f"plan {PLAN!r} in project {PROJECT!r} carries no stored review of the "
        "content about to be built; uncovered units: fixture-section; "
        "a plan is reviewed before it is built; compose one with "
        f"`{plan_review.review_invocation(PROJECT, PLAN)}`"
    )
    refusal = PlanReviewMissingError(detail)
    return f"{type(refusal).__name__}: {refusal}"


def _write_queued_request() -> None:
    runs._write_json(
        runs.pointer_path(REQUEST_RUN_ID),
        {
            "run_id": REQUEST_RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "phase": "queued",
            "queued_at": "2026-11-08T10:00:00+00:00",
            "reason": "waiting for a local lane slot",
            "failure": _plan_review_refusal(),
            "failure_at": "2026-11-08T11:40:00+00:00",
            "failure_attempts": 1,
            "retry_at": "2026-11-08T12:00:00+00:00",
            "node": {
                "id": REQUEST_RUN_ID,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _write_queued_plan_review(*, session: str = SESSION) -> None:
    """One plan review composed for the plan, queued for the lane."""
    runs._write_json(
        runs.pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "session": session,
            "phase": "queued",
            "queued_at": "2026-11-08T11:00:00+00:00",
            "reason": "waiting for a local lane slot",
            "node": {
                "id": f"plan-review-of-{PLAN}",
                "plan": "",
                "section": "",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _derivation_row() -> dict[str, Any]:
    owed = obligations_module.obligations(PROJECT, SESSION)
    listed = [item for item in owed["obligations"] if item["run_id"] == REQUEST_RUN_ID]
    assert len(listed) == 1, owed["obligations"]
    return listed[0]


def _published_snapshot_row() -> dict[str, Any]:
    """The request's row as the stop hook reads it, from the published file."""
    state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    payload = obligation_snapshot.payload_for(state, SESSION)
    document = obligation_snapshot.document_for(
        payload,
        computed_at=OBSERVED_AT,
        stream_offset=0,
        producer={
            "pid": None,
            "pid_start_time": None,
            "started_at": None,
            "code_stamp": None,
        },
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
    published = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert published is not None
    listed = [
        item for item in published["obligations"] if item["run_id"] == REQUEST_RUN_ID
    ]
    assert len(listed) == 1, published["obligations"]
    return listed[0]


def test_the_derivation_lists_the_request_waiting_on_its_plan_review(
    repository: Path,
) -> None:
    """With a queued review of the plan, the request is a wait naming it.

    The row is the lane-wait kind, whose remedy no action at this session
    shortens, and it names the review run instead of printing the compose
    command that would only queue a duplicate.
    """
    _write_queued_request()
    _write_queued_plan_review()

    row = _derivation_row()

    assert row["kind"] == obligations_module.QUEUED_RUN_DUTY_KIND, row
    assert row["review_run_id"] == REVIEW_RUN_ID, row
    assert "crew review" not in str(row.get("next_command") or ""), row


def test_the_derivation_keeps_the_duty_without_a_live_plan_review(
    repository: Path,
) -> None:
    """No review of the plan leaves the request a duty with its compose command."""
    _write_queued_request()

    row = _derivation_row()

    assert row["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND, row
    assert "review_run_id" not in row, row
    assert "crew review" in str(row.get("next_command") or ""), row


def test_the_published_snapshot_lists_the_request_waiting_on_its_plan_review(
    repository: Path,
) -> None:
    """The stop hook's snapshot reads the same wait, sliced from fleet state."""
    _write_queued_request()
    _write_queued_plan_review()

    row = _published_snapshot_row()

    assert row["kind"] == obligations_module.QUEUED_RUN_DUTY_KIND, row
    assert row["review_run_id"] == REVIEW_RUN_ID, row


def test_the_published_snapshot_keeps_the_duty_without_a_live_plan_review(
    repository: Path,
) -> None:
    _write_queued_request()

    row = _published_snapshot_row()

    assert row["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND, row
    assert "review_run_id" not in row, row


def test_a_plan_review_another_session_owns_does_not_clear_the_refusal(
    repository: Path,
) -> None:
    """The recovery reading of a review belongs to the session that composed it."""
    _write_queued_request()
    _write_queued_plan_review(session="another-coordinator")

    row = _derivation_row()

    assert row["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND, row
    assert "review_run_id" not in row, row
