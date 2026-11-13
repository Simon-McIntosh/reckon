"""The review store's index is maintained by its writers, not rebuilt by readers.

A reader that can only tell a fresh index from a stale one by the store
directory's stat identity must, on any write, list the store and stat every
record it names. On a store of thousands of records that is seconds of GPFS per
refresh, paid once per duty item. This case builds such a store and shows the
replacement: one document inside the store directory, advanced by the writer
that wrote the record, carrying a per-record hash, a per-run digest, a version
and a bounded change log, so a reader answers from the document and never lists
or stats what it already knows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import review, review_records
from reckon.crew.run_paths import crew_home

PROJECT = "fixture"
RUNS = 50
RECORDS_PER_RUN = 10
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


def _store_directory() -> Path:
    return review.review_store_root() / PROJECT


def _populate(directory: Path) -> list[str]:
    """Write the store's records at the paths its own naming would choose."""
    directory.mkdir(parents=True, exist_ok=True)
    run_ids: list[str] = []
    for run in range(RUNS):
        run_id = f"r-20261113T1200{run:02d}-node-{run:02d}"
        run_ids.append(run_id)
        for index in range(RECORDS_PER_RUN):
            name = f"{run_id}.json" if index == 0 else f"{run_id}.at-{index:040x}.json"
            (directory / name).write_text(
                json.dumps(_payload(run_id, index)), encoding="utf-8"
            )
    return run_ids


def _count_store_reads(directory: Path) -> tuple[list, object]:
    """Count every stat of a record or the directory, and every listing.

    The base reader stats each record whenever the directory moved, and lists
    the store to learn which records there are; counting both is what tells a
    reader that reads only what changed from one that re-derives the store.
    """
    counted: list[str] = []
    real_stat = os.stat
    real_scandir = os.scandir
    real_glob = Path.glob
    inside_glob = False

    def path_of(value):
        try:
            return Path(os.fsdecode(value))
        except (TypeError, ValueError):
            return None

    def counted_stat(value, *args, **kwargs):
        target = path_of(value)
        if target is not None:
            if target == directory:
                counted.append("stat:directory")
            elif target.parent == directory and target.suffix == ".json":
                counted.append(f"stat:{target.name}")
        return real_stat(value, *args, **kwargs)

    def counted_scandir(value="."):
        nonlocal inside_glob
        if not inside_glob and not isinstance(value, int):
            target = path_of(value)
            if target == directory:
                counted.append("scandir")
        return real_scandir(value)

    def counted_glob(self, pattern, **kwargs):
        nonlocal inside_glob
        if self == directory:
            counted.append(f"glob:{pattern}")
        inside_glob = True
        try:
            return real_glob(self, pattern, **kwargs)
        finally:
            inside_glob = False

    os.stat, os.scandir, Path.glob = counted_stat, counted_scandir, counted_glob

    def restore():
        os.stat, os.scandir, Path.glob = real_stat, real_scandir, real_glob

    return counted, restore


@pytest.fixture()
def stored(tmp_path, monkeypatch):
    home = crew_home()
    directory = _store_directory()
    run_ids = _populate(directory)
    review_records._STORE_DOCUMENTS.clear()
    review_records._STORE_INDEXES.clear()
    review_records._RESOLVED_DIRECTORIES.clear()
    yield directory, run_ids, home
    review_records._STORE_DOCUMENTS.clear()
    review_records._STORE_INDEXES.clear()
    review_records._RESOLVED_DIRECTORIES.clear()


def test_a_stored_record_is_returned_by_its_run_id_without_listing(stored) -> None:
    directory, run_ids, _home = stored
    run_id = run_ids[0]
    path = review.store_review(_payload(run_id, 99))

    counted, restore = _count_store_reads(directory)
    try:
        names = review.review_records_index_names(directory, run_id)
    finally:
        restore()

    assert path.name in names, (
        "the index did not return the record the writer stored: "
        f"{names[:4]} for {path.name}"
    )
    assert counted == [], (
        "looking a stored run up in the index listed or stat'ed the store: "
        f"{counted[:8]}"
    )


