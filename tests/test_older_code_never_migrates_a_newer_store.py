"""Older code opens a store newer code has already migrated forward.

A store on disk is shared by every process that opens it, and those processes
do not all run the same revision: a long-lived service keeps the code it
started with while a newer checkout migrates the store it reads. Newer code
adds a declared column, which changes the declaration hash the store records,
so a process running older code finds a record it does not know.

Treating an unknown record as "migrate" was the defect. The older process
decoded and rewrote every row to produce the rows the store already held,
inside one exclusive transaction — the whole store locked against every reader
— and then stamped the store with its own, older declaration. These tests
present that store to `RunStore` and count the rows the migration decodes,
through a stand-in on the one per-row decode the backfill performs. The store
keeps every column and index this code declares, as a store migrated forward
does, and it carries one column this code has never heard of.

The same recorder counts 500 decodes on a store that really is missing a
column, so a zero in the newer-store case is the store saying it did no work
rather than a stand-in that never fired.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from reckon import run_store

# Rows a store holds in these tests: enough that a whole-store backfill is
# unmistakable, and small enough that building one is not the measure.
ROWS = 500

# A declaration version no revision of this code hashes to. It stands for the
# record newer code leaves: a hash this code cannot interpret.
_UNKNOWN_VERSION = "a-declaration-written-by-newer-code"

# The runs table as an earlier declaration left it: every column this code
# declares except activity_seconds, with the payload blob already present.
_OLDER_RUNS = """
CREATE TABLE "runs" (
    "run_id" TEXT PRIMARY KEY,
    "member" TEXT,
    "node" TEXT,
    "project" TEXT,
    "completed_at" TEXT,
    "backend" TEXT,
    "plan" TEXT,
    "revision" TEXT,
    "payload" TEXT
);
CREATE TABLE "run_details" ("run_id" TEXT PRIMARY KEY, "detail" TEXT NOT NULL);
"""


def _record(index: int) -> dict:
    """One run record, in the shape a promotion appends."""
    return {
        "run_id": f"r-{index:04d}",
        "member": "worker-a",
        "node": f"node-{index}",
        "plan": "a-plan",
        "backend": "local",
        "completed_at": "2026-11-11T00:00:00Z",
    }


@pytest.fixture()
def decodes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the rows a run-store migration decodes and rewrites."""
    counted = {"rows": 0}
    original = run_store._legacy_payload

    def counting(row: Mapping[str, Any]) -> dict[str, Any]:
        counted["rows"] += 1
        return original(row)

    monkeypatch.setattr(run_store, "_legacy_payload", counting)
    return counted


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    """A store on a temporary path, built by this code with 500 rows."""
    path = tmp_path / "run_store.db"
    with run_store.RunStore(path) as store:
        for index in range(ROWS):
            store.append("proj", _record(index))
    return path


def _migrated_forward(path: Path) -> None:
    """Leave the store as newer code would: an extra column, an unknown record."""
    with sqlite3.connect(str(path)) as connection:
        connection.execute('ALTER TABLE "runs" ADD COLUMN "scheduled_at" TEXT')
        connection.execute(
            'UPDATE "store_meta" SET "value" = ? WHERE "key" = ?',
            (_UNKNOWN_VERSION, run_store._SCHEMA_RECORD_KEY),
        )
        connection.commit()


def _declared_shape(path: Path) -> tuple[set[str], set[str]]:
    with sqlite3.connect(str(path)) as connection:
        columns = {
            str(row[1]) for row in connection.execute('PRAGMA table_info("runs")')
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                'SELECT "name" FROM sqlite_master WHERE "type" = ?', ("index",)
            )
        }
    return columns, indexes


def _recorded_version(path: Path) -> str | None:
    with sqlite3.connect(str(path)) as connection:
        row = connection.execute(
            'SELECT "value" FROM "store_meta" WHERE "key" = ?',
            (run_store._SCHEMA_RECORD_KEY,),
        ).fetchone()
    return None if row is None else str(row[0])


