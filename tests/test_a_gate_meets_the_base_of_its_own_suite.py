"""A gate is measured against the base of the suite it actually ran.

A run may measure more than one suite, recording the first pair as
``baseline_suite``/``after_suite`` and each further pair under ``suite_pairs``.
Judging every gate against the default pair reads a second suite's inherited
failures as failures the head added, so a run whose second suite is exactly as
red as its base can never take the zero-added path.

These tests drive the red-base admission through a temporary run record: a
second pair whose base and head carry the same ids admits, the same gate
against a second-pair base missing one of them reports that id, and a manifest
recording no further pair behaves as the default pair alone always did.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import promotion_evidence
from reckon.crew.node import CrewError

HEAD_LOG_EXIT = 1

INHERITED_IDS = [
    "tests/test_charge.py::test_charge",
    "tests/test_charge.py::test_discharge",
    "tests/test_charge.py::test_hold",
]


def _arm(
    failure_ids: list[str], *, log_path: str, exit_status: int = HEAD_LOG_EXIT
) -> dict:
    """One suite observation in the shape a manifest records it."""
    return {
        "revision": "deadbeef",
        "command": "python -m pytest -q tests/test_charge.py",
        "exit_status": exit_status,
        "log_path": log_path,
        "failure_ids": failure_ids,
        "completed": True,
    }


def _pytest_log(failed_ids: list[str]) -> str:
    """A pytest log whose summary names each id as FAILED."""
    lines = [
        "=========================== short test summary info ============================"
    ]
    lines.extend(f"FAILED {test_id} - AssertionError: no" for test_id in failed_ids)
    lines.append(f"{len(failed_ids)} failed in 1.00s")
    lines.append(f"EXIT={HEAD_LOG_EXIT}")
    return "\n".join(lines) + "\n"


def _record(tmp_path: Path, manifest: dict) -> dict:
    """A run record whose fresh manifest is the one written here."""
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {"manifest_path": str(manifest_path)}


def _gate(tmp_path: Path, log_name: str, failed_ids: list[str]) -> dict:
    """The gate check the promotion cites, naming the head log it ran."""
    log = tmp_path / log_name
    log.write_text(_pytest_log(failed_ids), encoding="utf-8")
    return {"log_path": str(log), "exit_status": HEAD_LOG_EXIT}


def _two_pair_manifest(tmp_path: Path, *, base_ids: list[str]) -> dict:
    """A red default pair plus a second pair whose head is the gate's log.

    The default pair is red on entirely different tests, so a comparison that
    reads it instead of the second pair reports the second suite's inherited
    ids as added rather than covering them.
    """
    return {
        "node": "a-gate-meets-the-base-of-its-own-suite",
        "status": "complete",
        "baseline_suite": _arm(
            ["tests/test_other.py::test_other"],
            log_path=str(tmp_path / "default-base.log"),
        ),
        "after_suite": _arm(
            ["tests/test_other.py::test_other"],
            log_path=str(tmp_path / "default-head.log"),
        ),
        "suite_pairs": {
            "UNIT": {
                "base": _arm(base_ids, log_path=str(tmp_path / "unit-base.log")),
                "head": _arm(INHERITED_IDS, log_path=str(tmp_path / "unit-head.log")),
            }
        },
    }


def test_gate_matches_the_base_of_its_own_suite(tmp_path: Path) -> None:
    """A second pair's gate is measured against that pair's base, not the default."""
    record = _record(
        tmp_path, _two_pair_manifest(tmp_path, base_ids=list(INHERITED_IDS))
    )
    gate = _gate(tmp_path, "unit-head.log", INHERITED_IDS)

    assert promotion_evidence._red_base_uncovered_ids(record, gate) == set()
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True
    promotion_evidence._require_verdict_matches_exit_status(
        "r-test", record, gate, verdict="passed"
    )


def test_a_second_pair_base_missing_an_id_reports_it(tmp_path: Path) -> None:
    """An id the second pair's base does not carry is the id the head added."""
    base_ids = list(INHERITED_IDS[:-1])
    record = _record(tmp_path, _two_pair_manifest(tmp_path, base_ids=base_ids))
    gate = _gate(tmp_path, "unit-head.log", INHERITED_IDS)

    assert promotion_evidence._red_base_uncovered_ids(record, gate) == {
        INHERITED_IDS[-1]
    }
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is False
    with pytest.raises(CrewError) as raised:
        promotion_evidence._require_verdict_matches_exit_status(
            "r-test", record, gate, verdict="passed"
        )
    assert INHERITED_IDS[-1] in str(raised.value)


def test_a_manifest_without_further_pairs_keeps_the_default_pair(
    tmp_path: Path,
) -> None:
    """A gate on the default pair's head log still measures against it."""
    manifest = {
        "node": "a-gate-meets-the-base-of-its-own-suite",
        "status": "complete",
        "baseline_suite": _arm(
            INHERITED_IDS, log_path=str(tmp_path / "default-base.log")
        ),
        "after_suite": _arm(INHERITED_IDS, log_path=str(tmp_path / "default-head.log")),
    }
    record = _record(tmp_path, manifest)
    gate = _gate(tmp_path, "default-head.log", INHERITED_IDS)

    assert promotion_evidence._red_base_uncovered_ids(record, gate) == set()
    promotion_evidence._require_verdict_matches_exit_status(
        "r-test", record, gate, verdict="passed"
    )
