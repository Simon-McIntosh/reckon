"""A red base recorded with bare test names admits the head's qualified ids.

A worker records ``baseline_suite.failure_ids`` from the summary line it has in
front of it, which often carries the bare test name, while the head arm's gate
log writes the node id ``tests/x.py::test_charge``. Compared verbatim the two
spellings of one test are two tests, the head's ids are not covered, and a run
that added no failure at all is refused as having added one.

These tests drive the red-base admission and its refusal directly, and cover
the four shapes the shared name rule must tell apart: a bare name against
qualified ids, a bare name against two parametrisations, a bracketed bare name
against a parametrisation it does not name, and a qualified id against another
file's id of the same test name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import promotion_evidence, review
from reckon.crew.node import CrewError

HEAD_LOG_EXIT = 1


def _arm(
    failure_ids: list[str],
    *,
    exit_status: int = HEAD_LOG_EXIT,
    completed: bool | None = True,
) -> dict:
    """One suite observation in the shape a manifest records it."""
    arm = {
        "revision": "deadbeef",
        "command": "python -m pytest -q tests/x.py",
        "exit_status": exit_status,
        "log_path": "logs/arm.log",
        "failure_ids": failure_ids,
    }
    if completed is not None:
        arm["completed"] = completed
    return arm


def _record(
    tmp_path: Path, *, baseline_ids: list[str], after_completed: bool | None = True
) -> dict:
    """A run record whose fresh manifest records a red base and a head arm."""
    manifest = {
        "node": "a-red-base-matches-bare-names",
        "status": "complete",
        "baseline_suite": _arm(baseline_ids, exit_status=1),
        "after_suite": _arm(["tests/x.py::test_charge"], completed=after_completed),
    }
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {"manifest_path": str(manifest_path)}


def _pytest_log(failed_ids: list[str]) -> str:
    """A pytest log whose summary names each id as FAILED."""
    lines = [
        "=========================== short test summary info ============================"
    ]
    lines.extend(f"FAILED {test_id} - AssertionError: no" for test_id in failed_ids)
    lines.append(f"{len(failed_ids)} failed in 1.00s")
    lines.append(f"EXIT={HEAD_LOG_EXIT}")
    return "\n".join(lines) + "\n"


def _gate(tmp_path: Path, failed_ids: list[str]) -> dict:
    """The gate check the promotion cites, naming the head log."""
    log = tmp_path / "head.log"
    log.write_text(_pytest_log(failed_ids), encoding="utf-8")
    return {"log_path": str(log), "exit_status": HEAD_LOG_EXIT}


def _refusal(record: dict, gate: dict) -> str:
    """The red-base refusal for this pair, or a failure if it was admitted."""
    with pytest.raises(CrewError) as raised:
        promotion_evidence._require_verdict_matches_exit_status(
            "r-test", record, gate, verdict="passed"
        )
    return str(raised.value)


def test_bare_base_names_admit_qualified_head_ids(tmp_path: Path) -> None:
    """The same tests written bare in the base and qualified in the head admit."""
    record = _record(tmp_path, baseline_ids=["test_charge", "test_discharge"])
    gate = _gate(tmp_path, ["tests/x.py::test_charge", "tests/x.py::test_discharge"])

    assert promotion_evidence._red_base_uncovered_ids(record, gate) == set()
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True
    promotion_evidence._require_verdict_matches_exit_status(
        "r-test", record, gate, verdict="passed"
    )


def test_a_head_id_the_base_lacks_is_refused_and_named(tmp_path: Path) -> None:
    """One uncovered head id refuses the pair and the refusal names it."""
    record = _record(tmp_path, baseline_ids=["test_charge"])
    gate = _gate(tmp_path, ["tests/x.py::test_charge", "tests/x.py::test_new_failure"])

    assert promotion_evidence._red_base_uncovered_ids(record, gate) == {
        "tests/x.py::test_new_failure"
    }
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False
    message = _refusal(record, gate)
    assert "tests/x.py::test_new_failure" in message
    assert "tests/x.py::test_charge" not in message


def test_a_qualified_id_from_another_file_does_not_cover(tmp_path: Path) -> None:
    """Two tests sharing a name in different files are different tests."""
    record = _record(tmp_path, baseline_ids=["tests/other.py::test_charge"])
    gate = _gate(tmp_path, ["tests/x.py::test_charge"])

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False
    assert "tests/x.py::test_charge" in _refusal(record, gate)


def test_a_bare_name_covers_every_parametrisation(tmp_path: Path) -> None:
    """A bare base name covers both parametrisations of the one test."""
    record = _record(tmp_path, baseline_ids=["test_charge"])
    gate = _gate(tmp_path, ["tests/x.py::test_charge[1]", "tests/x.py::test_charge[2]"])

    assert promotion_evidence._red_base_uncovered_ids(record, gate) == set()
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True


def test_a_bracketed_bare_name_covers_only_its_parametrisation(
    tmp_path: Path,
) -> None:
    """``test_charge[1]`` does not cover ``tests/x.py::test_charge[2]``."""
    record = _record(tmp_path, baseline_ids=["test_charge[1]"])
    gate = _gate(tmp_path, ["tests/x.py::test_charge[2]"])

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False
    assert "tests/x.py::test_charge[2]" in _refusal(record, gate)


def test_a_bracketed_bare_name_covers_its_own_parametrisation(
    tmp_path: Path,
) -> None:
    """The same base name does cover the parametrisation it names."""
    record = _record(tmp_path, baseline_ids=["test_charge[1]"])
    gate = _gate(tmp_path, ["tests/x.py::test_charge[1]"])

    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True


def test_an_arm_without_completion_is_refused_without_an_id_list(
    tmp_path: Path,
) -> None:
    """An unfinished head arm is refused on completion, naming no head id."""
    record = _record(tmp_path, baseline_ids=["test_charge"], after_completed=None)
    gate = _gate(tmp_path, ["tests/x.py::test_charge"])

    assert promotion_evidence._red_base_uncovered_ids(record, gate) is None
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False
    message = _refusal(record, gate)
    assert "does not declare completed: true" in message
    assert "tests/x.py::test_charge" not in message


def test_the_predicate_reports_the_uncovered_head_ids() -> None:
    """The shared rule returns head ids, not a boolean or a base-side delta."""
    uncovered = review.failure_ids_covered(
        ["test_charge"], ["tests/x.py::test_charge", "tests/y.py::test_other"]
    )

    assert uncovered == {"tests/y.py::test_other"}
