"""A locked run store is retried, reported, and repaired.

The index is a cache over the committed run files, so a write that loses a race
for the store's lock must not cost a run its row: it is retried with a bounded
backoff, a lock past that bound is reported rather than raised, and the missing
row is left for the import the promotion gate runs.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from reckon import ledger, run_store

PROJECT = "proj"
RUN_ID = "r-locked-append"


def _store(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    path = tmp_path / "index.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(path))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)
    with run_store.RunStore():
        pass
    return path


def _record() -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "member": "worker",
        "node": "a-locked-ledger-store-is-retried",
        "completed_at": "2026-11-03T07:20:00Z",
        "outcome": "completed",
    }


def _holding(path: Path, seconds: float) -> threading.Thread:
    """Hold the store's write lock for ``seconds`` on another connection."""
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")

    def release() -> None:
        time.sleep(seconds)
        holder.execute("ROLLBACK")
        holder.close()

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread


def test_a_locked_write_is_attempted_again(tmp_path: Path, monkeypatch) -> None:
    """The retry is the mechanism, so the first refusal must not end the write."""
    _store(tmp_path, monkeypatch)
    attempts: list[int] = []
    real_append = run_store.RunStore.append

    def locked_once(self, project, record):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        real_append(self, project, record)

    monkeypatch.setattr(run_store.RunStore, "append", locked_once)
    monkeypatch.setattr(run_store, "_append_backoff", lambda _attempt: None)
    run_store.append(PROJECT, _record())
    assert len(attempts) == 2
    assert RUN_ID in (run_store.indexed_run_ids(PROJECT) or set())


def test_a_short_lock_is_waited_out(tmp_path: Path, monkeypatch) -> None:
    path = _store(tmp_path, monkeypatch)
    releaser = _holding(path, 0.4)
    try:
        outcome = ledger._index_append(PROJECT, _record())
    finally:
        releaser.join()
    assert outcome == {"status": "written"}
    assert RUN_ID in (run_store.indexed_run_ids(PROJECT) or set())


def test_a_lock_past_the_bound_is_reported_then_imported(
    tmp_path: Path, monkeypatch
) -> None:
    path = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(run_store, "INDEX_RETRY_SECONDS", 0.2)
    releaser = _holding(path, 0.6)
    try:
        outcome = ledger._index_append(PROJECT, _record())
    finally:
        releaser.join()
    assert outcome["status"] == "failed"
    assert RUN_ID not in (run_store.indexed_run_ids(PROJECT) or set())

    source = ledger.run_path(PROJECT, RUN_ID)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(ledger.serialize_run(_record()), encoding="utf-8")
    repaired = run_store.import_ledger(PROJECT)
    assert repaired["rows_imported"] == 1
    assert RUN_ID in (run_store.indexed_run_ids(PROJECT) or set())
