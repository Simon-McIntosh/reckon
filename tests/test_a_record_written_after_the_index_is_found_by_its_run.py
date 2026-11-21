"""A record written after the index is built is found by its run.

A reader that answers "which records does this run hold" from a per-run copy the
writer maintains beside the index has two copies of one grouping; a reader that
answers from the grouping the index's own entries already hold has one, and it
must move when the store's writer writes. This case builds the index, primes
that grouping by reading the names a run already holds, then writes one more
record through the store's writer path -- no rebuild -- and asks the same lookup
for the run that gained it. A grouping left stale would still answer with the
names read before the write, so the new record's name would be missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import review, review_records
from reckon.crew.run_paths import crew_home

PROJECT = "fixture-record-names"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def _payload(run_id: str, index: int) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}-{index:08x}",
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
    }


def _clear_caches() -> None:
    # The grouping under test is cached per document version; clear the caches
    # between cases so an entry held for another test's store is never read as
    # this one's.
    for name in ("_DOCUMENT_MAPS", "_STORE_INDEXES"):
        cache = getattr(review_records, name, None)
        if hasattr(cache, "clear"):
            cache.clear()


def _store_directory() -> Path:
    return review.review_store_root() / PROJECT


@pytest.fixture()
def store(tmp_path, monkeypatch):
    crew_home()
    _clear_caches()
    directory = _store_directory()
    directory.mkdir(parents=True, exist_ok=True)
    yield directory
    _clear_caches()


def test_a_record_written_after_the_index_is_built_is_found_by_its_run(store) -> None:
    directory = store
    existing_run = "r-20261115T090000-existing-run"
    new_run = "r-20261115T090500-new-run"
    first_name = f"{existing_run}.json"
    (directory / first_name).write_text(
        json.dumps(_payload(existing_run, 0)), encoding="utf-8"
    )

    # The index is built from the store once, before the write under test: the
    # writer below must advance it, never trigger a rebuild.
    review_records.store_index_document(directory)

    # Prime the cached grouping and show it is what the lookup reads: the run
    # that gains a record is not in it yet, and the run already indexed is.
    assert review_records.review_run_record_names(directory, new_run) == []
    assert review_records.review_run_record_names(directory, existing_run) == [
        first_name
    ]
    generation_before = (review_records.store_index_document(directory) or {}).get(
        "generation"
    )

    written_name = f"{new_run}.json"
    review_records.record_written(directory / written_name, _payload(new_run, 0))

    # The writer advanced the document rather than replacing it, so the cached
    # grouping had to move with it.
    after = review_records.store_index_document(directory) or {}
    assert after.get("generation") == generation_before, (
        "the indexed write rebuilt the whole document instead of advancing it"
    )
    names = review_records.review_run_record_names(directory, new_run)
    assert names == [written_name], (
        "the record written after the index was built is missing from its run's "
        f"names: {names}"
    )
    # The run that already had a record is still answered exactly as before.
    assert review_records.review_run_record_names(directory, existing_run) == [
        first_name
    ]
