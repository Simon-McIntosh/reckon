"""Store contention offers a retry; missing or unreadable stores offer repair."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reckon import ledger, run_store


def _assert_lock_refusal(error: ledger.LedgerError, path: Path) -> None:
    message = str(error)
    assert str(path) in message
    assert "locked past the wait bound" in message, message
    assert "promote the run again" in message
    assert "scripts/import_runs_into_store.py" not in message, message


def test_a_held_database_lock_names_contention_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runs.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(path))
    monkeypatch.setattr(run_store, "INDEX_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(run_store, "INDEX_BUSY_TIMEOUT_SECONDS", 0.01)
    with run_store.RunStore():
        pass
    record = {"run_id": "r-held-store"}
    holder = sqlite3.connect(path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ledger.LedgerError) as caught:
            ledger._index_append("proj", record)
        _assert_lock_refusal(caught.value, path)
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert holder.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        holder.rollback()
        holder.close()
    assert ledger._index_append("proj", record) == {"status": "written"}
    assert run_store.indexed_run_ids("proj") == {"r-held-store"}


@pytest.mark.parametrize(
    "detail", ["database is locked", "database table is locked", "database is busy"]
)
def test_sqlite_contention_variants_offer_promotion_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detail: str
) -> None:
    path = tmp_path / "runs.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(path))

    def refuse(_project: str, _record: dict) -> None:
        raise sqlite3.OperationalError(detail)

    monkeypatch.setattr(run_store, "append", refuse)
    with pytest.raises(ledger.LedgerError) as caught:
        ledger._index_append("proj", {"run_id": "r-contention"})
    _assert_lock_refusal(caught.value, path)


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing store"),
        sqlite3.DatabaseError("file is not a database"),
        sqlite3.OperationalError("unable to open database file"),
        RuntimeError("locked metadata is unreadable"),
    ],
)
def test_non_contention_failures_keep_the_store_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    path = tmp_path / "runs.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(path))

    def refuse(_project: str, _record: dict) -> None:
        raise error

    monkeypatch.setattr(run_store, "append", refuse)
    with pytest.raises(ledger.LedgerError) as caught:
        ledger._index_append("proj", {"run_id": "r-unreadable"})
    message = str(caught.value)
    assert str(path) in message
    assert "scripts/import_runs_into_store.py --project proj" in message
    assert "promote the run again" in message
    assert "locked past the wait bound" not in message
    assert caught.value.__cause__ is error
