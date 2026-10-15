"""A ctest gate log's failures are read, and an unreadable log is unmeasured.

A gate log in this fleet is written by either of two runners. A run whose gate
is ctest reports its failures — including the tests a build break left Not Run —
in a block under ``The following tests FAILED:``, carrying no pytest node id.
Read through the pytest reader alone, both arms of such a pair yield no ids, so
a difference that added two failing tests is stored as a measured ``(0, [])``,
which is the direction the count exists to prevent: a stored zero asserts a
measurement that was never taken.

These tests drive ``added_failures_from_gate_logs`` with synthesised ctest and
pytest logs, and require the count to be a difference over the ids of one
format. A log in neither format, or a pair split across the two, reads as
unmeasured rather than as a zero.
"""

from __future__ import annotations

from reckon.crew import review as review_module

BASE_CTEST_GREEN = """\
Test project /build/reckon
    Start 1: unit/marching_squares
1/2 Test #1: unit/marching_squares .........   Passed    0.10 sec
    Start 2: unit/leg_extraction
2/2 Test #2: unit/leg_extraction ...........   Passed    0.20 sec

100% tests passed, 0 tests failed out of 2

Total Test time (real) =   0.30 sec
"""

HEAD_CTEST_TWO_FAILURES = """\
Test project /build/reckon
    Start 1: unit/marching_squares
1/2 Test #1: unit/marching_squares .....***Not Run   0.00 sec
    Start 2: unit/leg_extraction
2/2 Test #2: unit/leg_extraction .......***Failed    0.00 sec

50% tests passed, 2 tests failed out of 2

The following tests FAILED:
\t  1 - unit/marching_squares (Not Run)
\t  2 - unit/leg_extraction (Failed)
Errors while running CTest
"""


def _count(base_text: str, head_text: str) -> tuple[int | None, list[str]]:
    return review_module.added_failures_from_gate_logs(base_text, head_text)


def test_a_ctest_failure_block_is_read_including_not_run() -> None:
    """The two failing entries, one Not Run, are added against a green base."""
    count, added = _count(BASE_CTEST_GREEN, HEAD_CTEST_TWO_FAILURES)

    assert count == 2, (
        "a ctest head log listing two failing entries against a green base was "
        "not read; read through the pytest reader alone it stores a measured "
        f"zero, the direction ({added!r}) the count exists to prevent"
    )
    assert added == ["unit/leg_extraction", "unit/marching_squares"]


def test_ctest_logs_sharing_a_failure_add_nothing() -> None:
    """Both arms listing the same failure is not an added failure."""
    count, added = _count(HEAD_CTEST_TWO_FAILURES, HEAD_CTEST_TWO_FAILURES)

    assert count == 0, "a failure present in both arms is not an added failure"
    assert added == []


def test_a_head_log_in_neither_format_is_unmeasured() -> None:
    """A log carrying no recognised summary is unmeasured, never a zero."""
    head = "some installer output\nthat records no test summary at all\n"

    assert _count(BASE_CTEST_GREEN, head) == (None, []), (
        "a head log in neither format cannot be read, so the count is "
        "unmeasured rather than the measured zero an unread log would store"
    )


def test_a_pair_split_across_formats_is_unmeasured() -> None:
    """A ctest name and a pytest node id name different things, so no difference."""
    pytest_log = "FAILED tests/test_gate.py::test_alpha - AssertionError: boom\n1 failed\n"

    assert _count(BASE_CTEST_GREEN, pytest_log) == (None, [])


def test_a_pytest_log_is_still_read() -> None:
    """The pytest reader is unchanged for a log in pytest format."""
    base = "FAILED tests/test_gate.py::test_alpha - AssertionError: boom\n1 failed\n"
    head = (
        "FAILED tests/test_gate.py::test_alpha - AssertionError: boom\n"
        "FAILED tests/test_gate.py::test_beta - AssertionError: boom\n"
        "2 failed\n"
    )

    assert _count(base, head) == (1, ["tests/test_gate.py::test_beta"])