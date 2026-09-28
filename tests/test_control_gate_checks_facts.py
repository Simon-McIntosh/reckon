"""A declared negative control is judged by what its run did, not how it is worded.

A node that writes a check declares the mutation that check must fail against,
and promotion discharges the declaration by reading the log the mutation
produced. Comparing that log's text with the declaration admits any wording that
contains the declaration and refuses any wording that does not, so it is
defeatable in the one direction that matters: pasting the declaration into a log
whose run exited zero satisfies it, and nothing afterwards separates that from an
honest re-run.

The gate here reads the two facts a run leaves behind instead — it exited
non-zero, and its failure set contains at least one test the baseline does not
fail — so the cases below exercise each refusal on a log that would satisfy a
textual comparison, and each admission on a log that would not. The two parties
the facts cannot settle, whether the mutation applied is the one declared, stay
with the reader: the declaration rides the verdict row beside the log path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion import _require_declared_negative_control

DECLARATION = "removing the guard turns the fixture red"
KNOWN_FAILURE = "tests/test_guard.py::test_the_guard_refuses"
ADDED_FAILURE = "tests/test_guard.py::test_the_guard_reports"
RUN_ID = "r-20260928T070000000000-control"


def _gate(
    tmp_path: Path,
    *,
    log_text: str,
    baseline: dict | None = None,
) -> dict:
    """Run the gate over one red log and the manifest that names it."""
    log = tmp_path / "control.log"
    log.write_text(log_text, encoding="utf-8")
    manifest: dict = {"negative_control_log": log.name}
    if baseline is not None:
        manifest["baseline_suite"] = baseline
    record = {
        "run_id": RUN_ID,
        "node": {
            "id": "node-a",
            "write_paths": ["tests/test_guard.py"],
            "negative_control": DECLARATION,
        },
    }
    return _require_declared_negative_control(
        RUN_ID,
        record,
        gate="passed",
        manifest=manifest,
        manifest_path=str(tmp_path / "manifest.md"),
    )


def test_a_control_whose_run_exits_zero_is_refused(tmp_path: Path) -> None:
    """The defeat a textual comparison admits: the declaration on line one, the run green."""
    with pytest.raises(CrewError) as refusal:
        _gate(tmp_path, log_text=f"{DECLARATION}\n1 passed\nEXIT=0\n")

    message = str(refusal.value)
    assert "EXIT=0" in message
    assert DECLARATION in message


def test_a_control_that_adds_no_failure_to_the_baseline_is_refused(
    tmp_path: Path,
) -> None:
    """The case a textual comparison cannot see at all: the run failed only as the baseline had."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=(
                f"{DECLARATION}\n"
                f"FAILED {KNOWN_FAILURE} - AssertionError: no refusal\n"
                "1 failed, 4 passed in 0.19s\n"
                "EXIT=1\n"
            ),
            baseline={"failure_ids": [KNOWN_FAILURE], "failure_count": 1},
        )

    message = str(refusal.value)
    assert "adds no failure" in message
    assert KNOWN_FAILURE in message


def test_a_control_that_adds_a_failure_is_admitted_whatever_its_wording(
    tmp_path: Path,
) -> None:
    """A run that failed a test the baseline does not is the control, in any words."""
    log_text = (
        "applied: the early return was removed in reckon/guard.py\n"
        f"FAILED {ADDED_FAILURE} - AssertionError: no refusal\n"
        "1 failed, 4 passed in 0.21s\n"
        "EXIT=1\n"
    )
    assert DECLARATION not in log_text

    control = _gate(tmp_path, log_text=log_text)

    assert control["verdict"] == "matched"
    assert control["added_failure_ids"] == [ADDED_FAILURE]
    # The promotion output carries the declaration beside the log path, so the
    # match the gate deliberately does not judge stays available to a reader.
    assert control["declaration"] == DECLARATION
    assert control["log"] == "control.log"


def test_a_control_whose_wording_matches_but_exited_zero_is_refused(
    tmp_path: Path,
) -> None:
    """A matching line does not outrank the run's own status, even with a failure listed."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=(
                f"{DECLARATION}\n"
                f"FAILED {ADDED_FAILURE} - AssertionError: no refusal\n"
                "1 failed, 4 passed in 0.21s\n"
                "EXIT=0\n"
            ),
        )

    message = str(refusal.value)
    assert "EXIT=0" in message
    assert DECLARATION in message
