"""A held request names the failure that stopped it, and a full lane is not a signal.

A queued request's pointer hold read the lane router's congestion sentence while
its last launch had failed on an unanswered plan-review finding: the request's
own record already carried that failure and the command that clears it. The
obligations row then read "stuck: held-for-allowance for 34574s" — a lane figure
the lead's own reading had already contradicted, an age measured from the queue
rather than from the reason shown, and no mention of the finding that held it.

Four readings are pinned here, against a temporary crew home and one mounted
project, never the real one: a launch that fails on a refusal naming its remedy
leaves that failure and that command on the request's own hold, replacing the
earlier lane reason; the obligations row for such a request names the failure
and its command rather than the lane, and measures the wait from the instant
that failure was recorded; the pass's own verdict row names that failure rather
than a capacity hold; and a request held only for the lane's capacity is a
waiting row no stop blocks, which no surface reports as a full lane.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligations as obligations_module
from reckon.crew import queue_admission, runs
from reckon.hooks import coordinator_obligations as hook

PROJECT = "held-failure-fixture"
PLAN = "the-held-fixture"
SESSION = "coordinator-fixture"
RUN_ID = "r-held-on-a-review"
LANE_RUN_ID = "r-held-on-the-lane"
OBSERVED_AT = datetime(2026, 11, 15, 22, 25, 15, tzinfo=UTC)

REVIEW_COMMAND = (
    f"reckon crew review --project {PROJECT} --plan {PLAN} --rubric content"
)
PLAN_REVIEW_FAILURE = (
    "PlanReviewMissingError: the plan carries no review; compose it with "
    f"`{REVIEW_COMMAND}`"
)
# The sentence the lane's router publishes, in the shape the pass records it.
ROUTER_FULL = (
    "the lane's router grants session "
    f"{SESSION!r} no extra worker slot (congested, admission headroom -1)"
)
HOLD_RECORDED_AT = datetime(2026, 11, 15, 22, 22, 56, tzinfo=UTC)
FAILURE_AT = datetime(2026, 11, 15, 22, 24, 30, tzinfo=UTC)
QUEUED_AT = datetime(2026, 11, 15, 12, 49, 1, tzinfo=UTC)
LANE_WORDS = ("router", "congest", "headroom", "no extra worker")


@pytest.fixture()
def mounted_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and one mounted project, so nothing real is read."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("commit", "-q", "--allow-empty", "-m", "test: seed held-failure fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)

    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return home


def _lane_hold() -> dict[str, object]:
    """The hold an admission pass records while the router grants no slot."""
    return {
        "verdict": "held-for-allowance",
        "reason": ROUTER_FULL,
        "recorded_at": HOLD_RECORDED_AT.isoformat(),
    }


def _write_pointer(
    run_id: str,
    *,
    phase: str,
    failure: str | None = None,
    failure_at: datetime | None = None,
    queued_at: datetime | None = None,
    pass_hold: dict[str, object] | None = None,
) -> dict[str, object]:
    """Store one pointer in the mounted project, and nothing outside the temp home."""
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "launcher_host": "some-host.invalid",
        "node": {"id": "s21", "plan": PLAN},
    }
    if failure is not None:
        record["failure"] = failure
    if failure_at is not None:
        record["failure_at"] = failure_at.isoformat()
    if queued_at is not None:
        record["queued_at"] = queued_at.isoformat()
    if pass_hold is not None:
        record["pass_hold"] = dict(pass_hold)
        record["reason"] = str(pass_hold.get("reason") or "")
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _record_lane_hold_verdict(run_id: str) -> None:
    """Write the pass verdict a full lane leaves, so the stuck reading can fire."""
    path = runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": HOLD_RECORDED_AT.isoformat(),
        "admissible_age_seconds": None,
        "pointers": [{"run_id": run_id, "verdict": "held-for-allowance"}],
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _duties() -> list[dict[str, object]]:
    return obligations_module.obligations(PROJECT, SESSION)["obligations"]


def _duty(run_id: str) -> dict[str, object]:
    return next(item for item in _duties() if item["run_id"] == run_id)


def test_a_failed_launch_leaves_its_own_hold_with_its_remedy(
    mounted_home: Path,
) -> None:
    """The request that comes back to the queue carries why it left it."""
    snapshot = _write_pointer(
        RUN_ID,
        phase="starting",
        queued_at=QUEUED_AT,
        pass_hold=_lane_hold(),
    )

    queue_admission._record_queued_failure(
        snapshot, PLAN_REVIEW_FAILURE, moment=FAILURE_AT
    )

    pointer = runs.read_pointer(RUN_ID)
    hold = queue_admission.recorded_pass_hold(pointer)
    assert hold["verdict"] == "held-on-plan-review", hold
    assert PLAN_REVIEW_FAILURE in str(hold["reason"]), hold
    assert str(hold.get("remedy") or "") == REVIEW_COMMAND, hold
    assert str(hold.get("recorded_at") or "") == FAILURE_AT.isoformat(), hold
    # The lane's own sentence is gone from both the hold and the request.
    for word in LANE_WORDS:
        assert word not in json.dumps(hold), (word, hold)
        assert word not in str(pointer.get("reason") or ""), pointer["reason"]


def test_the_obligations_row_names_the_failure_and_its_command(
    mounted_home: Path,
) -> None:
    """The row a coordinator reads carries the finding, not the lane's figure."""
    _record_lane_hold_verdict(RUN_ID)
    _write_pointer(
        RUN_ID,
        phase="queued",
        failure=PLAN_REVIEW_FAILURE,
        failure_at=FAILURE_AT,
        queued_at=QUEUED_AT,
        pass_hold=_lane_hold(),
    )

    duty = _duty(RUN_ID)
    text = f"{duty.get('reason') or ''} {duty.get('next_command') or ''}"
    assert duty["kind"] == "queued-awaiting-session", duty
    assert PLAN_REVIEW_FAILURE in text, duty
    assert REVIEW_COMMAND in text, duty
    assert "stuck" not in text, duty
    for word in LANE_WORDS:
        assert word not in text, (word, duty)
    # The age is the wait the row's own reason describes, not the queue's age.
    assert duty["age_seconds"] == int((OBSERVED_AT - FAILURE_AT).total_seconds()), duty


