"""The store index re-reads only what changed, and survives the process.

A lookup whose record is not filed under the run it reviews is answered from an
index of the store, built once per store directory per process. That index used
to be rebuilt whole whenever the directory's identity moved, which on a busy
fleet is nearly every read, and every fresh process paid the whole pass again:
for one project's store that is thousands of files and tens of megabytes of
JSON, on the path a dispatch, a completion or a recovery takes.

The index is now updated from one directory listing, parsing only the files
whose name is new or whose stat identity has moved, and it is persisted per
store directory in a node-local cache every process on the node shares, so a
fresh process starts from what the last one left instead of parsing the store.

The parse count is the measure, and it is taken on the index's own parse, so
the read a lookup makes of the record it selected — which is still read again
from disk so a rewrite in place is answered current — is outside it.

Every environment-resolved directory is pointed at a temporary path, including
the node-local cache directory, so nothing here reads or writes the operator's
own store or cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon.crew import review as review_module
from reckon.crew import review_records as records_module

PROJECT = "proj"
STORE_SIZE = 300
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
    """Write one record the way the store writes it.

    Through the store's own writer, which replaces the file atomically: a
    rewrite lands as a rename over the path it names, so the record is at the
    same path with a new modification time and the directory's set of entries
    is what a later listing sees.
    """
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


@pytest.fixture()
def parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every file the index's own parse read, in order."""
    counted: list[str] = []
    original = records_module._index_entry

    def counting(path: Path, info: Any) -> dict[str, Any] | None:
        counted.append(str(path))
        return original(path, info)

    monkeypatch.setattr(records_module, "_index_entry", counting)
    return counted


def _lookup(directory: Path, run_id: str, head: str | None = None) -> Any:
    """Ask the index for a record filed under some other run's path."""
    return records_module._record_filed_elsewhere(directory, run_id, head)


def _forget_the_process() -> None:
    """Drop what this process holds, the way a new process would hold nothing."""
    records_module._STORE_INDEXES.clear()


def _from_scratch(directory: Path, run_id: str, head: str | None = None) -> Any:
    """The same lookup answered after the index is discarded and rebuilt."""
    _forget_the_process()
    cache = records_module._index_cache_path(directory)
    if cache.exists():
        cache.unlink()
    return _lookup(directory, run_id, head)


def _also_rebuilt(directory: Path, run_id: str, head: str | None, answered: Any) -> Any:
    """Rebuild the same lookup and assert ``answered`` equals it in full.

    Both sides are the whole result — the path and the record's body, its head
    and its disposition among them — so an index that answers the right path
    from facts it should have re-read turns the caller's case red. Returns the
    rebuilt answer so the caller can also hold it against a written-out record.
    """
    rebuilt = _from_scratch(directory, run_id, head)
    assert answered == rebuilt, "the updated index answers what a rebuild answers"
    return rebuilt


def _store_under(base: Path, run_id: str) -> Path:
    """One record in a store beneath ``base``, which names the store's root."""
    directory = review_module.review_store_root(base) / PROJECT
    directory.mkdir(parents=True)
    _write_record(
        review_module.review_path(PROJECT, run_id, base), _record(run_id, HEAD)
    )
    return directory


def test_the_first_build_parses_every_record_once(
    store: Path, parses: list[str]
) -> None:
    assert _lookup(store, ABSENT) == (None, None)
    assert len(parses) == STORE_SIZE, "the first lookup reads the whole store once"


def test_adding_a_record_parses_only_the_new_file(
    store: Path, parses: list[str]
) -> None:
    assert _lookup(store, ABSENT) == (None, None)
    parses.clear()

    run_id = "r-arrived"
    path = review_module.review_path(PROJECT, "r-reviewer-arrival")
    _write_record(path, _record(run_id, HEAD))

    updated = _lookup(store, run_id, HEAD)
    assert updated[0] == path
    assert len(parses) == 1, "one new file is one parse, not a pass over the store"

    rebuilt = _also_rebuilt(store, run_id, HEAD, updated)
    assert rebuilt == (path, _misfiled(_record(run_id, HEAD)))


