"""A persisted review index covers only the directory state its listing saw.

A concurrent foreign write between listing and persistence must leave the
stored directory stamp stale, so the next reader discovers the missing name.
An unchanged store still costs one directory stat and no per-record stats.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import review as review_module
from reckon.crew import review_records as records_module

PROJECT = "proj"
STORE_SIZE = 40
ABSENT = "r-absent"
HEAD = "beef" * 10


def _record(run_id: str, head: str) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"r-reviewer-{run_id}",
        "status": "parsed",
        "scores": {},
        "absent": [],
        "total": 80,
        "reviewed_head_sha": head,
        "timestamp": "2026-10-01T00:00:00+00:00",
    }


def _head(index: int) -> str:
    return f"{index:040x}"


def _write_record(path: Path, record: dict[str, Any]) -> None:
    review_module._write_record(path, record)


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary store of ``STORE_SIZE`` records, cache directory included."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "config").mkdir()
    directory = review_module.review_store_root() / PROJECT
    directory.mkdir(parents=True)
    for index in range(STORE_SIZE):
        run_id = f"r-stored-{index:05d}"
        _write_record(
            review_module.review_path(PROJECT, run_id), _record(run_id, _head(index))
        )
    return directory


def _cold_reader() -> None:
    """Drop what this process holds, the way a newly started process holds none."""
    records_module._STORE_INDEXES.clear()
    records_module._DOCUMENT_MAPS.clear()


def _persisted_identity(directory: Path) -> int:
    document = json.loads(records_module._index_document_path(directory).read_text())
    return document["dir_mtime_ns"]


def _file_before_each_write(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    run_id: str,
    *,
    after_listing: bool = False,
) -> list[Path]:
    """Place a foreign record beyond the snapshot the document is recording."""
    boundary = "_entries_from_listing" if after_listing else "_persist_index_document"
    original = getattr(records_module, boundary)
    filed: list[Path] = []

    def file_record():
        if not filed:
            before = directory.stat()
            path = review_module.review_path(PROJECT, run_id)
            path.write_text(json.dumps(_record(run_id, HEAD)))
            # Directory timestamps can share one filesystem clock tick.
            os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            filed.append(path)

    def crossing_snapshot(*args: Any):
        if not after_listing:
            file_record()
        result = original(*args)
        if after_listing:
            file_record()
        return result

    monkeypatch.setattr(records_module, boundary, crossing_snapshot)
    return filed


def test_a_record_filed_before_the_index_write_is_not_claimed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-or-update path persists an identity its entries do not cover."""
    run_id = "r-filed-during-the-build"
    records_module._index_document_path(store).unlink()
    filed = _file_before_each_write(monkeypatch, store, run_id, after_listing=True)

    records_module._store_index(store)

    assert filed, "the fixture filed its record before the index was written"
    assert _persisted_identity(store) != records_module._directory_mtime_ns(store), (
        "the persisted identity must not be one taken after the listing"
    )

    _cold_reader()
    entries = records_module._store_index(store).get(run_id) or []
    assert [entry["path"] for entry in entries] == filed, (
        "a cold reader must find a record filed before the index was written"
    )


def test_a_record_filed_before_the_refresh_write_is_not_claimed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh path persists an identity its entries do not cover."""
    run_id = "r-filed-during-the-refresh"
    records_module._store_index(store)
    filed = _file_before_each_write(monkeypatch, store, run_id)

    # A foreign name makes the store reconcile its listing.
    before = store.stat()
    (store / "foreign.json").write_text(json.dumps(_record("r-foreign", HEAD)))
    os.utime(store, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert records_module._record_filed_elsewhere(store, "r-unexplained", None) == (
        None,
        None,
    )

    assert filed, "the fixture filed its record before the refreshed index was written"

    def _cold_lookup(directory: Path, wanted: str) -> Any:
        return records_module._record_filed_elsewhere(directory, wanted, HEAD)

    _cold_reader()
    found = _cold_lookup(store, run_id)
    assert found[0] == filed[0], (
        "a cold reader must find a record filed before the refreshed index was written"
    )


def test_a_cold_reader_of_an_undisturbed_store_stats_nothing_per_record(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adopting a persisted index costs one directory stat, never one per record."""
    records_module._store_index(store)
    _cold_reader()

    counts = {"records": 0, "directories": 0}
    record_stat = records_module._record_stat
    directory_identity = records_module._directory_mtime_ns

    def counting_record_stat(path: Path) -> Any:
        counts["records"] += 1
        return record_stat(path)

    def counting_directory_identity(path: Path) -> Any:
        counts["directories"] += 1
        return directory_identity(path)

    monkeypatch.setattr(records_module, "_record_stat", counting_record_stat)
    monkeypatch.setattr(
        records_module, "_directory_mtime_ns", counting_directory_identity
    )

    entries = records_module._store_index(store).get("r-stored-00003")
    assert entries, "the adopted index still answers for a stored record"
    assert counts["records"] == 0, "no record is stat'ed to adopt a current index"
    assert counts["directories"] == 1, "one directory stat decides adoption"
