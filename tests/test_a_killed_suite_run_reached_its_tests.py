import pytest

from reckon.crew import standing_suite


@pytest.mark.parametrize(
    ("log", "exit_status", "failed", "collected"),
    [
        ("tests/test_sample.py .... [ 76%]\nEXIT=-9\n", -9, False, None),
        ("tests/test_sample.py [gw0] [ 76%]\nEXIT=-9\n", -9, False, None),
        ("[ 76%]\nERROR collecting tests/test_sample.py\nEXIT=2\n", 2, True, 0),
        ("runner stopped before output\nEXIT=-9\n", -9, True, 0),
    ],
)
def test_progress_proves_the_test_phase_was_reached(
    log: str, exit_status: int, failed: bool, collected: int | None
) -> None:
    assert standing_suite._exit_status(log) == exit_status
    counts = standing_suite._build_counts(log, exit_status)
    assert counts["collection_failed"] is failed
    assert counts["collected"] == collected


def test_a_budget_stopped_run_holds_only_for_overrun(tmp_path) -> None:
    log = "tests/test_sample.py .... [ 76%]\nEXIT=-9\n"
    counts = standing_suite._build_counts(log, -9)
    record = {
        **counts,
        "kind": "run",
        "revision": "known-head",
        "observed_at": "2026-10-16T00:00:00Z",
        "over_budget": True,
    }
    standing_suite.record(tmp_path, "project", record)
    reason = standing_suite.hold_reason(tmp_path, "none", "project")
    assert reason is not None
    assert "overran its budget" in reason
    assert "failed to collect" not in reason