def test_the_pass_verdict_names_the_failure_a_capacity_hold_would_hide(
    mounted_home: Path,
) -> None:
    """A pass that finds no slot still records the refusal that stopped the launch."""
    pointer = _write_pointer(
        RUN_ID,
        phase="queued",
        failure=PLAN_REVIEW_FAILURE,
        failure_at=FAILURE_AT,
        queued_at=QUEUED_AT,
        pass_hold=_lane_hold(),
    )

    row = queue_admission._verdict_row(
        pointer,
        allowance=0.0,
        allowance_source="congested",
        verdict="held-for-allowance",
    )

    assert row["verdict"] == "held-on-plan-review", row
    assert PLAN_REVIEW_FAILURE in str(row.get("detail") or ""), row
    assert str(row.get("remedy") or "") == REVIEW_COMMAND, row


def test_a_request_held_for_the_lane_is_a_wait_no_stop_blocks(
    mounted_home: Path,
) -> None:
    """A full lane is absorbed by the queue, so no surface reports it."""
    _write_pointer(
        LANE_RUN_ID,
        phase="queued",
        queued_at=OBSERVED_AT - timedelta(minutes=3),
        pass_hold=_lane_hold(),
    )

    duties = _duties()
    duty = next(item for item in duties if item["run_id"] == LANE_RUN_ID)
    # A positive control on the reading: the row is drawn, as a waiting row.
    assert duty["kind"] == "run-queued", duty
    assert hook._row_is_actionable(duty) is False, duty
    assert hook._blocking_items(duties) == [], duties
    text = f"{duty.get('reason') or ''} {duty.get('next_command') or ''}"
    for word in LANE_WORDS:
        assert word not in text, (word, duty)
    assert "help" not in text and "stuck" not in text, duty
