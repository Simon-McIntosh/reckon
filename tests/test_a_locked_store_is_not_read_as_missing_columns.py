"""A store that is locked is reported as locked, not as owing its migration.

A reader that cannot describe the runs table because another process holds the
store has learned nothing about the table's columns. Only a table that is
absent reads as carrying none.
"""

from __future__ import annotations

import sqlite3

import pytest

from reckon import run_store


class _Refusing:
    """A connection whose every statement is refused with one message."""

    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError(self.message)


def test_a_locked_store_is_refused_as_locked(tmp_path) -> None:
    with pytest.raises(run_store.UnreadableRunStoreError) as refusal:
        run_store._refuse_owed_migration(
            _Refusing("database is locked"), tmp_path / "run_store.db"
        )

    assert "database is locked" in str(refusal.value)
    assert "owes the schema migration" not in str(refusal.value)


def test_a_recorded_store_without_its_runs_table_owes_its_migration(tmp_path) -> None:
    path = tmp_path / "recorded.db"
    with run_store.RunStore(path):
        pass
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE runs")
        assert run_store._table_columns(connection) == frozenset()
        with pytest.raises(run_store.UnreadableRunStoreError) as refusal:
            run_store._refuse_owed_migration(connection, path)
    finally:
        connection.close()

    assert "owes the schema migration" in str(refusal.value)


def test_a_complete_runs_table_is_read(tmp_path) -> None:
    path = tmp_path / "home" / "run_store.db"
    path.parent.mkdir()
    with run_store.RunStore(path) as store:
        store.append("example", {"run_id": "r-1", "project": "example"})
    connection = sqlite3.connect(path)
    try:
        run_store._refuse_owed_migration(connection, path)
    finally:
        connection.close()
