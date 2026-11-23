"""Processes on different code keep separate review-store index documents.

A process files the review-store index under a stamp of the code that reads it,
and a document filed under another stamp is unusable to it. With one shared
document, a long-lived process still on older code and the services on current
code each found the other's document unusable and rebuilt it from every record
in the store, in turn, every few seconds. On 23 November that read the reckon
review store's 9,361 files (128 MB on shared storage) about once every twenty
seconds. Each code version now reads and writes its own document, so neither
rebuilds after its first build.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import review, review_records

PROJECT = "two-code-versions"
HEAD = "0123456789abcdef0123456789abcdef01234567"
CURRENT = "c" * 64
OLDER = "a" * 64


def _payload(run_id: str) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}",
        "reviewed_at": "2026-11-14T12:00:00Z",
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
    }


def _forget_process_state() -> None:
    """What a different process would not hold: maps, memos and the held stamp."""
    for name in ("_DOCUMENT_MAPS", "_STORE_INDEXES", "_RESOLVED_DIRECTORIES"):
        clear = getattr(getattr(review_records, name, None), "clear", None)
        if clear is not None:
            clear()
    review_records._INDEX_CODE_STAMP.clear()


def _as_process_on(stamp: str) -> None:
    _forget_process_state()
    review_records._INDEX_CODE_STAMP.append(stamp)


@pytest.fixture()
def directory(monkeypatch) -> tuple[Path, list[str]]:
    store = review.review_store_root() / PROJECT
    store.mkdir(parents=True, exist_ok=True)
    for number in range(6):
        run_id = f"r-20261114T1200{number:02d}-node-{number:02d}"
        (store / f"{run_id}.json").write_text(
            json.dumps(_payload(run_id)), encoding="utf-8"
        )
    rebuilds: list[str] = []
    real_rebuild = review_records._rebuild_index_document

    def counting_rebuild(path):
        rebuilds.append(review_records._index_code_stamp())
        return real_rebuild(path)

    monkeypatch.setattr(review_records, "_rebuild_index_document", counting_rebuild)
    yield store, rebuilds
    _forget_process_state()


def test_alternating_code_versions_rebuild_once_each(directory) -> None:
    store, rebuilds = directory
    for _round in range(3):
        for stamp in (CURRENT, OLDER):
            _as_process_on(stamp)
            document = review_records.store_index_document(store)
            assert document is not None
            assert document["code_stamp"] == stamp
    assert sorted(rebuilds) == sorted([CURRENT, OLDER]), rebuilds


def test_each_version_writes_its_own_document(directory) -> None:
    store, _rebuilds = directory
    paths = set()
    for stamp in (CURRENT, OLDER):
        _as_process_on(stamp)
        review_records.store_index_document(store)
        paths.add(review_records._index_document_path(store))
    assert len(paths) == 2
    for path in paths:
        assert path.parent == store / ".review-index"
        assert path.is_file()


def test_a_rebuild_retires_only_another_versions_stale_document(directory) -> None:
    store, _rebuilds = directory
    holding = store / ".review-index"
    holding.mkdir(parents=True, exist_ok=True)
    stale = holding / "index-0000000000000000.json"
    recent = holding / "index-1111111111111111.json"
    legacy = holding / "index.json"
    lock = holding / "lock"
    for path in (stale, recent, legacy, lock):
        path.write_text("{}", encoding="utf-8")
    long_ago = time.time() - 3 * 86400
    for path in (stale, legacy, lock):
        os.utime(path, (long_ago, long_ago))

    _as_process_on(CURRENT)
    review_records.store_index_document(store)

    assert not stale.exists()
    assert not legacy.exists()
    assert recent.exists()
    assert lock.exists()
    assert review_records._index_document_path(store).exists()