def test_rewriting_a_record_parses_only_that_file(
    store: Path, parses: list[str]
) -> None:
    run_id = "r-stored-00007"
    path = review_module.review_path(PROJECT, run_id)
    assert _lookup(store, ABSENT) == (None, None)
    parses.clear()

    rewritten = dict(_record(run_id, _head(7)), total=81)
    _write_record(path, rewritten)

    updated = _lookup(store, run_id, None)
    assert updated[0] == path
    assert len(parses) == 1, "the file that moved is re-read, its siblings are not"

    rebuilt = _also_rebuilt(store, run_id, None, updated)
    assert rebuilt == (path, _misfiled(rewritten)), (
        "the body is the rewritten record, not the one the index was built from"
    )


def test_deleting_a_record_drops_its_entry(store: Path, parses: list[str]) -> None:
    run_id = "r-stored-00011"
    path = review_module.review_path(PROJECT, run_id)
    assert _lookup(store, run_id, None)[0] == path
    parses.clear()

    path.unlink()

    updated = _lookup(store, run_id, None)
    assert updated == (None, None)
    assert parses == [], "a deleted record is dropped from the listing, not parsed"
    assert _also_rebuilt(store, run_id, None, updated) == (None, None)


def test_two_stores_under_different_bases_are_indexed_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parses: list[str]
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "config").mkdir()
    first = _store_under(tmp_path / "first", "r-one")
    second = _store_under(tmp_path / "second", "r-two")
    assert first != second, "one project name under two roots is two store directories"

    first_here = _lookup(first, "r-one", HEAD)
    second_here = _lookup(second, "r-two", HEAD)
    crossed = _lookup(first, "r-two", HEAD)
    crossed_back = _lookup(second, "r-one", HEAD)
    assert first_here[0] is not None
    assert second_here[0] is not None
    assert crossed == (None, None), (
        "a lookup in one store never reads another store's entries"
    )
    assert crossed_back == (None, None)
    assert len(set(parses)) == 2, "each store's own record is the only file read"

    assert _also_rebuilt(first, "r-one", HEAD, first_here)
    assert _also_rebuilt(second, "r-two", HEAD, second_here)
    assert _also_rebuilt(first, "r-two", HEAD, crossed) == (None, None)
    assert _also_rebuilt(second, "r-one", HEAD, crossed_back) == (None, None)


def test_a_fresh_process_parses_nothing_when_nothing_changed(
    store: Path, parses: list[str]
) -> None:
    run_id = "r-stored-00023"
    path = review_module.review_path(PROJECT, run_id)
    assert _lookup(store, run_id, None)[0] == path
    parses.clear()

    _forget_the_process()

    updated = _lookup(store, run_id, None)
    assert updated[0] == path
    assert _lookup(store, ABSENT) == (None, None)
    assert parses == [], "the persisted index answers without parsing a record"

    rebuilt = _also_rebuilt(store, run_id, None, updated)
    assert rebuilt == (path, _misfiled(_record(run_id, _head(23))))


def test_a_persisted_index_under_another_code_stamp_is_rebuilt(
    store: Path, parses: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(records_module, "_INDEX_CODE_STAMP", ["stamp-one"])
    assert _lookup(store, ABSENT) == (None, None)
    assert len(parses) == STORE_SIZE
    parses.clear()

    _forget_the_process()
    monkeypatch.setattr(records_module, "_INDEX_CODE_STAMP", ["stamp-two"])
    assert _lookup(store, ABSENT) == (None, None)
    assert len(parses) == STORE_SIZE, "another code's index is ignored, not trusted"
    parses.clear()

    _forget_the_process()
    answered = _lookup(store, ABSENT)
    assert answered == (None, None)
    assert parses == [], "the rebuild is persisted under the stamp that made it"
    assert _also_rebuilt(store, ABSENT, None, answered) == (None, None)


def test_a_record_changed_between_processes_is_still_found(
    store: Path, parses: list[str]
) -> None:
    """A fresh process holding a persisted index still sees a later write."""
    run_id = "r-incoming"
    assert _lookup(store, ABSENT) == (None, None)
    parses.clear()

    _forget_the_process()
    path = review_module.review_path(PROJECT, "r-reviewer-incoming")
    _write_record(path, _record(run_id, HEAD))

    updated = _lookup(store, run_id, HEAD)
    assert updated[0] == path
    assert len(parses) == 1, "the file added since the index was persisted is parsed"
    rebuilt = _also_rebuilt(store, run_id, HEAD, updated)
    assert rebuilt == (path, _misfiled(_record(run_id, HEAD)))


def _misfiled(record: dict[str, Any]) -> dict[str, Any]:
    """The record as a lookup away from its own path returns it."""
    found = dict(record)
    found["misfiled"] = True
    return found
