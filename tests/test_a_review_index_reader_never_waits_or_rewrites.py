"""A review-store reader never waits on another process's index lock or rewrites for nothing.

Readers bring the writer-maintained index up to date when the store directory's
identity moves. That reconciliation took the index lock with a blocking flock,
so on 14 November the reckon producer's main thread and two station sweep
threads queued behind one reconciling process and the monitor stalled. And a
reconciliation that found no record changed still rewrote the whole document to
record the new directory identity: about 4.8 MB under a new inode every five
seconds, at an unchanged version.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time

import pytest

from reckon.crew import review_records
from tests import test_the_review_store_is_indexed_by_its_writers as indexed

stored = indexed.stored


@pytest.fixture(autouse=True)
def fresh_settled_identities():
    review_records._SETTLED_IDENTITIES.clear()
    yield
    review_records._SETTLED_IDENTITIES.clear()


def _foreign_record(directory, run_id: str, index: int):
    path = directory / f"{run_id}.at-{index:040x}.json"
    path.write_text(json.dumps(indexed._payload(run_id, index)), encoding="utf-8")
    return path


def test_a_held_index_lock_does_not_hold_a_reader(stored) -> None:
    directory, run_ids, _home = stored
    before = review_records.store_index_document(directory)
    foreign = _foreign_record(directory, run_ids[2], 901)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with (directory / ".review-index" / "lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            held.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        changed = review_records.reconcile_foreign_write(directory)
        document = review_records.store_index_document(directory)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(15)
    assert elapsed < 3.0, elapsed
    assert changed == 0
    assert document is not None
    assert document["version"] == before["version"]
    assert review_records.reconcile_foreign_write(directory) == 1
    assert foreign.name in review_records.store_index_document(directory)["entries"]


def test_a_reconciliation_that_finds_nothing_writes_nothing(stored) -> None:
    directory, _run_ids, _home = stored
    review_records.store_index_document(directory)
    index = directory / ".review-index" / "index.json"
    before = os.stat(index)
    transient = directory / ".a-writer-temporary"
    transient.write_text("x", encoding="utf-8")
    transient.unlink()

    assert review_records.reconcile_foreign_write(directory) == 0
    after = os.stat(index)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    listings: list[str] = []
    real = os.scandir

    def counting(path=".", *args, **kwargs):
        listings.append(str(path))
        return real(path, *args, **kwargs)

    os.scandir = counting
    try:
        assert review_records.reconcile_foreign_write(directory) == 0
    finally:
        os.scandir = real
    assert listings == [], "a settled identity was listed again"
