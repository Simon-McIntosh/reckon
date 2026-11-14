"""A persisted review index never claims more than it read.

The store index is persisted per store directory in a node-local document that
carries the directory's stat identity beside the entries it was built from. A
later process adopts that document without listing the store when the identity
it sees matches the one recorded, so recording an identity taken *after* the
listing is a silent over-claim: a record filed between the listing and the
write is missing from the entries while the recorded identity covers it, and
every cold reader on the node then adopts an index that does not hold the
record. The invariant the document has to keep is that the identity it records
is never newer than the entries it holds.

Both paths that persist the index are driven here: the load-or-update path in
:func:`_store_indexes`, and the refresh a lookup no held entry explains takes
in :func:`_refresh_store_entries`. Each files a new record in the same
directory between the read and the write, then starts a cold reader, which
must find the record. An undisturbed store still costs a cold reader no
per-record stats.

Every environment-resolved directory is pointed at a temporary path, including
the node-local cache directory, so nothing here reads or writes the operator's
own store or cache.
"""

from __future__ import annotations

import json
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


def _persisted_identity(directory: Path) -> tuple[int, ...]:
    document = json.loads(records_module._index_cache_path(directory).read_text())
    return tuple(int(item) for item in document.get("directory_identity") or ())


def _file_before_each_write(
    monkeypatch: pytest.MonkeyPatch, directory: Path, run_id: str
) -> list[Path]:
    """File one new record the instant before the index document is written.

    The write is where the defect lives: the entries were read from a listing
    that did not hold this record, so anything the document records about the
    directory after this point covers a record its entries do not. Wrapping
    the persist puts the write exactly between the read and the document
    write, which is where a concurrent writer can put it.
    """
    original = records_module._persist_store_entries
    filed: list[Path] = []

    def filing_first(directory_arg: Path, entries: Any, *args: Any) -> None:
        if not filed:
            path = review_module.review_path(PROJECT, run_id)
            _write_record(path, _record(run_id, HEAD))
            filed.append(path)
        original(directory_arg, entries, *args)

    monkeypatch.setattr(records_module, "_persist_store_entries", filing_first)
    return filed


def test_a_record_filed_before_the_index_write_is_not_claimed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-or-update path persists an identity its entries do not cover."""
    run_id = "r-filed-during-the-build"
    filed = _file_before_each_write(monkeypatch, store, run_id)

    records_module._store_index(store)

    assert filed, "the fixture filed its record before the index was written"
    assert _persisted_identity(store) != records_module._store_directory_identity(
        store
    ), "the persisted identity must not be one taken after the listing"

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

    # A lookup no held entry explains is what reaches the refresh.
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
    directory_identity = records_module._store_directory_identity

    def counting_record_stat(path: Path) -> Any:
        counts["records"] += 1
        return record_stat(path)

    def counting_directory_identity(path: Path) -> Any:
        counts["directories"] += 1
        return directory_identity(path)

    monkeypatch.setattr(records_module, "_record_stat", counting_record_stat)
    monkeypatch.setattr(
        records_module, "_store_directory_identity", counting_directory_identity
    )

    entries = records_module._store_index(store).get("r-stored-00003")
    assert entries, "the adopted index still answers for a stored record"
    assert counts["records"] == 0, "no record is stat'ed to adopt a current index"
    assert counts["directories"] == 1, "one directory stat decides adoption"
