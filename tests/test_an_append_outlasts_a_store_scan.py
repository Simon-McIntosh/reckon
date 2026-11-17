"""An append keeps retrying for longer than a reader's scan holds the store.

The store keeps a rollback journal, so a reader scanning it holds off every
writer until the scan ends, and a scan of the whole store takes several
seconds. An append that gives up sooner than that loses a promotion each time
it meets a scan.
"""

from __future__ import annotations

import sqlite3

from reckon import run_store

LONGEST_SCAN_SECONDS = 30.0


def test_the_retry_bound_exceeds_a_whole_store_scan() -> None:
    assert run_store.INDEX_RETRY_SECONDS > LONGEST_SCAN_SECONDS


def test_an_append_lands_after_locks_that_outlast_the_old_bound(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store.db"))
    clock = {"now": 0.0}
    attempts = []
    monkeypatch.setattr(run_store.time, "monotonic", lambda: clock["now"])

    def backoff(attempt):
        attempts.append(attempt)
        clock["now"] += 2.0

    monkeypatch.setattr(run_store, "_append_backoff", backoff)

    def write(_store):
        if clock["now"] < 20.0:
            raise sqlite3.OperationalError("database is locked")

    run_store._retrying(write)

    assert clock["now"] >= 20.0
    assert len(attempts) == 10