def test_a_store_written_by_newer_code_is_opened_without_a_backfill(
    store_path: Path, decodes: dict[str, int]
) -> None:
    # The instrument starts at zero: nothing has migrated this store yet.
    assert decodes["rows"] == 0
    _columns_before, indexes_before = _declared_shape(store_path)
    _migrated_forward(store_path)

    # The store is the shape newer code leaves: every column and index this
    # code declares, one column it has never heard of, an unknown record.
    columns, indexes = _declared_shape(store_path)
    assert columns >= run_store._RUNS_COLUMNS
    assert indexes == indexes_before
    assert "scheduled_at" in columns
    assert _recorded_version(store_path) == _UNKNOWN_VERSION

    with run_store.RunStore(store_path):
        pass

    # Opening it decoded no row, added no column, dropped no index, and left
    # the newer code's record alone.
    assert decodes["rows"] == 0
    assert _declared_shape(store_path)[0] == columns
    assert _declared_shape(store_path)[1] == indexes
    assert _recorded_version(store_path) == _UNKNOWN_VERSION


def test_a_store_missing_a_declared_column_is_still_given_and_backfilled(
    tmp_path: Path, decodes: dict[str, int]
) -> None:
    # The store an earlier declaration left: no activity_seconds column, and
    # therefore no record of this code's declaration either.
    path = tmp_path / "older.db"
    with sqlite3.connect(str(path)) as connection:
        connection.executescript(_OLDER_RUNS)
        for index in range(ROWS):
            record = _record(index)
            connection.execute(
                'INSERT INTO "runs" ("run_id", "member", "node", "project", '
                '"completed_at", "backend", "plan", "payload") '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["run_id"],
                    record["member"],
                    record["node"],
                    "proj",
                    record["completed_at"],
                    record["backend"],
                    record["plan"],
                    json.dumps(record),
                ),
            )
        connection.commit()
    columns, _indexes = _declared_shape(path)
    assert "activity_seconds" not in columns
    assert decodes["rows"] == 0

    with run_store.RunStore(path):
        pass

    # The missing column was added and every row of the store was backfilled:
    # the same recorder that counted nothing above counts all 500 here.
    assert decodes["rows"] == ROWS
    with sqlite3.connect(str(path)) as connection:
        columns, _indexes = _declared_shape(path)
        total, unset = connection.execute(
            'SELECT COUNT(*), COUNT(*) FILTER (WHERE "activity_seconds" IS NULL) '
            'FROM "runs"'
        ).fetchone()
        activity_indexes = {
            str(row[0])
            for row in connection.execute(
                'SELECT "name" FROM sqlite_master WHERE "type" = ? AND "sql" LIKE ?',
                ("index", "%activity_seconds%"),
            )
        }
    assert "activity_seconds" in columns
    assert activity_indexes
    assert (total, unset) == (ROWS, 0)
    assert _recorded_version(path) == run_store._STORE_SCHEMA_VERSION


def test_reads_and_appends_on_a_store_written_by_newer_code_still_work(
    store_path: Path, tmp_path: Path
) -> None:
    _migrated_forward(store_path)
    # A root with no state directory keeps every read inside this tree: the
    # store answers, and no ledger file or mount is consulted.
    with run_store.RunStore(store_path, root=tmp_path / "checkout") as store:
        assert store.get_run("r-0000")["run_id"] == "r-0000"
        assert len(store.records()) == ROWS
        appended = _record(ROWS)
        store.append("proj", appended)
        assert store.get_run(appended["run_id"])["run_id"] == appended["run_id"]
        assert len(store.records()) == ROWS + 1

    # The append is durable, and the store still carries newer code's record.
    with sqlite3.connect(str(store_path)) as connection:
        stored = connection.execute(
            'SELECT COUNT(*) FROM "runs" WHERE "run_id" = ?', (f"r-{ROWS:04d}",)
        ).fetchone()[0]
    assert stored == 1
    assert _recorded_version(store_path) == _UNKNOWN_VERSION
