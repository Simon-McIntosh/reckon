"""The standing-suite record carries its skips.

The record gains ``skipped`` and ``skipped_ids``: the count the runner summary
already prints, and the per-item locations a ``-rs`` summary lists. Both are
read from a fixed log, so this measures the record the writer builds rather
than whichever suite this checkout happens to be running. A log with no skips
records ``0`` and an empty list, so an absent field never stands for a skip
count that was never read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reckon.crew import standing_suite

# A fixed ``-rs`` capture: the runner summarises three failures, ten passes and
# two skips, and lists each skip by the file line that declared it.
LOG_WITH_SKIPS = """\
============================= test session starts ==============================
collected 15 items

tests/test_sample.py .....F..F.F...                                      [100%]

=================================== FAILURES ===================================
__________________________________ test_one ____________________________________

    def test_one():
>       assert 1 == 2
E       assert 1 == 2

tests/test_sample.py:4: AssertionError
=========================== short test summary info ============================
FAILED tests/test_sample.py::test_one - assert 1 == 2
FAILED tests/test_sample.py::test_two - assert 1 == 2
FAILED tests/test_sample.py::test_three - assert 1 == 2
SKIPPED [1] tests/test_sample.py:12: needs a GPU
SKIPPED [1] tests/test_sample.py:20: platform specific
3 failed, 10 passed, 2 skipped in 0.42s
EXIT=1
"""

# The same shape with the two skips absent: no ``skipped`` token in the summary
# and no ``SKIPPED`` line, so the record must still answer rather than omit.
LOG_WITHOUT_SKIPS = """\
============================= test session starts ==============================
collected 13 items

tests/test_sample.py .....F..F.F.                                        [100%]

=================================== FAILURES ===================================
__________________________________ test_one ____________________________________

    def test_one():
>       assert 1 == 2
E       assert 1 == 2

tests/test_sample.py:4: AssertionError
=========================== short test summary info ============================
FAILED tests/test_sample.py::test_one - assert 1 == 2
FAILED tests/test_sample.py::test_two - assert 1 == 2
FAILED tests/test_sample.py::test_three - assert 1 == 2
3 failed, 10 passed in 0.42s
EXIT=1
"""


def _record(log_text: str) -> dict[str, Any]:
    """Build the record a fixed log supports, without running a suite."""
    return standing_suite._build_record(
        revision="0" * 40,
        command=["pytest", "-rs", "tests/"],
        exit_status=1,
        log_text=log_text,
        log_path=Path("suite.log"),
        duration_seconds=0.42,
        budget_seconds=1800,
        over_budget=False,
    )


def test_a_record_carries_the_skipped_count_and_both_ids():
    record = _record(LOG_WITH_SKIPS)
    assert record["skipped"] == 2
    assert record["skipped_ids"] == [
        "tests/test_sample.py:12",
        "tests/test_sample.py:20",
    ]


def test_a_log_with_no_skips_records_zero_and_an_empty_list():
    record = _record(LOG_WITHOUT_SKIPS)
    assert record["skipped"] == 0
    assert record["skipped_ids"] == []
