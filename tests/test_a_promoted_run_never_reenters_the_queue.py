"""Admission cannot republish a request that has left the live queue."""

# ruff: noqa: F811 - imported fixture is named by each test parameter

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import queue_admission, runs
from tests.test_a_queued_run_is_admitted import (
    PROJECT,
    _config_for,
    _queue,
    admit_repo,  # noqa: F401 - shared temporary crew home and checkout fixture
)


def _promote(repository: Path, run_id: str) -> Path:
    path = ledger.run_path(PROJECT, run_id, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "gate": "passed"}))
    assert runs._promoted_record_project(run_id, PROJECT) == PROJECT
    return path


@pytest.mark.parametrize("remove_pointer", [True, False])
def test_a_launch_failure_after_promotion_is_not_written_back(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch, remove_pointer: bool
) -> None:
    run_id = "r-promoted"
    runs.queue_dispatch(_queue(admit_repo, node_id="queued", run_id=run_id))
    pointer = runs.pointer_path(run_id)
    before = pointer.read_bytes()
    promotion = []

    def fail_after_promotion(_pointer, **_kwargs):
        promotion.append(_promote(admit_repo, run_id))
        if remove_pointer:
            pointer.unlink()
        raise RuntimeError("launch did not survive")

    monkeypatch.setattr(queue_admission, "_launch_queued", fail_after_promotion)
    _, resolve = _config_for()
    result = queue_admission.admit_once(config_for=resolve)

    assert len(promotion) == 1, "promotion must happen after the pass reads the queue"
    if remove_pointer:
        assert not pointer.exists(), "a promoted run was republished"
    else:
        assert pointer.read_bytes() == before, "a promoted pointer was rewritten"
    verdicts = [row for row in result["verdicts"] if row["run_id"] == run_id]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "refused"
    assert "promotion" in verdicts[0]["detail"]


@pytest.mark.parametrize("departure", ["removed", "promoted"])
def test_a_departed_failed_launch_is_not_requeued(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch, departure: str
) -> None:
    run_id = "r-launch-failed"
    request = _queue(admit_repo, node_id="queued", run_id=run_id)
    runs.queue_dispatch(request)
    runs._mutate_pointer(
        run_id,
        lambda current: {
            **current,
            "phase": "launch-failed",
            "admit_request": request,
            "launch_failures": [{"kind": "spawn-failed"}],
        },
    )
    original = runs.list_live
    reads = []

    def read_then_depart(*, project=None, phase=None):
        records = original(project=project, phase=phase)
        if phase == "launch-failed":
            assert [record["run_id"] for record in records] == [run_id]
            reads.append(run_id)
            if departure == "promoted":
                _promote(admit_repo, run_id)
            runs.pointer_path(run_id).unlink()
        return records

    monkeypatch.setattr(runs, "list_live", read_then_depart)
    _, resolve = _config_for()
    result = queue_admission.admit_once(config_for=resolve)

    assert reads == [run_id]
    assert not runs.pointer_path(run_id).exists(), "requeue recreated a removed pointer"
    assert result["requeued"] == []
    verdicts = [row for row in result["verdicts"] if row["run_id"] == run_id]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "refused"
    assert ("promotion" if departure == "promoted" else "pointer") in verdicts[0][
        "detail"
    ]


def test_a_launch_failure_for_a_queued_run_is_restored_by_the_pass(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-queued-failure"
    runs.queue_dispatch(_queue(admit_repo, node_id="queued", run_id=run_id))

    def fail(_pointer, **_kwargs):
        raise RuntimeError("launch did not survive")

    monkeypatch.setattr(queue_admission, "_launch_queued", fail)
    _, resolve = _config_for()
    result = queue_admission.admit_once(config_for=resolve)

    restored = runs.read_pointer(run_id)
    assert restored["phase"] == "queued"
    assert restored["failure"] == "RuntimeError: launch did not survive"
    assert restored["failure_attempts"] == 1
    assert restored["node"]["id"] == "queued"
    assert not [
        row
        for row in result["verdicts"]
        if "writeback refused" in row.get("detail", "")
    ]


def test_a_launch_failure_still_in_the_queue_is_restored(admit_repo: Path) -> None:
    run_id = "r-still-queued"
    request = _queue(admit_repo, node_id="queued", run_id=run_id)
    runs.queue_dispatch(request)

    queue_admission._restore_queued(request, "launch did not survive")

    restored = runs.read_pointer(run_id)
    assert restored["phase"] == "queued"
    assert restored["failure"] == "launch did not survive"
    assert restored["failure_attempts"] == 1
    assert restored["node"] == request["node"]
    assert restored["retry_at"] > restored["failure_at"]
