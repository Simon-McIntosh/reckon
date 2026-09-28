"""A declared negative control is judged by what its run did, not how it is worded.

A node that writes a check declares the mutation that check must fail against,
and promotion discharges the declaration by reading the log the mutation
produced. Comparing that log's text with the declaration admits any wording that
contains the declaration and refuses any wording that does not, so it is
defeatable in the one direction that matters: pasting the declaration into a log
whose run exited zero satisfies it, and nothing afterwards separates that from an
honest re-run.

The gate here reads the two facts a run leaves behind instead — its terminal
exit record is non-zero, and it names at least one failing test id the baseline
does not fail — so the cases below exercise each refusal on a log that would
satisfy a textual comparison, and each admission on a log that would not. A
fact neither record carries is not inferred from a count or from wording: a log
without the facts is refused, or waived by a reasoned waiver. The two parties
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
    waiver: str = "",
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
        waiver_reason=waiver,
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


def test_a_control_log_with_no_exit_record_is_refused(tmp_path: Path) -> None:
    """A fact nothing recorded is unknown, never inferred from a failing count."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=(
                f"{DECLARATION}\n"
                f"FAILED {ADDED_FAILURE} - AssertionError: no refusal\n"
                "1 failed, 4 passed in 0.21s\n"
            ),
        )

    message = str(refusal.value)
    assert "records no EXIT status" in message
    assert DECLARATION in message


def test_a_control_log_naming_no_failing_test_is_refused(tmp_path: Path) -> None:
    """The runner's own count is a summary, not an identity to compare."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=f"{DECLARATION}\n1 failed, 4 passed in 0.21s\nEXIT=1\n",
        )

    message = str(refusal.value)
    assert "names no failing test id" in message
    assert DECLARATION in message


def test_a_reasoned_waiver_is_the_door_unrecorded_facts_may_enter_by(
    tmp_path: Path,
) -> None:
    """Refused facts with a waiver are recorded as waived, never admitted."""
    control = _gate(
        tmp_path,
        log_text=f"{DECLARATION}\n1 failed, 4 passed in 0.21s\n",
        waiver="no exit record was kept; the mutation was observed by hand.",
    )

    assert control["verdict"] == "waived"
    assert control["reason"]
    assert control["added_failure_ids"] == []
