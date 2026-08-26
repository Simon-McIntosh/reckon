from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion
from reckon.crew.node import CrewError


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / "proj").mkdir(parents=True)
    return root


def test_failing_promotion_requires_a_closed_failure_classification() -> None:
    with pytest.raises(CrewError) as error:
        promotion.complete("run-a", gate="failed", outcome="the check failed")

    message = str(error.value)
    assert "--failure-classification" in message
    assert all(value in message for value in ledger.FAILURE_CLASSIFICATIONS)


def test_worker_pass_rate_uses_only_work_rejected_failures(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    records = [
        ledger.build_record(run_id=f"pass-{index}", plan="work", gate="passed")
        for index in range(8)
    ]
    records.extend(
        ledger.build_record(
            run_id=f"rejected-{index}",
            plan="work",
            gate="failed",
            failure_classification="work-rejected",
        )
        for index in range(2)
    )
    records.extend(
        ledger.build_record(
            run_id=f"excluded-{index}",
            plan="work",
            gate="failed",
            failure_classification=classification,
        )
        for index, classification in enumerate(ledger.FAILURE_CLASSIFICATIONS[1:])
    )
    for record in records:
        ledger.append_run("proj", record, root=root)

    stored = ledger.runs("proj", root=root)
    report = ledger.summary("proj", root=root)["worker_gate"]

    assert stored[8]["failure_classification"] == "work-rejected"
    assert report["passed"] == 8
    assert report["work_rejected"] == 2
    assert report["pass_rate"] == 0.8
    assert report["excluded"] == {
        classification: 1 for classification in ledger.FAILURE_CLASSIFICATIONS[1:]
    }
    assert report["unclassified"] == 0
