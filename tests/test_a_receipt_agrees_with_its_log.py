"""A gate receipt's failure figures must agree with the log it cites.

The receipt is the manifest's ``after_suite`` arm: the exit status, the failure
count and the failure ids a worker asserted for one gate. The log is the
evidence. A log holding two runs makes the comparison worth making, because a
receipt copied from the first run agrees with the last run's exit status while
naming the other run's failures.

Each case below is one shape the guard meets, over a temporary log and a
temporary manifest: a stale receipt refused naming its count and ids, an honest
receipt accepted, a bare receipt id covering a qualified log id, an arm stating
no count judged on status alone, an error counted as a failure, an arm
describing another log left alone, a log with no exit record refused, a pytest
gate that never reached its summary refused, and a ctest log whose failures and
count are both read from its FAILED block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion_evidence import _require_gate_log_agrees

PYTEST_COMMAND = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_x.py"
)
CTEST_COMMAND = "ctest --output-on-failure"

# Two runs captured into one log: the first fails two tests and exits 1, the
# second passes everything and exits 0. The tail's status belongs to the second
# run, so a receipt carrying the first run's figures describes a run the status
# line next to it does not belong to.
TWO_RUNS = """\
FAILED tests/test_x.py::test_one
FAILED tests/test_x.py::test_two
1 passed, 2 failed in 0.20s
EXIT=1
3 passed in 0.10s
EXIT=0
"""

ONE_FAILURE = """\
FAILED tests/test_x.py::test_one
2 passed, 1 failed in 0.20s
EXIT=1
"""

ONE_FAILURE_AND_ONE_ERROR = """\
FAILED tests/test_x.py::test_a
ERROR tests/test_x.py::test_b
1 passed, 1 failed, 1 error in 0.30s
EXIT=1
"""

NO_EXIT_RECORD = """\
3 passed in 0.10s
"""

UNFINISHED_PYTEST = """\
3 passed in 0.10s
EXIT=0
collecting ...
EXIT=0
"""

CTEST_LOG = """\
Test project /build
    Start 1: SOLVER/BASIC
1/2 Test #1: SOLVER/BASIC ....................   Passed    0.31 sec
    Start 2: SOLVER/XPOINT
2/2 Test #2: SOLVER/XPOINT ...***Failed    1.02 sec

The following tests FAILED:
\t  2 - SOLVER/XPOINT (Failed)
Errors while running CTest
EXIT=1
"""


def _guard(
    tmp_path: Path,
    log_text: str,
    *,
    arm: dict | None = None,
    exit_status: int = 0,
    command: str = PYTEST_COMMAND,
) -> dict:
    """Run the guard over a temporary log and manifest, returning its record.

    The manifest carries only what the guard reads: a recognised status and the
    ``after_suite`` arm. ``arm is None`` writes a manifest recording no arm, the
    shape a report-only run delivers.
    """
    log = tmp_path / "gate.log"
    log.write_text(log_text, encoding="utf-8")
    lines = ["status: complete", f"tests: {command}"]
    if arm is not None:
        lines.insert(1, f"after_suite: {json.dumps(arm)}")
    manifest = tmp_path / "manifest.md"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    record = {"manifest_path": str(manifest)}
    gate = {"log_path": str(log), "exit_status": exit_status, "command": command}
    _require_gate_log_agrees("r-receipt", gate, record, verdict="passed")
    return record


def _arm_for(
    tmp_path: Path,
    *,
    log_name: str = "gate.log",
    exit_status: int = 0,
    **fields: object,
) -> dict:
    return {
        "log_path": str(tmp_path / log_name),
        "exit_status": exit_status,
        "completed": True,
        **fields,
    }


def test_a_receipt_copied_from_the_first_run_is_refused_naming_count_and_ids(
    tmp_path: Path,
) -> None:
    arm = _arm_for(
        tmp_path,
        failure_count=2,
        failure_ids=["tests/test_x.py::test_one", "tests/test_x.py::test_two"],
    )
    with pytest.raises(CrewError) as refusal:
        _guard(tmp_path, TWO_RUNS, arm=arm)
    message = str(refusal.value)
    assert "failure_count records 2" in message
    assert "tests/test_x.py::test_one" in message
    assert "tests/test_x.py::test_two" in message


def test_an_honest_receipt_for_the_same_log_is_accepted(tmp_path: Path) -> None:
    arm = _arm_for(tmp_path, failure_count=0, failure_ids=[])
    _guard(tmp_path, TWO_RUNS, arm=arm)


def test_a_bare_receipt_id_covers_the_logs_qualified_id(tmp_path: Path) -> None:
    arm = _arm_for(tmp_path, exit_status=1, failure_count=1, failure_ids=["test_one"])
    _guard(tmp_path, ONE_FAILURE, arm=arm, exit_status=1)


def test_an_arm_recording_no_count_is_judged_on_exit_status_only(
    tmp_path: Path,
) -> None:
    arm = _arm_for(tmp_path, failure_ids=[])
    _guard(tmp_path, TWO_RUNS, arm=arm)


def test_an_error_is_counted_with_the_failures(tmp_path: Path) -> None:
    arm = _arm_for(
        tmp_path,
        exit_status=1,
        failure_count=2,
        failure_ids=["tests/test_x.py::test_a", "tests/test_x.py::test_b"],
    )
    _guard(tmp_path, ONE_FAILURE_AND_ONE_ERROR, arm=arm, exit_status=1)


def test_an_arm_describing_another_log_is_not_compared(tmp_path: Path) -> None:
    (tmp_path / "other.log").write_text(ONE_FAILURE, encoding="utf-8")
    arm = _arm_for(
        tmp_path,
        log_name="other.log",
        exit_status=1,
        failure_count=7,
        failure_ids=["tests/test_x.py::somewhere_else"],
    )
    _guard(tmp_path, TWO_RUNS, arm=arm)


def test_a_log_with_no_exit_record_is_refused(tmp_path: Path) -> None:
    arm = _arm_for(tmp_path, failure_count=0, failure_ids=[])
    with pytest.raises(CrewError) as refusal:
        _guard(tmp_path, NO_EXIT_RECORD, arm=arm)
    assert "no terminal EXIT=" in str(refusal.value)


def test_a_pytest_gate_whose_last_run_reached_no_summary_is_refused(
    tmp_path: Path,
) -> None:
    arm = _arm_for(tmp_path, failure_count=0, failure_ids=[])
    with pytest.raises(CrewError) as refusal:
        _guard(tmp_path, UNFINISHED_PYTEST, arm=arm)
    assert "reaches no pytest summary" in str(refusal.value)


def test_a_ctest_logs_count_is_read_from_its_failed_block(tmp_path: Path) -> None:
    arm = _arm_for(
        tmp_path,
        exit_status=1,
        failure_count=1,
        failure_ids=["SOLVER/XPOINT"],
    )
    _guard(
        tmp_path,
        CTEST_LOG,
        arm=arm,
        exit_status=1,
        command=CTEST_COMMAND,
    )


def test_a_ctest_arm_naming_another_test_is_refused(tmp_path: Path) -> None:
    arm = _arm_for(
        tmp_path,
        exit_status=1,
        failure_count=1,
        failure_ids=["SOLVER/SOMETHING_ELSE"],
    )
    with pytest.raises(CrewError) as refusal:
        _guard(tmp_path, CTEST_LOG, arm=arm, exit_status=1, command=CTEST_COMMAND)
    assert "SOLVER/SOMETHING_ELSE" in str(refusal.value)
