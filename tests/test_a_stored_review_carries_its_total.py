"""The review store derives a landed review's total from its six scores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import review
from reckon.crew.recovery_review_subject import _review_is_complete


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    return tmp_path / "store"


def _record() -> dict:
    return {
        "project": "stored-total-test",
        "reviewed_run_id": "r-stored-total-test",
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
    }


def _read(store: Path) -> dict:
    found = review.read_review(
        "stored-total-test", "r-stored-total-test", base_dir=store
    )
    assert found is not None
    return found


def test_writer_and_reader_sum_six_scores_without_a_reviewer_total(store: Path) -> None:
    path = review.store_review(_record(), base_dir=store)

    assert json.loads(path.read_text(encoding="utf-8"))["total"] == 108
    found = _read(store)
    assert found["total"] == 108
    assert _review_is_complete(found)


def test_a_missing_dimension_withholds_the_total(store: Path) -> None:
    record = _record()
    absent = review.REVIEW_DIMENSIONS[-1]
    record["scores"].pop(absent)
    record["absent"] = [absent]

    path = review.store_review(record, base_dir=store)

    assert json.loads(path.read_text(encoding="utf-8"))["total"] is None
    found = _read(store)
    assert found["total"] is None
    assert not _review_is_complete(found)


def test_disagreeing_reviewer_total_is_preserved_beside_the_sum(store: Path) -> None:
    record = _record()
    record["total"] = 95

    path = review.store_review(record, base_dir=store)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["total"] == 108
    assert stored["reviewer_total"] == 95
    found = _read(store)
    assert found["total"] == 108
    assert found["reviewer_total"] == 95


def test_legacy_record_without_a_total_reads_complete(store: Path) -> None:
    record = _record()
    path = review.review_path("stored-total-test", "r-stored-total-test", store)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    found = _read(store)

    assert found["total"] == 108
    assert _review_is_complete(found)
    assert "total" not in json.loads(path.read_text(encoding="utf-8"))


def test_null_reviewer_total_is_derived_then_capped_for_added_failures(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    record["total"] = None
    path = review.store_review(record, base_dir=store)
    assert json.loads(path.read_text(encoding="utf-8"))["total"] == 108

    run_directory = tmp_path / "reviewed-run"
    run_directory.mkdir()
    (run_directory / "base.log").write_text("0 failed\n", encoding="utf-8")
    (run_directory / "head.log").write_text(
        "FAILED tests/test_gate.py::test_added - AssertionError: boom\n1 failed\n",
        encoding="utf-8",
    )
    (run_directory / "manifest.md").write_text(
        'baseline_suite: {"log_path": "base.log"}\n'
        'after_suite: {"log_path": "head.log"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(review, "_run_directory", lambda _run_id: run_directory)

    found = _read(store)

    assert found["total"] == review.ADDED_FAILURES_TOTAL_CAP
    assert found["added_failure_count"] == 1
    assert "capped" in found["added_failures_note"]
    assert _review_is_complete(found)


def test_claims_record_keeps_its_own_shape(store: Path) -> None:
    record = _record()
    record["rubric"] = review.CLAIMS_RUBRIC
    path = review.store_review(record, base_dir=store)

    assert "total" not in json.loads(path.read_text(encoding="utf-8"))
    assert "total" not in _read(store)