def test_reading_every_run_after_a_store_lists_and_stats_nothing(stored) -> None:
    directory, run_ids, _home = stored
    review_records.store_index_document(directory)
    review.store_review(_payload(run_ids[7], 99))

    counted, restore = _count_store_reads(directory)
    try:
        with review_records.review_store_tick():
            for run_id in run_ids:
                review.store_index_run_digest(directory, run_id)
                review.store_index_document(directory)
    finally:
        restore()

    assert counted == [], (
        "reading every run's records after a store listed or stat'ed the store: "
        f"{[item for item in counted if item.startswith(('scandir', 'glob'))][:4]} "
        f"listings, {sum(1 for i in counted if i.startswith('stat:' + 'r-'))} record "
        f"stats"
    )


def test_the_change_log_names_exactly_the_run_written(stored) -> None:
    directory, run_ids, _home = stored
    review_records.store_index_document(directory)
    before = review_records.store_index_version(directory)
    digests_before = {
        run_id: review_records.store_index_run_digest(directory, run_id)
        for run_id in run_ids
    }

    review.store_review(_payload(run_ids[7], 99))

    changed = review_records.store_index_changed_runs(directory, before)
    assert changed == {run_ids[7]}, (
        "the change log names runs the write did not touch: "
        f"{sorted(changed)[:6]} against a single written run {run_ids[7]}"
    )
    after = {
        run_id: review_records.store_index_run_digest(directory, run_id)
        for run_id in run_ids
    }
    assert after[run_ids[7]] != digests_before[run_ids[7]], (
        "the written run's digest did not move"
    )
    moved = {run for run in run_ids if after[run] != digests_before[run]}
    assert moved == {run_ids[7]}, f"digests moved for runs nothing wrote: {moved}"


def test_a_foreign_write_is_found_by_one_listing_of_names(stored) -> None:
    directory, run_ids, _home = stored
    review_records.store_index_document(directory)
    foreign = directory / f"{run_ids[3]}.at-{'e' * 40}.json"
    foreign.write_text(json.dumps(_payload(run_ids[3], 77)), encoding="utf-8")

    counted, restore = _count_store_reads(directory)
    try:
        review_records.reconcile_foreign_write(directory)
    finally:
        restore()

    listings = [item for item in counted if item.startswith(("scandir", "glob"))]
    record_stats = [
        item
        for item in counted
        if item.startswith("stat:") and item != "stat:directory"
    ]
    assert run_ids[3] in review_records.store_index_changed_runs(directory, 0) or (
        foreign.name in review_records.store_index_document(directory)["entries"]
    ), "the foreign record was not indexed"
    assert len(listings) == 1, f"the foreign write cost {len(listings)} listings"
    assert record_stats == [], (
        "the foreign write re-stat'ed records the index already held: "
        f"{record_stats[:6]}"
    )


def test_a_missing_index_is_rebuilt_from_one_listing(stored) -> None:
    directory, run_ids, _home = stored
    review_records.store_index_document(directory)
    document = review_records._index_document_path(directory)
    document.unlink()
    review_records._STORE_DOCUMENTS.clear()

    counted, restore = _count_store_reads(directory)
    try:
        rebuilt = review_records.store_index_document(directory)
    finally:
        restore()

    listings = [item for item in counted if item.startswith(("scandir", "glob"))]
    assert len(listings) == 1, f"the rebuild listed the store {len(listings)} times"
    assert len(rebuilt["entries"]) >= RUNS * RECORDS_PER_RUN, (
        "the rebuilt index holds fewer records than the store: "
        f"{len(rebuilt['entries'])}"
    )
    assert set(rebuilt["runs"]) == set(run_ids)


def test_a_second_process_write_is_seen_by_the_first(stored) -> None:
    directory, run_ids, _home = stored
    review_records.store_index_document(directory)
    before = review_records.store_index_version(directory)

    script = (
        "import json, sys\n"
        "from reckon.crew import review\n"
        f"review.store_review({_payload(run_ids[7], 98)!r})\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]

    after = review_records.store_index_version(directory)
    assert after > before, (
        "the first process did not see the second process's write: the version "
        f"stayed {before}"
    )
    names = review.review_records_index_names(directory, run_ids[7], reload=True)
    assert names, f"the second process's record is not in the index: {names}"
