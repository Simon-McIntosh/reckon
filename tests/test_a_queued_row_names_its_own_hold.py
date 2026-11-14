"""A queued row names the hold it waits on, never another run's command.

A queued request's duty is read from the refusal its last admission attempt
stored on the pointer. That refusal is a fact about a moment, and nothing that
built the row could tell a condition still standing from one the admission pass
had since answered. The same code also published a text-searched command for a
scope claim, which addresses the run holding the path rather than the request,
so a coordinator was sent to act on a peer's claim and the stop hook refused the
turn until it did.

Three readings are pinned here. A request refused on a live run's write claim
reads ``scope-held``, naming the holder and the path, with no command. No
``queued-awaiting-session`` row carries a command that names another run. A
refusal recorded before the pointer's current wait hold is not a session duty at
all: the request reads as queued at its own place and the hook does not hold a
stop for it. A session duty recorded after that hold still reads as one, so the
comparison is shown to discriminate rather than to hide every duty. The two
mutations the manifest names make the second and third cases above fail.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligations as obligations_module
from reckon.crew import recovery, runs
from reckon.crew.node import ScopeConflict
from reckon.hooks import coordinator_obligations as hook

PROJECT = "queued-hold-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
CLAIMED_PATH = "scripts/iter/fix.py"
PEER_RUN = "r-peer-holder"
CLAIM_RUN = "r-claim-held"
SESSION_RUN = "r-session-held"
STALE_RUN = "r-stale-refusal"
STALE_FAILURE = "WatcherRequired: the dispatching session has no follower"


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
        ("commit", "-q", "--allow-empty", "-m", "test: seed queued-hold fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)

    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)


def _write_pointer(
    run_id: str,
    *,
    phase: str,
    failure: str | None = None,
    failure_at: datetime | None = None,
    queued_at: datetime | None = None,
    pass_hold: dict[str, object] | None = None,
    process_alive: bool = False,
) -> dict[str, object]:
    """Store one pointer in the mounted project, and nothing outside the temp home."""
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "process_alive": process_alive,
        "launcher_host": "some-host.invalid",
    }
    if failure is not None:
        record["failure"] = failure
    if failure_at is not None:
        record["failure_at"] = failure_at.isoformat()
    if queued_at is not None:
        record["queued_at"] = queued_at.isoformat()
    if pass_hold is not None:
        record["pass_hold"] = pass_hold
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _classify(record: dict[str, object]) -> dict[str, object]:
    return recovery.classify_pointer(record, now_seconds=OBSERVED_AT.timestamp())


def _duties() -> list[dict[str, object]]:
    return obligations_module.obligations(PROJECT, SESSION)["obligations"]


def _duty(run_id: str) -> dict[str, object]:
    return next(item for item in _duties() if item["run_id"] == run_id)


def _claim_text() -> str:
    claim = ScopeConflict(
        run_id=PEER_RUN,
        node_id="peer-node",
        candidate_path=CLAIMED_PATH,
        claimed_path=CLAIMED_PATH,
    )
    return f"ScopeConflict: {claim}"


def test_a_scope_claim_reads_scope_held(mounted_home: Path) -> None:
    _write_pointer(PEER_RUN, phase="working", process_alive=True)
    record = _write_pointer(
        CLAIM_RUN,
        phase="queued",
        failure=_claim_text(),
        failure_at=OBSERVED_AT - timedelta(minutes=30),
        queued_at=OBSERVED_AT - timedelta(hours=1),
    )
    row = _classify(record)
    assert row["classification"] == "queued", row
    refusal = row["session_refusal"]
    assert isinstance(refusal, dict), row
    assert refusal["next_command"] == "", refusal
    assert refusal["holder_run_id"] == PEER_RUN, refusal
    assert refusal["held_path"] == CLAIMED_PATH, refusal
    assert refusal["recorded_at"] == (OBSERVED_AT - timedelta(minutes=30)).isoformat()

    duty = _duty(CLAIM_RUN)
    assert duty["kind"] == obligations_module.SCOPE_HELD_DUTY_KIND
    assert duty.get("next_command", "") == "", duty
    assert duty.get("holder_run_id") == PEER_RUN, duty
    assert duty.get("held_path") == CLAIMED_PATH, duty
    assert (
        obligations_module.SCOPE_HELD_DUTY_KIND
        in obligations_module.NON_BLOCKING_DUTY_KINDS
    )
    assert "scope-held" in hook._NON_BLOCKING_KINDS


def test_no_queued_awaiting_session_row_names_another_run(
    mounted_home: Path,
) -> None:
    _write_pointer(PEER_RUN, phase="working", process_alive=True)
    _write_pointer(
        CLAIM_RUN,
        phase="queued",
        failure=_claim_text(),
        failure_at=OBSERVED_AT - timedelta(minutes=30),
        queued_at=OBSERVED_AT - timedelta(hours=1),
    )
    _write_pointer(
        SESSION_RUN,
        phase="queued",
        failure=STALE_FAILURE,
        failure_at=OBSERVED_AT - timedelta(minutes=5),
        queued_at=OBSERVED_AT - timedelta(minutes=10),
    )
    duties = _duties()
    kinds = {str(item["run_id"]): str(item["kind"]) for item in duties}
    # Positive control: the session-subject refusal is still a named duty, so
    # the rule below is not passing because no such row exists.
    assert kinds[SESSION_RUN] == "queued-awaiting-session", kinds
    assert kinds[CLAIM_RUN] == obligations_module.SCOPE_HELD_DUTY_KIND, kinds
    run_ids = {PEER_RUN, CLAIM_RUN, SESSION_RUN}
    offenders = [
        (str(item["run_id"]), str(item["next_command"]))
        for item in duties
        if str(item["kind"]) == "queued-awaiting-session"
        if any(
            other != str(item["run_id"]) and other in str(item["next_command"])
            for other in run_ids
        )
    ]
    assert offenders == [], offenders


def _open_hold(recorded_at: datetime) -> dict[str, object]:
    return {
        "verdict": "held-for-allowance",
        "reason": "the lane is at its concurrent request ceiling",
        "recorded_at": recorded_at.isoformat(),
    }


def test_a_refusal_older_than_an_open_hold_is_not_a_session_duty(
    mounted_home: Path,
) -> None:
    record = _write_pointer(
        STALE_RUN,
        phase="queued",
        failure=STALE_FAILURE,
        failure_at=OBSERVED_AT - timedelta(hours=1),
        queued_at=OBSERVED_AT - timedelta(hours=2),
        pass_hold=_open_hold(OBSERVED_AT - timedelta(minutes=5)),
    )
    row = _classify(record)
    assert row["classification"] == "queued", row
    assert "session_refusal" not in row, row

    duty = _duty(STALE_RUN)
    assert duty["kind"] == obligations_module.QUEUED_RUN_DUTY_KIND, duty
    assert duty["position"] == 1, duty
    assert "held-for-allowance" in str(duty["next_command"]), duty
    assert hook._row_is_actionable(duty) is False, duty
    assert hook._blocking_items([duty]) == [], duty


def test_a_refusal_recorded_after_the_hold_is_still_a_session_duty(
    mounted_home: Path,
) -> None:
    _write_pointer(
        SESSION_RUN,
        phase="queued",
        failure=STALE_FAILURE,
        failure_at=OBSERVED_AT - timedelta(minutes=2),
        queued_at=OBSERVED_AT - timedelta(minutes=5),
        pass_hold=_open_hold(OBSERVED_AT - timedelta(hours=1)),
    )
    duty = _duty(SESSION_RUN)
    assert duty["kind"] == "queued-awaiting-session", duty
