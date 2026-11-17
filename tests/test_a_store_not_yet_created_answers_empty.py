"""Creation is empty until runs exist; a recorded store cannot lose its table."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from reckon import ledger, run_store

PROJECT = "example"


@pytest.fixture()
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "run_store.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(path))
    monkeypatch.setattr(run_store, "RECORD_BUSY_TIMEOUT_SECONDS", 0.01)
    return path


def _read_ledger(root):
    data, _version = ledger.load(PROJECT, root)
    return data["runs"]


@pytest.fixture(params=["rows", "ledger", "index"])
def read_runs(request, tmp_path):
    readers = {
        "rows": lambda: run_store.stored_run_rows(PROJECT),
        "ledger": lambda: _read_ledger(tmp_path),
        "index": lambda: [
            {"run_id": run_id} for run_id in run_store.indexed_run_ids(PROJECT)
        ],
    }
    return readers[request.param]


@pytest.mark.parametrize("metadata", ["absent", "empty", "auxiliary"])
def test_a_store_without_runs_or_a_schema_record_answers_empty(
    store_path, read_runs, metadata
):
    with closing(sqlite3.connect(store_path)) as connection:
        if metadata != "absent":
            connection.execute("CREATE TABLE store_meta (key TEXT, value TEXT)")
        if metadata == "auxiliary":
            connection.execute("INSERT INTO store_meta VALUES ('unrelated', '1')")
            connection.commit()

    before = store_path.read_bytes()
    assert read_runs() == []
    assert store_path.read_bytes() == before


def test_a_recorded_store_without_runs_is_refused_naming_the_migration(
    store_path, read_runs
):
    with run_store.RunStore(store_path):
        pass
    with closing(sqlite3.connect(store_path)) as connection:
        connection.execute("DROP TABLE runs")

    with pytest.raises(run_store.UnreadableRunStoreError, match="schema migration"):
        read_runs()


def test_a_runs_table_missing_payload_is_refused_naming_the_migration(
    store_path, read_runs
):
    with run_store.RunStore(store_path):
        pass
    with closing(sqlite3.connect(store_path)) as connection:
        connection.execute("ALTER TABLE runs DROP COLUMN payload")

    with pytest.raises(run_store.UnreadableRunStoreError) as raised:
        read_runs()
    assert "schema migration" in str(raised.value)
    assert "payload" in str(raised.value)


def test_a_locked_store_is_refused_naming_the_lock(store_path, read_runs):
    with run_store.RunStore(store_path):
        pass
    with closing(sqlite3.connect(store_path)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(run_store.UnreadableRunStoreError) as raised:
                read_runs()
            assert "locked" in str(raised.value)
            assert "migration" not in str(raised.value)
        finally:
            holder.rollback()


def test_a_created_store_answers_with_a_known_run(store_path, read_runs):
    with run_store.RunStore(store_path) as store:
        store.append(PROJECT, {"run_id": "present"})
    assert [row["run_id"] for row in read_runs()] == ["present"]


def test_creation_finishing_between_schema_reads_is_readable(
    store_path, read_runs, monkeypatch
):
    with closing(sqlite3.connect(store_path)):
        pass
    describe = run_store._table_columns
    created = False

    def describe_then_create(connection, schema="main"):
        nonlocal created
        columns = describe(connection, schema)
        if not columns and not created:
            created = True
            with run_store.RunStore(store_path) as store:
                store.append(PROJECT, {"run_id": "present"})
        return columns

    monkeypatch.setattr(run_store, "_table_columns", describe_then_create)
    assert [row["run_id"] for row in read_runs()] == ["present"]
    assert created


def test_every_creation_statement_leaves_readers_an_empty_or_complete_store(
    store_path, tmp_path, monkeypatch
):
    connect = sqlite3.connect
    observations = []
    errors = []

    def observe(statement):
        # A separate connection sees exactly what another process can read.
        try:
            with closing(connect(store_path, timeout=0.01)) as reader:
                tables = {
                    row[0]
                    for row in reader.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                recorded = "store_meta" in tables and bool(
                    reader.execute(
                        "SELECT 1 FROM store_meta WHERE key = ?",
                        (run_store._SCHEMA_RECORD_KEY,),
                    ).fetchone()
                )
            observations.append((statement, "runs" in tables, recorded))
            assert run_store.stored_run_rows(PROJECT) == []
            assert _read_ledger(tmp_path) == []
            assert run_store.indexed_run_ids(PROJECT) == set()
        except Exception as exc:  # noqa: BLE001 - SQLite suppresses callback errors
            # SQLite ignores exceptions from trace callbacks; retain each one.
            errors.append((statement, repr(exc)))

    def connect_with_observer(database, *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        if str(database) == str(store_path):
            observe("opened")
            connection.set_trace_callback(observe)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_with_observer)
    with run_store.RunStore(store_path) as store:
        store._conn.set_trace_callback(None)
        observe("creation complete")
        store.append(PROJECT, {"run_id": "present"})

    assert not errors
    assert all(has_runs or not recorded for _, has_runs, recorded in observations)
    assert {(has_runs, recorded) for _, has_runs, recorded in observations} == {
        (False, False),
        (True, False),
        (True, True),
    }
    assert [row["run_id"] for row in _read_ledger(tmp_path)] == ["present"]
