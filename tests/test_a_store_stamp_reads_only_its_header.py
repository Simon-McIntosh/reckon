"""A store stamp reads the header it needs and no filesystem block beyond it.

A buffered reader fills one ``st_blksize`` block before it returns the first
byte. On GPFS that block is 4 MiB, so a 28-byte header read through a buffered
handle costs 4 MiB, and every store read stamps two stores. The stamp must open
the file unbuffered, and must still carry SQLite's file change counter.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reckon import run_store


def _store(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.execute("INSERT INTO t VALUES (1)")
        connection.commit()
    finally:
        connection.close()
    return path


def test_the_stamp_opens_the_store_unbuffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "run_store.db")
    seen: list[tuple[Path, tuple, dict]] = []
    real_open = Path.open

    def recording_open(self: Path, *args, **kwargs):
        seen.append((self, args, kwargs))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    assert run_store.store_stamp(store) is not None
    opens = [entry for entry in seen if entry[0] == store]
    assert opens, "the stamp did not open the store through Path.open"
    for _, args, kwargs in opens:
        buffering = kwargs.get("buffering", args[1] if len(args) > 1 else -1)
        assert buffering == 0, f"store opened with buffering={buffering}"


def test_the_stamp_carries_the_change_counter(tmp_path: Path) -> None:
    store = _store(tmp_path / "run_store.db")
    before = run_store.store_stamp(store)
    header = store.read_bytes()[:28]
    assert before is not None
    assert before[-1] == int.from_bytes(header[24:28], "big")

    connection = sqlite3.connect(store)
    try:
        connection.execute("INSERT INTO t VALUES (2)")
        connection.commit()
    finally:
        connection.close()
    after = run_store.store_stamp(store)
    assert after is not None
    assert after[-1] != before[-1]


def test_an_absent_store_has_no_stamp(tmp_path: Path) -> None:
    assert run_store.store_stamp(tmp_path / "missing.db") is None
