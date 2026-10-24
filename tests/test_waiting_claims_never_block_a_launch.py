"""Requests without launched workers leave their paths to launch-time admission."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_claims import refuse_widen_scope_conflicts
from tests import test_overlapping_queued_requests_launch_in_turn as queue_cases


@pytest.mark.parametrize("phase", ["awaiting-upstream", "held"])
def test_ready_dispatch_launches_across_a_waiting_claim(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    queue_cases._configure(dispatch_repo, monkeypatch, session_slots=4)
    queue_cases._live(
        dispatch_repo, "r-waiting", phase=phase, write_paths=[queue_cases.SHARED]
    )

    result = queue_cases._dispatch(dispatch_repo, "ready", paths=(queue_cases.SHARED,))

    assert result.exit_code == 0, result.output
    assert queue_cases._payload(result)["phase"] == "starting"
    assert runs.read_pointer("r-waiting")["phase"] == phase


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return queue_cases.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return queue_cases.admit_repo.__wrapped__(tmp_path, monkeypatch)


def test_working_run_can_widen_across_a_waiting_claim(
    dispatch_repo: Path,
) -> None:
    queue_cases._live(
        dispatch_repo,
        "r-waiting",
        phase="awaiting-upstream",
        write_paths=[queue_cases.SHARED],
    )
    queue_cases._live(
        dispatch_repo, "r-working", phase="working", write_paths=["own.py"]
    )

    refuse_widen_scope_conflicts(runs.read_pointer("r-working"), [queue_cases.SHARED])


def test_working_claim_still_refuses_a_dispatch(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_cases._configure(dispatch_repo, monkeypatch, session_slots=4)
    queue_cases._live(
        dispatch_repo, "r-working", phase="working", write_paths=[queue_cases.SHARED]
    )

    result = queue_cases._dispatch(dispatch_repo, "ready", paths=(queue_cases.SHARED,))

    assert result.exit_code == 7, result.output
    assert queue_cases._payload(result)["error"] == "scope-conflict"


def test_ready_waiter_stays_queued_while_the_launched_claim_holds_its_path(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = queue_cases._lane_document(
        tmp_path, slots={queue_cases.SESSION_A: 1, queue_cases.SESSION_B: 1}
    )
    config = queue_cases._config(lane)
    waiting = queue_cases._queue(
        admit_repo,
        node_id="waiting",
        run_id="r-waiting",
        session=queue_cases.SESSION_B,
        write_paths=[queue_cases.SHARED],
        queued_at="2026-10-14T11:59:00+00:00",
    )
    runs.queue_dispatch(waiting)
    waiting = runs.read_pointer("r-waiting")
    waiting["phase"] = "awaiting-upstream"
    runs._write_json(runs.pointer_path("r-waiting"), waiting)
    queue_cases._live(
        admit_repo,
        "r-launched",
        phase="working",
        write_paths=[queue_cases.SHARED],
    )

    # Readiness moves the request into the queue; its worker still needs the
    # ordinary launch-time claim check against the run already writing there.
    pointer = runs.read_pointer("r-waiting")
    pointer["phase"] = "queued"
    runs._write_json(runs.pointer_path("r-waiting"), pointer)
    result = queue_admission.admit_once(
        now=queue_cases.START + timedelta(minutes=1),
        config_for=lambda _pointer: config,
        launcher=queue_cases._launcher([]),
    )

    assert result["launched"] == []
    assert runs.read_pointer("r-waiting")["phase"] == "queued"
    assert runs.read_pointer("r-waiting").get("failure")
