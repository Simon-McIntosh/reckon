"""Promotion reads a ctest gate log the way the review does.

One reader in the review module names a gate log's failing ids whatever format
the log is written in. The promotion-side readers must go through that same
reader, or the review and promotion disagree about one log: a ctest head log
whose ``The following tests FAILED:`` block lists two entries reads as an empty
set through a pytest-only reader, and an empty set is a subset of every red
base, so a ctest-gated run that added failures is admitted as zero added
whenever its base is red.

These tests drive the two promotion readers with a synthesised ctest head log
and a manifest that records a red base:

- ``_zero_added_against_a_red_base`` refuses when the base names one unrelated
  failure, so the head's two ctest entries are added, and admits when the base
  names both of them.
- ``_head_arm_log_failure_ids`` returns ``None`` for a head log in neither
  format, so the red-base clause refuses rather than measuring an empty set.
  Against the ctest log it names both entries, which is the positive control
  the ``None`` reading is compared with.
- ``_control_failure_ids`` reads the ctest failure block, and for a log in
  neither format returns the empty set the control check treats as never shown
  to fail.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import promotion_checks, promotion_evidence

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

LOG_IN_NEITHER_FORMAT = "some installer output\nthat records no test summary at all\n"

CTEST_FAILURES = {"unit/leg_extraction", "unit/marching_squares"}
UNRELATED_BASE_FAILURE = "tests/test_elsewhere.py::test_a_pre_existing_failure"


def _arm(failure_ids: list[str]) -> dict:
    """One suite observation in the shape a manifest records it."""
    return {
        "revision": "deadbeef",
        "command": "ctest --test-dir build --output-on-failure",
        "exit_status": 1,
        "log_path": "logs/arm.log",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _record(tmp_path: Path, *, baseline_ids: list[str]) -> dict:
    """A run record whose fresh manifest records a red base and a complete head."""
    manifest = {
        "node": "a-ctest-gate-is-read-at-promotion",
        "status": "complete",
        "baseline_suite": _arm(baseline_ids),
        "after_suite": _arm(sorted(CTEST_FAILURES)),
    }
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {"manifest_path": str(manifest_path)}


def _head_gate(tmp_path: Path, text: str) -> dict:
    log = tmp_path / "head.log"
    log.write_text(text, encoding="utf-8")
    return {"log_path": str(log)}


def test_a_ctest_head_log_widens_a_red_base(tmp_path: Path) -> None:
    """The head's two ctest entries are added failures over a base naming one other."""
    record = _record(tmp_path, baseline_ids=[UNRELATED_BASE_FAILURE])
    gate = _head_gate(tmp_path, HEAD_CTEST_TWO_FAILURES)

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False, (
        "a ctest head log's failure block was not read, so its two added ctest "
        "failures read as an empty set against a red base and the run is "
        "admitted as zero added, the direction this reader exists to prevent"
    )


def test_a_red_base_naming_both_ctest_failures_admits(tmp_path: Path) -> None:
    """A base naming every head ctest failure is zero added against a red base."""
    record = _record(
        tmp_path, baseline_ids=["unit/marching_squares", "unit/leg_extraction"]
    )
    gate = _head_gate(tmp_path, HEAD_CTEST_TWO_FAILURES)

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True, (
        "a head that adds no ctest failure over its base is zero added"
    )


def test_a_head_log_in_neither_format_is_unreadable(tmp_path: Path) -> None:
    """A log in neither format reads as ``None``, not as an empty set."""
    gate = _head_gate(tmp_path, HEAD_CTEST_TWO_FAILURES)
    assert promotion_evidence._head_arm_log_failure_ids(gate) == CTEST_FAILURES, (
        "positive control: a readable ctest log must name its failures before an "
        "unreadable log is reported as naming none"
    )

    unreadable = _head_gate(tmp_path, LOG_IN_NEITHER_FORMAT)
    assert promotion_evidence._head_arm_log_failure_ids(unreadable) is None, (
        "a head log in neither format must read as unreadable so the red-base "
        "clause refuses rather than admitting over an empty set"
    )


def test_the_control_reads_a_ctest_failure_block() -> None:
    """The control reader names both entries of the ctest failure block."""
    assert (
        promotion_checks._control_failure_ids(HEAD_CTEST_TWO_FAILURES) == CTEST_FAILURES
    )


def test_the_control_of_a_log_in_neither_format_names_nothing() -> None:
    """A control log in neither format names no failure, the empty set the check reads."""
    assert promotion_checks._control_failure_ids(LOG_IN_NEITHER_FORMAT) == set()
