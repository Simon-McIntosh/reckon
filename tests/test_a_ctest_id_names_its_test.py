"""A ctest id copied from a run's summary names the same test as the log reader.

ctest prints a failing test in its run summary as ``157 - <name>``, the index
being the test's position in that one run's list, and a worker copies that line
verbatim into the manifest's ``baseline_suite.failure_ids``. The gate-log reader
takes the name alone out of a ``The following tests FAILED:`` block. Left
uncanonicalised, the two spellings of one test compare unequal, so a head that
adds nothing over a red base is refused: the head id is not in the baseline
set, and the delta reads as an addition the run never made.

These tests require ``canonical_node_id`` to drop a leading ctest index, and
drive ``_zero_added_against_a_red_base`` with a manifest whose base records the
indexed spelling beside a head ctest log failing only that test.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import promotion_evidence
from reckon.crew import review as review_module

CTEST_NAME = "XPOINT_BASELINE/ITER/imas/corsica"
INDEXED_ID = "157 - XPOINT_BASELINE/ITER/imas/corsica"
UNRELATED_BASE_FAILURE = "tests/test_elsewhere.py::test_a_pre_existing_failure"

# A ctest run whose only failure is CTEST_NAME, as ctest writes it in the
# failure block a worker copies the indexed id out of.
HEAD_CTEST_ONE_FAILURE = (
    "Test project /build/reckon\n"
    "    Start 1: XPOINT_BASELINE/ITER/imas/corsica\n"
    "1/1 Test #1: XPOINT_BASELINE/ITER/imas/corsica ...***Failed    0.00 sec\n"
    "\n"
    "0% tests passed, 1 tests failed out of 1\n"
    "\n"
    "The following tests FAILED:\n"
    "\t  1 - XPOINT_BASELINE/ITER/imas/corsica (Failed)\n"
    "Errors while running CTest\n"
)


def _arm(failure_ids: list[str], *, exit_status: int) -> dict:
    """One suite observation in the shape a manifest records it."""
    return {
        "revision": "deadbeef",
        "command": "ctest --test-dir build --output-on-failure",
        "exit_status": exit_status,
        "log_path": "logs/arm.log",
        "completed": True,
        "failure_ids": failure_ids,
    }


def _record(tmp_path: Path, *, baseline_ids: list[str]) -> dict:
    """A run record whose fresh manifest records a red base and a complete head."""
    manifest = {
        "node": "a-ctest-id-names-its-test",
        "status": "complete",
        "baseline_suite": _arm(baseline_ids, exit_status=1),
        "after_suite": _arm([CTEST_NAME], exit_status=1),
    }
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {"manifest_path": str(manifest_path)}


def _head_gate(tmp_path: Path) -> dict:
    """The head arm's gate check, citing a ctest log failing only CTEST_NAME."""
    log = tmp_path / "head.log"
    log.write_text(HEAD_CTEST_ONE_FAILURE, encoding="utf-8")
    return {"log_path": str(log)}


def test_a_ctest_index_is_not_part_of_the_test_id() -> None:
    """A summary id carrying its index reduces to the name the log reader keeps."""
    indexed = review_module.canonical_node_id(INDEXED_ID)
    assert indexed == CTEST_NAME, (
        "a ctest index is a position in one run's list, not the test's identity; "
        "left in place the manifest id and the gate-log id for one test never "
        "compare equal"
    )


def test_an_indexed_base_admits_a_head_that_adds_nothing(tmp_path: Path) -> None:
    """A red base recorded with the ctest index covers the head's one failure."""
    record = _record(tmp_path, baseline_ids=[INDEXED_ID])
    gate = _head_gate(tmp_path)

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True, (
        "the base names the head's one failing ctest test under the indexed "
        "spelling a worker copies from the summary; canonicalising both sides "
        "makes it zero added against a red base"
    )


def test_an_indexed_base_adds_a_head_failure_it_does_not_name(tmp_path: Path) -> None:
    """The index strip must not admit a head failure the base does not name."""
    record = _record(tmp_path, baseline_ids=[UNRELATED_BASE_FAILURE])
    gate = _head_gate(tmp_path)

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False, (
        "the head's ctest failure is not in the base set, so the run added a "
        "failure and the red-base clause must refuse"
    )
