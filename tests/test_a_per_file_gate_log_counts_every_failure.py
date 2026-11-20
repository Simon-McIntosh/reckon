"""A gate log of several per-file runs counts every failure.

A repository whose suite runs one test process per file appends every run into
one gate log, marking each run with a ``# file:`` header. The promotion's
receipt reader once took the log's last run only, on the premise that several
runs are successive attempts at one suite. It therefore read 0 failures from a
log whose earlier files named six and refused a landing whose arm recorded six.

The cases below are the shapes that reader meets: a marked three-file log whose
first two files failed and whose last passed is counted across all three; an
unmarked log of two attempts at one suite is still read at its last attempt;
and a marked file that ended without a summary line leaves the gate unmeasured
rather than clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import promotion_evidence, review
from reckon.crew.node import CrewError

PER_FILE_COMMAND = (
    "for f in tests/test_a.py tests/test_b.py tests/test_c.py; do "
    'PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q "$f"; done'
)

# Three files of one gate: the first two fail, the third passes, and each run
# closes with its own capture record.
PER_FILE_LOG = """\
# command: for f in tests/test_a.py tests/test_b.py tests/test_c.py; do pytest -q "$f"; done
# file: tests/test_a.py
FAILED tests/test_a.py::test_one
1 failed, 2 passed in 0.21s
EXIT=1
# file: tests/test_b.py
FAILED tests/test_b.py::test_two
1 failed, 1 passed in 0.14s
EXIT=1
# file: tests/test_c.py
3 passed in 0.12s
EXIT=0
"""

PER_FILE_FAILURES = {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}

# Two attempts at one suite: the last attempt's figures are the ones whose
# status the tail records.
TWO_ATTEMPTS_LOG = """\
FAILED tests/test_a.py::test_one
2 passed, 1 failed in 0.20s
EXIT=1
3 passed in 0.10s
EXIT=0
"""

# The second file was killed before its summary line: it states no result at
# all, so the gate has no count.
KILLED_PER_FILE_LOG = """\
# file: tests/test_a.py
FAILED tests/test_a.py::test_one
1 failed, 2 passed in 0.21s
EXIT=1
# file: tests/test_b.py
FAILED tests/test_b.py::test_two
# file: tests/test_c.py
3 passed in 0.12s
EXIT=0
"""


def test_a_per_file_log_sums_every_files_failures() -> None:
    """Every marked file belongs to the gate, so both failures are reported."""
    assert review.gate_log_failure_ids(PER_FILE_LOG) == PER_FILE_FAILURES
    assert review.pytest_summary_failure_total(PER_FILE_LOG) == 2
    assert promotion_evidence._text_the_receipt_describes(PER_FILE_LOG) == PER_FILE_LOG
    assert promotion_evidence._log_failure_count(PER_FILE_LOG) == 2


def test_an_unmarked_log_is_still_read_from_its_last_attempt() -> None:
    """Successive attempts at one suite are not separate files of one gate."""
    assert review.pytest_summary_failure_total(TWO_ATTEMPTS_LOG) == 0
    last = promotion_evidence._text_the_receipt_describes(TWO_ATTEMPTS_LOG)
    assert "FAILED tests/test_a.py::test_one" not in last
    assert review.gate_log_failure_ids(TWO_ATTEMPTS_LOG, last_run=True) == set()


def test_a_per_file_run_without_a_summary_leaves_the_gate_unmeasured() -> None:
    """A killed file is unreadable rather than clean."""
    assert review.per_file_gate_runs(KILLED_PER_FILE_LOG) is not None
    assert review.pytest_summary_failure_total(KILLED_PER_FILE_LOG) is None
    assert review.gate_log_failure_ids(KILLED_PER_FILE_LOG) is None


def _agreement_check(
    tmp_path: Path, log_text: str, *, count: int, ids: list[str]
) -> None:
    """Run the receipt agreement check over a per-file log and its arm."""
    log = tmp_path / "gate.log"
    log.write_text(log_text, encoding="utf-8")
    arm = {
        "log_path": str(log),
        "exit_status": 0,
        "completed": True,
        "failure_count": count,
        "failure_ids": ids,
    }
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "\n".join(
            [
                "status: complete",
                f"after_suite: {json.dumps(arm)}",
                f"tests: {PER_FILE_COMMAND}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record = {"manifest_path": str(manifest)}
    gate = {"log_path": str(log), "exit_status": 0, "command": PER_FILE_COMMAND}
    promotion_evidence._require_gate_log_agrees(
        "r-per-file-gate", gate, record, verdict="passed"
    )


def test_a_receipt_recording_every_files_failure_is_accepted(tmp_path: Path) -> None:
    """The arm's two failures are the gate's, though the last file passed."""
    _agreement_check(tmp_path, PER_FILE_LOG, count=2, ids=sorted(PER_FILE_FAILURES))


def test_a_receipt_for_a_killed_file_is_refused(tmp_path: Path) -> None:
    """A gate holding a file that never summarised cannot be asserted passed."""
    with pytest.raises(CrewError) as refused:
        _agreement_check(
            tmp_path, KILLED_PER_FILE_LOG, count=1, ids=["tests/test_a.py::test_one"]
        )
    assert "reached no pytest summary" in str(refused.value)
