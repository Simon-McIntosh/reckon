"""Requests without launched workers leave their paths to launch-time admission."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_claims import (
    _repository_scope_claims,
    refuse_widen_scope_conflicts,
)
from tests import test_a_queued_request_waits_on_its_dependencies as dependency_cases
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
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return dependency_cases.chain_repo.__wrapped__(tmp_path, monkeypatch)


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


@pytest.mark.parametrize("phase", ["queued", "awaiting-upstream", "held"])
def test_scope_registry_agrees_with_dispatch_for_waiting_requests(
    dispatch_repo: Path, phase: str
) -> None:
    queue_cases._live(
        dispatch_repo,
        "r-waiting",
        phase=phase,
        write_paths=[queue_cases.SHARED],
    )

    dispatch_claim = next(
        claim
        for claim in _repository_scope_claims(
            candidate_paths=[dispatch_repo / queue_cases.SHARED],
            candidate_repositories=[dispatch_repo],
        )
        if claim.run_id == "r-waiting"
    )
    registry_claim = next(
        claim
        for claim in runs.scope_claims(queue_cases.PROJECT, dispatch_repo)
        if claim["run_id"] == "r-waiting"
    )

    assert dispatch_claim.binding is False
    assert registry_claim["binding"] is False
    assert dispatch_claim.disposition_reason == registry_claim["disposition_reason"]


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
    chain_repo: Path,
) -> None:
    waiting = dependency_cases._node("waiting", "s2")
    waiting.write_paths = [queue_cases.SHARED]
    stored = queue_admission.queue_requests(
        [waiting],
        project=dependency_cases.PROJECT,
        session=dependency_cases.SESSION,
        repo_root=chain_repo,
        config=dependency_cases.CONFIG,
        run_ids={"waiting": "r-waiting"},
    )
    assert stored["r-waiting"]["phase"] == "awaiting-upstream"
    queue_cases._live(
        chain_repo,
        "r-launched",
        phase="working",
        write_paths=[queue_cases.SHARED],
    )

    dependency_cases._declare(chain_repo, "s1")
    result = queue_admission.admit_once(
        config_for=dependency_cases._config_for(),
        launcher=queue_cases._launcher([]),
    )

    assert result["launched"] == []
    pointer = runs.read_pointer("r-waiting")
    assert pointer["phase"] == "queued"
    # The path is held by a binding claim, so the pass holds the request before
    # spending a dispatch on it rather than attempting and recording a refusal.
    assert not pointer.get("failure")
    verdict = {row["run_id"]: row for row in result["verdicts"]}["r-waiting"]
    assert verdict["verdict"] == "held-on-claim"
    assert "r-launched" in verdict["detail"]
    assert queue_cases.SHARED in verdict["detail"]
