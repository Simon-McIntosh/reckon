"""A ctest receipt's failure count is read in the log's own format.

The receipt is a suite arm's ``failure_count``; the log is the evidence. A
pytest log states its totals on a summary line, which is what the count was
always read from. A ctest log prints no such line: its last line carries a
percentage instead, while the test programs it ran print counts of their own,
``0 failed`` among them. Reading a ctest log with the pytest reader therefore
takes a program's line for the suite's total and reports zero, refusing an
honest receipt that records the 22 tests ctest itself listed as failed.

Each case below is one shape the reader meets, over a temporary log and the arm
naming it: a ctest log whose programs print ``0 failed`` and whose FAILED block
lists 22 entries agreeing with a receipt of 22, the same log disagreeing with a
receipt of 21, a pytest log still read from its summary line, and a log in
neither format raising no count mismatch at all.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.promotion_evidence import _receipt_mismatches

CTEST_COMMAND = "ctest --output-on-failure -L VALIDATION"

# The body of a failing ctest run as its test programs write it: each case
# prints its own slice counts, and every one of them says ``0 failed``. A
# reader that matches ``<n> failed`` takes the last of these for the suite's
# total, which is why the count came back as zero.
_CTEST_PROGRAM_OUTPUT = """\
      Start  1: validate/ITER/imas/dina3420
  1/59 Test  #1: validate/ITER/imas/dina3420 .....   Passed    0.51 sec
      Start  2: validate/TCV/imas/liuqe
  2/59 Test  #2: validate/TCV/imas/liuqe .........***Failed    1.02 sec
Total: 3 slices — 3 converged, 0 vacuum, 0 failed
      Start  3: validate/MASTU/imas/magnetics
  3/59 Test  #3: validate/MASTU/imas/magnetics ...***Failed    0.88 sec
Total: 5 slices — 5 converged, 0 vacuum, 0 failed
"""

# The 22 tests ctest listed as failed, in the block its summary follows.
_CTEST_FAILED_NAMES = [
    "validate/ITER/imas/nice-58072",
    "validate/ITER/imas/nice-63159",
    "validate/ITER/imas/nice-62751",
    "validate/WEST/imas/nice-58072",
    "validate/WEST/imas/nice-63159",
    "validate/WEST/imas/nice-62751",
    "validate/WEST/imas/nice-58072__dd3.37.0",
    "validate/WEST/imas/nice-63159__dd3.42.0",
    "validate/WEST/imas/nice-62751__dd3.42.0",
    "validate/WEST/imas/nice-62751-internal",
    "validate/ITER/imas/dina3420",
    "validate/TCV/imas/magnetics-77549/liuqe",
    "validate/TCV/imas/magnetics-77549/dina",
    "validate-quick/TCV/imas/magnetics-77549/liuqe",
    "validate-quick/MASTU/imas/magnetics-v4-1-0",
    "validate-quick/HL-3/imas/magnetics",
    "validate/ITER/imas/corsica",
    "validate/ITER/imas/astra",
    "validate/ITER/imas/dina105027",
    "validate-quick/ITER/imas/fullPulse",
    "validate/ITER/imas/solps",
    "validate/ITER/imas/jorek",
]

_COUNT = len(_CTEST_FAILED_NAMES)

_CTEST_FAILED_BLOCK = "The following tests FAILED:\n" + "".join(
    f"\t{index} - {name} (Failed)\n"
    for index, name in enumerate(_CTEST_FAILED_NAMES, start=1)
)

CTEST_LOG = (
    "Test project /build/sdcc\n"
    + _CTEST_PROGRAM_OUTPUT
    + "\n"
    + _CTEST_FAILED_BLOCK
    + "Errors while running CTest\n"
    + f"{100 - _COUNT}% tests passed, {_COUNT} tests failed out of 59\n"
    + "EXIT=8\n"
)

PYTEST_LOG = """\
FAILED tests/test_x.py::test_one
FAILED tests/test_x.py::test_two
ERROR tests/test_x.py::test_three
3 passed, 2 failed, 1 error in 0.30s
EXIT=1
"""

# A build log: it ran, it exited, and it prints no figure either runner's
# format is recognised by.
UNFORMATTED_LOG = """\
Building case A
Building case B
Built 59 test programs
EXIT=0
"""


def _arm(log_path: Path, **fields: object) -> dict:
    """The receipt an arm writes for this log: its path and the figures it states."""
    return {"log_path": str(log_path), "completed": True, **fields}


def _log(tmp_path: Path, text: str, name: str = "gate.log") -> Path:
    log = tmp_path / name
    log.write_text(text, encoding="utf-8")
    return log


def _counted(tmp_path: Path, text: str, count: int) -> list[str]:
    """Run the receipt comparison over a temporary log and a receipt of ``count``."""
    log = _log(tmp_path, text)
    return _receipt_mismatches(_arm(log, failure_count=count), str(log), text)


def test_a_ctest_receipt_of_22_agrees_with_the_logs_failed_block(
    tmp_path: Path,
) -> None:
    assert _counted(tmp_path, CTEST_LOG, _COUNT) == []


def test_the_same_ctest_log_disagrees_with_a_receipt_of_21(tmp_path: Path) -> None:
    mismatches = _counted(tmp_path, CTEST_LOG, _COUNT - 1)
    assert len(mismatches) == 1
    assert f"failure_count records {_COUNT - 1}" in mismatches[0]
    assert f"names {_COUNT} failed" in mismatches[0]


def test_a_pytest_logs_count_is_read_from_its_summary_line(tmp_path: Path) -> None:
    assert _counted(tmp_path, PYTEST_LOG, 3) == []
    mismatches = _counted(tmp_path, PYTEST_LOG, 2)
    assert len(mismatches) == 1
    assert "failure_count records 2" in mismatches[0]
    assert "names 3 failed" in mismatches[0]


def test_a_log_in_neither_format_raises_no_count_mismatch(tmp_path: Path) -> None:
    assert _counted(tmp_path, UNFORMATTED_LOG, 99) == []
