"""A findings read answers from an index, re-reading only what moved.

Answering "what is already known about these write paths" by reading every
committed review record costs a dispatch preflight thousands of record reads
across thousands of directories. The reader indexes each record's rows against
the record's stat identity, so a second call over an unchanged store opens no
record at all, a rewritten record is the only record opened, and a deleted
record's rows leave with it — the three facts this test holds.

The negative control is the index's removal: with every call rebuilding it, the
unchanged-store case opens every record and the first assertion fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import findings as findings_module
from reckon.crew import review as review_module

PROJECT = "indexed-findings"
FILE = "reckon/a.py"
RUN_A = "r-reviewed-a"
RUN_B = "r-reviewed-b"
REVIEW_A = "r-20261008T090000000000-review-a"
REVIEW_B = "r-20261008T091000000000-review-b"


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout holding a committed reviews tree."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (tmp_path / "config" / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")})
    )
    return root


@pytest.fixture()
def record_reads() -> list[Path]:
    """Every record path the reader opened, in the order it opened them."""
    return []


@pytest.fixture(autouse=True)
def _count_reads(
    record_reads: list[Path], monkeypatch: pytest.MonkeyPatch
) -> list[Path]:
    """Count the records the reader opens, through the store's record reader."""
    original = review_module._committed_record_at

    def counted(path):
        record_reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(review_module, "_committed_record_at", counted)
    return record_reads


def _record(
    reviewed_run_id: str,
    review_run_id: str,
    text: str,
) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": reviewed_run_id,
        "review_run_id": review_run_id,
        "reviewed_head_sha": "0" * 40,
        "scores": {"evidence": 17},
        "findings": [
            {"file": FILE, "line": "12", "text": text, "severity": "blocking"}
        ],
    }


def _store(checkout: Path, record: dict) -> Path:
    return review_module.store_committed_review(record, root=checkout)


def _two_records(checkout: Path) -> None:
    _store(checkout, _record(RUN_A, REVIEW_A, "unguarded index"))
    _store(checkout, _record(RUN_B, REVIEW_B, "stale docstring"))


def _texts(payload: dict) -> list[str]:
    return sorted(row["text"] for row in payload["rows"])


def test_a_second_call_after_no_change_reads_no_record(
    checkout: Path, record_reads: list[Path]
) -> None:
    _two_records(checkout)

    first = findings_module.findings_by_path(PROJECT, root=checkout)
    assert sorted(_texts(first)) == ["stale docstring", "unguarded index"]
    assert len(record_reads) == 2
    record_reads.clear()

    second = findings_module.findings_by_path(PROJECT, root=checkout)

    assert record_reads == []
    assert _texts(second) == _texts(first)


def test_a_rewritten_record_is_the_only_one_read(
    checkout: Path, record_reads: list[Path]
) -> None:
    _two_records(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)
    record_reads.clear()

    rewritten = review_module.store_committed_review(
        _record(RUN_A, REVIEW_A, "guarded index now"), root=checkout
    )
    record_reads.clear()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert [path.name for path in record_reads] == [rewritten.name]
    assert "guarded index now" in _texts(payload)
    assert "unguarded index" not in _texts(payload)


def test_a_deleted_records_rows_are_gone(
    checkout: Path, record_reads: list[Path]
) -> None:
    _two_records(checkout)
    first = findings_module.findings_by_path(PROJECT, root=checkout)
    assert "stale docstring" in _texts(first)

    stored = review_module.review_path(
        PROJECT,
        RUN_B,
        committed_root=review_module.committed_review_root(PROJECT, root=checkout),
        review_run_id=REVIEW_B,
        rubric=None,
    )
    stored.unlink()
    record_reads.clear()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert "stale docstring" not in _texts(payload)
    assert _texts(payload) == ["unguarded index"]
    assert record_reads == []
