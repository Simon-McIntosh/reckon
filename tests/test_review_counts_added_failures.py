"""A review a promotion reads carries the failures its reviewed run added, by id.

A review scored on a run whose own gate logs show tests that were red at the
head and green at the base reports a verdict a reader cannot reconcile with the
evidence the run produced. These tests pin the count to a set difference over
pytest node ids rather than a difference of two counts, because a run that fixed
one pre-existing failure while introducing another nets to zero by number and
added a failing test by id.

The count is derived on the read path, not where a review is written. A review
worker writes its JSON by hand inside its own fence and nothing in production
calls ``store_review``, so a derivation attached to the write path would leave
every delivered review unannotated. These tests therefore write a review record
by hand and read it back through ``select_review_for_head``, the selector a
promotion uses, asserting on what that production path returns.

Every crew directory is environment-resolved under ``tmp_path``: the run
directory holding the reviewed manifest and its two gate logs, and the review
store holding the record. Nothing reaches outside the temporary home the test
sets up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery as recovery_module
from reckon.crew import review as review_module
from reckon.crew.runs import run_dir

PROJECT = "proj"
RUN = "r-20260926T000000000000-reviewed-run"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

A = "tests/test_gate.py::test_alpha"
B = "tests/test_gate.py::test_beta"
C = "tests/test_gate.py::test_gamma"
D = "tests/test_gate.py::test_delta"

SCORE = 18
TOTAL = SCORE * len(review_module.REVIEW_DIMENSIONS)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary home for this test."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _log(failed: list[str]) -> str:
    lines = [f"FAILED {test_id} - AssertionError: boom" for test_id in failed]
    lines.append(f"{len(failed)} failed")
    return "\n".join(lines) + "\n"


def _manifest_text(
    base_failed: list[str] | None,
    head_failed: list[str],
    *,
    done_when: str = "",
) -> str:
    """Compose a manifest the way the dispatch contract has a worker write one.

    The suite observations are JSON objects on one line, which is the shape a
    delivered manifest carries; the log paths are relative to the run directory,
    so the read path must resolve them there.
    """
    lines = [f"node: {RUN}", "status: complete"]
    if base_failed is not None:
        lines.append(
            "baseline_suite: "
            + json.dumps({"log_path": "base.log", "failure_ids": base_failed})
        )
    lines.append(
        "after_suite: "
        + json.dumps({"log_path": "head.log", "failure_ids": head_failed})
    )
    if done_when:
        lines.append(f"done_when: {done_when}")
    return "\n".join(lines) + "\n"


def _synthesise_run(
    failed_base: list[str] | None,
    failed_head: list[str],
    *,
    done_when: str = "",
) -> Path:
    """Write the reviewed run's manifest and gate logs into its run directory."""
    directory = run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    if failed_base is not None:
        (directory / "base.log").write_text(_log(failed_base), encoding="utf-8")
    (directory / "head.log").write_text(_log(failed_head), encoding="utf-8")
    (directory / "manifest.md").write_text(
        _manifest_text(failed_base, failed_head, done_when=done_when),
        encoding="utf-8",
    )
    return directory


def _write_review() -> Path:
    """Write a review record by hand, as a review worker does in its own fence."""
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, SCORE),
        "absent": [],
        "total": TOTAL,
        review_module.REVIEWED_BASE_KEY: BASE_SHA,
        review_module.REVIEWED_HEAD_KEY: HEAD_SHA,
        "timestamp": "2026-09-26T18:30:00+00:00",
    }
    path = review_module.review_path(PROJECT, RUN, reviewed_head_sha=HEAD_SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def _selected() -> dict:
    """Return the review the production selector reports for the reviewed run."""
    review, stale = recovery_module.select_review_for_head(PROJECT, RUN, HEAD_SHA)
    assert stale == ""
    assert review is not None, "the hand-written record must be selected by its head"
    return review


def test_added_failures_are_counted_by_id_and_cap_the_total(
    crew_home: Path,
) -> None:
    _write_review()
    _synthesise_run([A, B], [B, C, D])

    review = _selected()

    assert review["added_failure_count"] == 2, (
        "three head failures less two base failures is one by number; the run "
        "added two tests by id, and the count must be by id"
    )
    assert review["added_failure_ids"] == sorted([C, D])
    assert review["total"] <= review_module.ADDED_FAILURES_TOTAL_CAP, (
        "a run that added unretired failures must not carry its uncapped total"
    )
    assert "capped" in review["added_failures_note"]


def test_ids_retired_by_name_leave_the_total_uncapped(crew_home: Path) -> None:
    _write_review()
    _synthesise_run(
        [A, B],
        [B, C, D],
        done_when=f"the suite is green apart from {C} and {D}, both retired by name",
    )

    review = _selected()

    assert review["added_failure_count"] == 2
    assert review["added_failure_ids"] == sorted([C, D])
    assert review["total"] == TOTAL, (
        "retiring the added ids by name leaves the total alone"
    )
    assert "not capped" in review["added_failures_note"]


def test_a_missing_base_log_records_unmeasured_not_zero(crew_home: Path) -> None:
    _write_review()
    _synthesise_run(None, [B, C, D])

    review = _selected()

    assert review["added_failure_count"] is None, (
        "an unmeasured count must not be stored as a measured zero"
    )
    assert review["added_failure_ids"] == []
    assert review["total"] == TOTAL, "an unmeasured count must not cap the total"
    assert "unmeasured" in review["added_failures_note"]


def test_a_base_only_fixes_run_reads_zero_and_uncapped(crew_home: Path) -> None:
    """The positive control: nothing added means zero, and zero is not unmeasured."""
    _write_review()
    _synthesise_run([A, B, C], [B, C])

    review = _selected()

    assert review["added_failure_count"] == 0
    assert review["added_failure_ids"] == []
    assert review["total"] == TOTAL


def test_a_run_with_no_manifest_reads_the_record_unchanged(crew_home: Path) -> None:
    """A review read before its run delivered a manifest gains nothing invented."""
    _write_review()

    review = _selected()

    assert "added_failure_count" not in review
    assert review["total"] == TOTAL
