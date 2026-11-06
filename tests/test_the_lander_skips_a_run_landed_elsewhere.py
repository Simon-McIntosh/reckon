"""A landing pass recognizes a run another actor has already landed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import landing_measure, queue_admission, queue_landing, runs
from reckon.crew.node import CrewError
from tests.test_the_landing_pass_measures_and_lands import (
    PROJECT,
    RUN,
    _accept,
)
from tests.test_the_landing_pass_measures_and_lands import (
    delivery as imported_delivery,
)

delivery_case = imported_delivery


def _measurement(head: str) -> landing_measure.Measurement:
    return landing_measure.Measurement(
        command="true", suite={}, population={}, head=head, project=PROJECT
    )


def test_removed_pointer_is_recorded_in_landing_stage(
    delivery_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery_case["base"]), head=str(delivery_case["head"]))

    def measure(_pointer, run_id, head, _tree, _stored):
        runs.pointer_path(run_id).unlink()
        return _measurement(head)

    monkeypatch.setattr(queue_landing, "_measure_delivery", measure)

    def stale_promotion(run_id):
        raise CrewError(f"no live run {run_id!r}")

    monkeypatch.setattr(queue_landing, "_land", stale_promotion)
    worker = queue_admission._LandingWorker(queue_landing.land_once)
    worker.tick()
    landing = worker.settle()

    assert "error" not in landing, landing
    assert landing["skipped"] == [{"run_id": RUN, "status": "landed-elsewhere"}]
    assert landing["landed"] == []


def test_ledger_row_skips_measurement_with_surviving_pointer(
    delivery_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery_case["base"]), head=str(delivery_case["head"]))
    repo = Path(delivery_case["repo"])
    real_conflicts = queue_landing._merge_conflicts

    def finish_elsewhere(pointer, head):
        conflicts = real_conflicts(pointer, head)
        row = ledger.run_path(PROJECT, RUN, repo)
        row.parent.mkdir(parents=True, exist_ok=True)
        row.write_text(json.dumps({"run_id": RUN, "promoted_revision": head}))
        return conflicts

    calls: list[str] = []

    def measure(**_kwargs):
        calls.append("measure_run")
        return _measurement(str(delivery_case["head"]))

    monkeypatch.setattr(queue_landing, "_merge_conflicts", finish_elsewhere)
    monkeypatch.setattr(landing_measure, "measure_run", measure)
    monkeypatch.setattr(queue_landing, "_land", lambda _run_id: (True, ""))

    landing = queue_landing.land_once()

    assert runs.pointer_path(RUN).exists()
    assert calls == []
    assert landing["skipped"] == [{"run_id": RUN, "status": "landed-elsewhere"}]


def test_pointer_removed_while_recording_attempt_is_skipped(
    delivery_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept(base=str(delivery_case["base"]), head=str(delivery_case["head"]))
    monkeypatch.setattr(
        queue_landing,
        "_measure_delivery",
        lambda _pointer, _run_id, head, _tree, _stored: _measurement(head),
    )
    original_record = queue_landing._record_attempt

    def record(run_id, **kwargs):
        if kwargs["status"] == "attempting":
            runs.pointer_path(run_id).unlink()
            raise CrewError(f"no live run {run_id!r}")
        return original_record(run_id, **kwargs)

    monkeypatch.setattr(queue_landing, "_record_attempt", record)
    worker = queue_admission._LandingWorker(queue_landing.land_once)
    worker.tick()
    landing = worker.settle()

    assert "error" not in landing, landing
    assert landing["skipped"] == [{"run_id": RUN, "status": "landed-elsewhere"}]
