"""A cut-down ctest control log is read as red from its progress line.

A negative-control log is sometimes an extract of a longer ctest run: the
capture window opens below the run summary, so the log carries ctest's progress
lines and the test's own output but no ``The following tests FAILED:`` block.
The reader recognised a ctest run only from that block or from the run summary,
so such an extract was read as a pytest log — its ``1 failed, 0 passed`` line
matches the pytest summary — and a pytest log with no ``FAILED`` node id names
no failure at all. The control was then refused as one never shown to fail, and
the run was landed with the control waived.

These tests drive the shared gate-log reader with a synthesised extract of each
shape and require the red one to name the test ctest reported, the green one to
read green, and the full-summary and pytest logs to read as they did.
"""

from __future__ import annotations

from reckon.crew import promotion_evidence as evidence_module
from reckon.crew import review as review_module

# An extract of a ctest gate log: three header lines, the progress line naming
# the failed test, the test's own output, the entry line ctest repeats below its
# colourised failure, then a count line with no percentage and the capture's
# exit record.
CTEST_EXTRACT_FAILED = """\
# revision 204509cf6f tree /build/imas-efit
# command: ctest -R unit/imas/iron_limb_outline --output-on-failure
# started 2026-11-22T00:14:02Z
1/1 Test #1155: unit/imas/iron_limb_outline ......***Failed    0.31 sec
\x1b[31mFAIL\x1b[0m outline point 7 lies outside the separatrix
\t  1155 - unit/imas/iron_limb_outline (Failed)
1 failed, 0 passed
EXIT=8
"""

CTEST_EXTRACT_PASSED = """\
# revision 204509cf6f tree /build/imas-efit
# command: ctest -R unit/imas/iron_limb_outline --output-on-failure
# started 2026-11-22T00:20:11Z
1/1 Test #1155: unit/imas/iron_limb_outline ......   Passed    0.28 sec
1 passed, 0 failed
EXIT=0
"""

# The same extract with its progress line removed, so only the entry line names
# the failure. A log whose header fell outside the capture has this shape too.
CTEST_EXTRACT_ENTRY_ONLY = """\
# revision 204509cf6f tree /build/imas-efit
# command: ctest -R unit/imas/iron_limb_outline --output-on-failure
outline point 7 lies outside the separatrix
\t  1155 - unit/imas/iron_limb_outline (Failed)
1 failed, 1 passed
EXIT=8
"""

CTEST_FULL_SUMMARY = """\
Test project /build/reckon
    Start 1: unit/marching_squares
1/2 Test #1: unit/marching_squares .........   Passed    0.10 sec
1/2 Test #2: unit/leg_extraction .......***Failed    0.00 sec

50% tests passed, 1 tests failed out of 2

The following tests FAILED:
\t  2 - unit/leg_extraction (Failed)
Errors while running CTest
EXIT=8
"""

PYTEST_LOG = """\
revision b5444d24ba
$ pytest -q tests/test_charge.py
FAILED tests/test_charge.py::test_charge - AssertionError
1 failed, 2 passed in 0.41s
EXIT=1
"""

# A test's own output that happens to print an entry-shaped line with a harmless
# reason is not a ctest failure, so it must not read as one.
CTEST_OUTPUT_THAT_LOOKS_LIKE_AN_ENTRY = """\
Test project /build/reckon
1/1 Test #7: unit/shape .......   Passed    0.10 sec
  check 3 - residual (small)
100% tests passed, 0 tests failed out of 1
EXIT=0
"""


def test_an_extract_naming_a_failed_ctest_test_reads_red() -> None:
    """The extract is ctest, and it names the test its progress line reported."""
    ids = review_module.gate_log_failure_ids(CTEST_EXTRACT_FAILED)

    assert ids == {"unit/imas/iron_limb_outline"}, (
        "a ctest extract whose progress line reads ***Failed was not read as "
        "red, so a control log of this shape is refused as one never shown to "
        f"fail; the reader returned {ids!r}"
    )
    assert review_module._gate_log_format(CTEST_EXTRACT_FAILED) == "ctest"


def test_the_entry_line_alone_names_the_failed_test() -> None:
    """An entry line below no failure header still names the test ctest failed."""
    ids = review_module.gate_log_failure_ids(CTEST_EXTRACT_ENTRY_ONLY)

    assert ids == {"unit/imas/iron_limb_outline"}, (
        "an extract whose progress line fell outside the capture, leaving only "
        f"the entry line ctest repeats, read as {ids!r}"
    )


def test_a_ctest_log_with_the_full_summary_header_reads_as_today() -> None:
    """The block's entries are read exactly as before."""
    ids = review_module.gate_log_failure_ids(CTEST_FULL_SUMMARY)

    assert ids == {"unit/leg_extraction"}


def test_a_green_ctest_extract_reads_green_and_is_refused_as_a_control() -> None:
    """An all-Passed extract is read, names no failure, and admits no control."""
    ids = review_module.gate_log_failure_ids(CTEST_EXTRACT_PASSED)

    assert ids == set(), (
        "an all-Passed ctest extract must read green rather than unreadable; "
        f"the reader returned {ids!r}"
    )
    assert (
        evidence_module._control_log_added_failure_ids(CTEST_EXTRACT_PASSED, set())
        == set()
    ), (
        "a green control log must not be admitted as one that failed; an "
        "extract read as unmeasured or as naming a failure would be"
    )


def test_a_pytest_log_is_read_as_today() -> None:
    """A pytest log still reads through the pytest reader."""
    ids = review_module.gate_log_failure_ids(PYTEST_LOG)

    assert ids == {"tests/test_charge.py::test_charge"}
    assert review_module._gate_log_format(PYTEST_LOG) == "pytest"


def test_an_output_line_shaped_like_an_entry_is_not_a_failure() -> None:
    """A passed ctest run whose test output prints an entry line reads green."""
    ids = review_module.gate_log_failure_ids(CTEST_OUTPUT_THAT_LOOKS_LIKE_AN_ENTRY)

    assert ids == set(), (
        "an entry-shaped line with a harmless reason in a test's own output was "
        f"read as a ctest failure: {ids!r}"
    )
