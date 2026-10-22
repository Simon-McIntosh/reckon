"""A queued run refused at launch names its refusal and states its remedy.

A queued request's launch is re-checked every time a slot opens, and a launch
can be refused for a condition the lane will never clear: a plan whose review
is missing, for one. That refusal is stamped on the queued pointer's
``failure`` field, and the obligations view reads the pointer through the
classifier. The reader there used to drop every refusal whose text did not
name the session, so a ``PlanReviewMissingError`` — which names the plan and
its remedy, never the session — left the coordinator reading "waiting for a
lane slot" for hours. The tests below hold three queued pointers for one
session — a review refusal that names no session, a refusal that does name it,
and a lane hold — and require each to read as the plan says.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.dispatch_watch import _FollowerAdmissionUnmet
from reckon.crew.node import PlanReviewMissingError
from reckon.crew.plan_review import review_invocation
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "queued-remedy-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
OBSERVED_AT = datetime(2026, 10, 22, 5, 50, tzinfo=UTC)


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
        ("commit", "-q", "-m", "test: seed queued-remedy fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_queued(run_id: str, *, failure: str) -> None:
    """Write one queued pointer carrying the admission failure it was left with.

    The failure is spelled exactly as the admission pass stamps it — the
    exception's type name, the same way ``admit_once`` composes it — so the
    test reads the refusal a real pass would have recorded rather than a
    hand-written stand-in for one.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "queued",
            "queued_at": "2026-10-22T04:00:00+00:00",
            "reason": "waiting for a local lane slot",
            "failure": failure,
            "failure_at": "2026-10-22T05:40:00+00:00",
            "failure_attempts": 3,
            "retry_at": "2026-10-22T06:00:00+00:00",
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _failure_of(exception: BaseException) -> str:
    """The recorded failure text for one raised refusal, as a pass composes it."""
    return f"{type(exception).__name__}: {exception}"


def _review_invocation() -> str:
    """The command the plan-review gate names as its remedy."""
    return review_invocation(PROJECT, PLAN)


def _review_refusal() -> str:
    """A ``PlanReviewMissingError`` refusal, naming no session.

    The detail is the sentence the gate composes for an uncovered unit, with
    its remedy quoted in backticks exactly as ``require_plan_reviewed`` emits
    it. The plan slug and the ``<session>`` placeholder are what the gate
    writes; the coordinator's session id appears nowhere in it.
    """
    return _failure_of(
        PlanReviewMissingError(
            f"plan {PLAN!r} in project {PROJECT!r} carries no stored review of "
            "the content about to be built; uncovered units: s74; "
            "a plan is reviewed before it is built; compose one with "
            f"`{_review_invocation()}`"
        )
    )


def _session_refusal() -> str:
    """A refusal that names the session, as the follower's unmet condition does."""
    return _failure_of(
        _FollowerAdmissionUnmet(
            PROJECT,
            {"attach_line": "reckon crew follow --project fixture"},
            session=SESSION,
            conditions=[f"session {SESSION!r} has no registered follower"],
        )
    )


def _lane_hold() -> str:
    return _failure_of(
        LaneHeld(
            {
                "state": "full",
                "detail": (
                    f"the lane grants session {SESSION!r} no worker slot; "
                    "the gate is open and the lane will launch it when a slot frees"
                ),
            }
        )
    )


def _row(run_id: str) -> dict[str, Any]:
    classified = recovery.classify_pointer(runs.read_pointer(run_id))
    assert classified["run_id"] == run_id
    return classified


def test_a_review_refusal_names_the_review_command(repository: Path) -> None:
    """A refusal that names no session is surfaced with the remedy it states.

    The refusal names the plan and quotes the ``reckon crew review`` that
    composes the missing review; the row publishes that command rather than
    sending the coordinator to wait on a lane that will keep refusing.
    """
    _write_queued("r-review-refusal", failure=_review_refusal())
    row = _row("r-review-refusal")
    assert row["classification"] == "queued"
    assert row["session_refusal"] is not None, row
    assert row["next_action"] == _review_invocation(), row["next_action"]
    assert "PlanReviewMissingError" in row["detail"], row["detail"]


def test_the_review_refusal_appears_in_the_sessions_obligations(
    repository: Path,
) -> None:
    """The obligations item names the refusal and the command that clears it."""
    _write_queued("r-review-refusal", failure=_review_refusal())
    owed = obligations_module.obligations(PROJECT, SESSION)
    listed = [item for item in owed["obligations"] if item["run_id"] == "r-review-refusal"]
    assert len(listed) == 1, owed["obligations"]
    assert listed[0]["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND
    assert listed[0]["next_command"] == _review_invocation(), listed[0]["next_command"]


def test_a_refusal_that_names_the_session_keeps_the_follower_remedy(
    repository: Path,
) -> None:
    """A session-side refusal still publishes the follower the session arms."""
    _write_queued("r-session-refusal", failure=_session_refusal())
    row = _row("r-session-refusal")
    assert row["classification"] == "queued"
    assert row["next_action"] == runs._watch_attach_line(PROJECT, session=SESSION)


def test_a_lane_hold_still_reads_as_waiting_for_a_slot(repository: Path) -> None:
    """A lane hold is the lane speaking and keeps the waiting reading."""
    _write_queued("r-lane-hold", failure=_lane_hold())
    row = _row("r-lane-hold")
    assert row["classification"] == "queued"
    assert row.get("session_refusal") is None, row
    assert row["next_action"] == "wait for a local lane slot", row["next_action"]


def test_the_three_readings_hold_side_by_side(repository: Path) -> None:
    """One session holding all three reads all three, told apart."""
    _write_queued("r-review-refusal", failure=_review_refusal())
    _write_queued("r-session-refusal", failure=_session_refusal())
    _write_queued("r-lane-hold", failure=_lane_hold())
    owed = obligations_module.obligations(PROJECT, SESSION)
    kinds = {item["run_id"]: item["kind"] for item in owed["obligations"]}
    assert kinds == {
        "r-review-refusal": obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND,
        "r-session-refusal": obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND,
        "r-lane-hold": obligations_module.QUEUED_RUN_DUTY_KIND,
    }, kinds