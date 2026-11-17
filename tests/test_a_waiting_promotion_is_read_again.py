"""Waiting promotions re-read repaired manifests and surface overdue conditions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main
from reckon.crew import promotion_requests as requests
from reckon.crew import recovery_review_acceptance as acceptance
from tests.test_review_promotion_waits_for_its_record import (
    PROJECT,
    _store_record,
    _write_exit_record,
    _write_pointer,
)
from tests.test_review_promotion_waits_for_its_record import (
    repository as review_repository,
)

repository = review_repository

# Captured request data; project and session bindings are replaced by the fixture.
WAITING_RECORDS = [
    {
        "checked_at": "2026-11-16T21:20:35.414952+00:00",
        "kind": "review-self",
        "node": "review-of-vofd-design-report-127aab134a7f",
        "ok": True,
        "options": {"config": None},
        "phase": "promotion-waiting",
        "plan": "",
        "previous_phase": "working",
        "project": "power_convertor",
        "request_id": "414bd6fe5fba42fcae681afd6ed1622d",
        "requested_at": "2026-11-16T21:19:56.762836+00:00",
        "run_id": "r-20261116T211234648844-review-of-vofd-design-report-127aab134a7f",
        "session": "c71ae21b-bd1a-4f28-a356-27ade6673050",
        "waiting_reason": "the review run's manifest does not read complete, so its verdict has not been written",
    },
    {
        "checked_at": "2026-11-16T21:20:36.539793+00:00",
        "kind": "review-self",
        "node": "review-of-vofd-design-report-127aab134a7f-figure",
        "ok": True,
        "options": {"config": None},
        "phase": "promotion-waiting",
        "plan": "",
        "previous_phase": "working",
        "project": "power_convertor",
        "request_id": "dc4fe49a5f4b4447b3a3b535e3ebc75d",
        "requested_at": "2026-11-16T21:19:56.775368+00:00",
        "run_id": "r-20261116T211733528569-review-of-vofd-design-report-127aab134a7f-figure",
        "session": "c71ae21b-bd1a-4f28-a356-27ade6673050",
        "waiting_reason": "the review run's manifest does not read complete, so its verdict has not been written",
    },
]
SESSION = "waiting-request-session"


def _park(repository: Path, tmp_path: Path, record: dict) -> tuple[dict, Path]:
    run_id = record["run_id"]
    reviewed = "r-20261116T090000000001-reviewed"
    _write_pointer(repository, tmp_path, run_id, reviewed, pid=None)
    _store_record(run_id, reviewed)
    _write_exit_record(run_id)
    pointer = requests.runs.read_pointer(run_id)
    pointer["session"] = SESSION
    requests.runs._write_json(requests.runs.pointer_path(run_id), pointer)
    manifest = Path(pointer["manifest_path"])
    manifest.write_text("status: in-progress\n")
    waiting = {**record, "project": PROJECT, "session": SESSION}
    requests.runs._write_json(requests._request_path(run_id), waiting)
    state, reason, _ = acceptance.review_self_state(pointer)
    assert state == acceptance.REVIEW_SELF_WAITING
    assert reason == record["waiting_reason"]
    return waiting, manifest


def test_a_drain_promotes_both_repaired_waiting_requests(repository, tmp_path):
    for original in WAITING_RECORDS:
        request, manifest = _park(repository, tmp_path, original)
        manifest.write_text("status: complete\n")
        repaired_at = datetime.fromtimestamp(manifest.stat().st_mtime, UTC)
        service = requests.PromotionService(width=1)
        assert service.tick() == [request["run_id"]]
        service.settle(timeout=30)
        receipt = requests.read_promotion(request["run_id"])
        print(
            f"{request['run_id']}: phase={receipt['phase']} checked_at={receipt['checked_at']}"
        )
        assert receipt["phase"] == requests.PROMOTED, receipt
        assert datetime.fromisoformat(receipt["checked_at"]) > repaired_at, receipt
        assert receipt["request_id"] == original["request_id"]
        assert not requests.runs.pointer_path(request["run_id"]).exists()
        assert any(
            row["run_id"] == request["run_id"]
            for row in ledger.runs(PROJECT, root=repository)
        )


def test_repeated_complete_refreshes_the_wait_and_promotes_after_repair(
    repository, tmp_path
):
    request, manifest = _park(repository, tmp_path, WAITING_RECORDS[0])
    arguments = ["crew", "complete", "--run", request["run_id"], "--gate", "not-run"]
    first = CliRunner().invoke(main, arguments)
    assert first.exit_code == 0, first.output
    waiting = requests.read_promotion(request["run_id"])
    print(
        f"repeat complete: checked_at={waiting['checked_at']} reason={waiting['waiting_reason']}"
    )
    assert waiting["phase"] == requests.WAITING
    assert waiting["waiting_reason"] == request["waiting_reason"]
    assert datetime.fromisoformat(waiting["checked_at"]) > datetime.fromisoformat(
        request["checked_at"]
    )
    assert waiting["request_id"] == request["request_id"]
    assert not ledger.runs(PROJECT, root=repository)
    manifest.write_text("status: complete\n")
    second = CliRunner().invoke(main, arguments)
    assert second.exit_code == 0, second.output
    assert not requests.runs.pointer_path(request["run_id"]).exists(), second.output
    assert any(
        row["run_id"] == request["run_id"]
        for row in ledger.runs(PROJECT, root=repository)
    )


def test_an_overdue_wait_names_its_condition_and_last_check(repository, tmp_path):
    request, _ = _park(repository, tmp_path, WAITING_RECORDS[0])
    # The bound is measured from the first wait, so later checks cannot postpone it.
    start = datetime.fromisoformat(request["requested_at"])
    boundary = start + timedelta(seconds=1800)
    assert requests.failure_obligations(PROJECT, SESSION, now=boundary) == []
    service = requests.PromotionService(width=1)
    assert service.tick() == []
    waiting = requests.read_promotion(request["run_id"])
    rows = requests.failure_obligations(
        PROJECT, SESSION, now=boundary + timedelta(seconds=1)
    )
    assert len(rows) == 1, rows
    [row] = rows
    assert row["kind"] == requests.WAITING
    assert row["run_id"] == request["run_id"]
    assert row["checked_at"] == waiting["checked_at"]
    assert request["waiting_reason"] in row["reason"]
    assert waiting["checked_at"] in row["reason"]
    assert (
        requests.failure_obligations(
            PROJECT, "other-session", now=boundary + timedelta(seconds=1)
        )
        == []
    )
    assert (
        requests.failure_obligations(
            "other-project", SESSION, now=boundary + timedelta(seconds=1)
        )
        == []
    )
