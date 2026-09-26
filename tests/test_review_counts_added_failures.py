"""A stored review carries the failures its reviewed run added, by id.

A review scored on a run whose own gate logs show tests that were red at the
head and green at the base reports a verdict a reader cannot reconcile with the
evidence the run produced. These tests pin the count to a set difference over
pytest node ids rather than a difference of two counts, because a run that
fixed one pre-existing failure while introducing another nets to zero by number
and added a failing test by id.

Each case synthesises a reviewed run in ``tmp_path``: two gate logs and the
manifest that names them. Nothing reaches outside the temporary repository the
test builds.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import review as review_module

PROJECT = "proj"
RUN = "r-20260926T000000000000-reviewed-run"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

A = "tests/test_gate.py::test_alpha"
B = "tests/test_gate.py::test_beta"
C = "tests/test_gate.py::test_gamma"
D = "tests/test_gate.py::test_delta"


def _log(failed: list[str]) -> str:
    lines = [f"FAILED {test_id} - AssertionError: boom" for test_id in failed]
    lines.append(f"{len(failed)} failed")
    return "\n".join(lines) + "\n"


def _manifest(
    base_failed: list[str] | None, head_failed: list[str], root: Path
) -> dict:
    """Write the two gate logs under ``root`` and name them in a manifest."""
    manifest: dict = {}
    if base_failed is not None:
        base_log = root / "base.log"
        base_log.write_text(_log(base_failed), encoding="utf-8")
        manifest["baseline_suite"] = {"log_path": str(base_log)}
    head_log = root / "head.log"
    head_log.write_text(_log(head_failed), encoding="utf-8")
    manifest["after_suite"] = {"log_path": str(head_log)}
    return manifest


def _record(total: int = 90) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, total // 5),
        "absent": [],
        "total": total,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
    }


def _store(
    tmp_path: Path, manifest: dict, *, total: int = 90, done_when: str = ""
) -> dict:
    path = review_module.store_review(
        _record(total),
        base_dir=tmp_path / "store",
        manifest=manifest,
        run_dir=tmp_path,
        done_when=done_when,
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_added_failures_are_counted_by_id_and_cap_the_total(tmp_path: Path) -> None:
    manifest = _manifest([A, B], [B, C, D], tmp_path)

    stored = _store(tmp_path, manifest)

    assert stored["added_failure_count"] == 2, (
        "three head failures less two base failures is one by number; the run "
        "added two tests by id, and the count must be by id"
    )
    assert stored["added_failure_ids"] == sorted([C, D])
    assert stored["total"] <= review_module.ADDED_FAILURES_TOTAL_CAP, (
        "a run that added unretired failures must not carry its uncapped total"
    )
    assert "capped" in stored["added_failures_note"]


def test_ids_retired_by_name_leave_the_total_uncapped(tmp_path: Path) -> None:
    manifest = _manifest([A, B], [B, C, D], tmp_path)
    done_when = f"the suite is green apart from {C} and {D}, both retired by name"

    stored = _store(tmp_path, manifest, done_when=done_when)

    assert stored["added_failure_count"] == 2
    assert stored["added_failure_ids"] == sorted([C, D])
    assert stored["total"] == 90, (
        "retiring the added ids by name leaves the total alone"
    )
    assert "not capped" in stored["added_failures_note"]


def test_a_missing_base_log_records_unmeasured_not_zero(tmp_path: Path) -> None:
    manifest = _manifest(None, [B, C, D], tmp_path)

    stored = _store(tmp_path, manifest)

    assert stored["added_failure_count"] is None, (
        "an unmeasured count must not be stored as a measured zero"
    )
    assert stored["added_failure_ids"] == []
    assert stored["total"] == 90, "an unmeasured count must not cap the total"
    assert "unmeasured" in stored["added_failures_note"]


def test_a_base_only_fixes_run_reads_zero_and_uncapped(tmp_path: Path) -> None:
    """The positive control: nothing added means zero, and zero is not unmeasured."""
    manifest = _manifest([A, B, C], [B, C], tmp_path)

    stored = _store(tmp_path, manifest)

    assert stored["added_failure_count"] == 0
    assert stored["added_failure_ids"] == []
    assert stored["total"] == 90
