"""The backend is a query key: one declaration derives the column and its index.

Adding ``backend`` to ``QUERY_KEYS`` makes the column, the index, the insert
extraction and the migration backfill all derive from that one declaration in
``reckon/run_store.py``. These tests open two stores under ``tmp_path``. A
fresh store gains an indexed ``backend`` column that the insert fills from the
record. A store in the legacy shape — where a ``backend`` column already
exists but holds NULL while each row's payload carries the backend, which is
the shape the shared config-home store carried — is backfilled from the
payload when it is opened. Every store resolves to the test's own temporary
path, leaving the real crew config home untouched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from reckon import run_store


@pytest.fixture()
def fresh_store(tmp_path: Path) -> Path:
    """A temporary SQLite path for the fresh-store case."""
    return tmp_path / "fresh_run_store.db"


@pytest.fixture()
def legacy_store(tmp_path: Path) -> Path:
    """A temporary SQLite path for the legacy-store case."""
    return tmp_path / "legacy_run_store.db"


def _record(run_id: str, backend: str, **overrides: object) -> dict:
    """A record carrying the query-key fields a promotion writes."""
    record = {
        "run_id": run_id,
        "project": "proj",
        "member": "worker-a",
        "node": f"{run_id}-node",
        "completed_at": "2026-09-28T00:00:00Z",
        "gate": "passed",
        "backend": backend,
    }
    record.update(overrides)
    return record


def _column_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute('PRAGMA table_info("runs")')}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            "SELECT * FROM sqlite_master WHERE type = 'index' AND tbl_name = 'runs'"
        )
    }


def test_a_fresh_store_insert_fills_an_indexed_backend_column(
    fresh_store: Path,
) -> None:
    record = _record("r-clive", "clive")
    with run_store.RunStore(fresh_store) as store:
        store.append("proj", record)
        store.append("proj", _record("r-codex", "codex"))

    with sqlite3.connect(str(fresh_store)) as connection:
        # A real column, and an index over it, both derived from QUERY_KEYS.
        assert "backend" in _column_names(connection)
        assert "idx_runs_backend" in _index_names(connection)
        # The insert extracted the value from each record.
        rows = {
            str(run_id): backend
            for run_id, backend in connection.execute(
                'SELECT "run_id", "backend" FROM "runs"'
            )
        }
        assert rows == {"r-clive": "clive", "r-codex": "codex"}
        # A reader routing through the column is answered by the index.
        plan = connection.execute(
            'EXPLAIN QUERY PLAN SELECT "run_id" FROM "runs" WHERE "backend" = ?',
            ("clive",),
        ).fetchall()
        assert any("idx_runs_backend" in str(row[3]) for row in plan), plan


def _build_legacy_backend_shape(connection: sqlite3.Connection) -> None:
    """Create the runs table in the shape the shared store carried.

    The earlier declaration kept a ``backend`` column beside a payload blob,
    but nothing extracted the key at insert, so the column holds NULL on every
    row while the payload carries the backend. The table also retains the
    narrow columns (``plan``, ``section``, ``gate``, ``base_sha``) the earlier
    shape declared, so its column set differs from the current one and opening
    it takes the migrating and backfilling path.
    """
    connection.executescript(
        """
        CREATE TABLE "runs" (
            "run_id" TEXT PRIMARY KEY,
            "project" TEXT,
            "member" TEXT,
            "node" TEXT,
            "plan" TEXT,
            "section" TEXT,
            "gate" TEXT,
            "completed_at" TEXT,
            "backend" TEXT,
            "base_sha" TEXT,
            "payload" TEXT
        );
        CREATE INDEX "idx_runs_member" ON "runs" ("member");
        CREATE TABLE "run_details" (
            "run_id" TEXT PRIMARY KEY,
            "detail" TEXT NOT NULL
        );
        CREATE TABLE "members" (
            "member_id" TEXT PRIMARY KEY,
            "payload" TEXT NOT NULL
        );
        CREATE TABLE "holds" (
            "hold_id" TEXT PRIMARY KEY,
            "payload" TEXT NOT NULL
        );
        """
    )
    payload = _record("r-old", "codex")
    payload["plan"] = "history-persists-as-detail-washes-out"
    payload["section"] = "s2"
    payload["base_sha"] = "aaaa1111"
    connection.execute(
        'INSERT INTO "runs" '
        '("run_id", "project", "member", "node", "completed_at", "backend", '
        '"payload") '
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "r-old",
            "proj",
            "worker-a",
            "r-old-node",
            "2026-09-28T00:00:00Z",
            None,  # the decoy: NULL while the payload carries the backend
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ),
    )


def test_a_legacy_store_is_backfilled_from_its_payload_on_open(
    legacy_store: Path,
) -> None:
    with sqlite3.connect(str(legacy_store)) as connection:
        _build_legacy_backend_shape(connection)
        before = connection.execute(
            'SELECT "backend" FROM "runs" WHERE "run_id" = ?', ("r-old",)
        ).fetchone()[0]
    assert before is None, "fixture must start with a NULL backend column"

    # Opening through the current code migrates the table and backfills each
    # row's missing query keys from its payload.
    with run_store.RunStore(legacy_store) as store:
        durable = store.get_run("r-old")

    with sqlite3.connect(str(legacy_store)) as connection:
        after = connection.execute(
            'SELECT "backend" FROM "runs" WHERE "run_id" = ?', ("r-old",)
        ).fetchone()[0]
        found = connection.execute(
            'SELECT "run_id" FROM "runs" WHERE "backend" = ?', ("codex",)
        ).fetchall()
        assert "idx_runs_backend" in _index_names(connection)

    assert after == "codex", "the backfill fills the NULL from the payload"
    assert [str(row[0]) for row in found] == ["r-old"]
    assert durable is not None and durable["backend"] == "codex"
