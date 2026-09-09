"""The run ledger's embedded store, shadowing the committed file.

The committed ledger file (``docs/state/<project>/crew.json``) is the store
every reader reads. This module keeps the same rows in an embedded SQLite
database beside it — for now a shadow that nothing reads, whose writes must
never break the file write. A run row is split into a narrow durable half and
a wide detail half keyed to it; that split is what a later rotation stage can
use to wash wide detail out while every durable field survives.

The durable columns come from the single ``DURABLE_FIELDS`` declaration: the
runs table is created from it and the durable row is inserted from it, so
adding a durable field is one declaration change and appears in both the
schema and every written durable row. The declaration is the only place the
list of durable fields is spelled out.

Nothing here is read by any existing reader, and this module exposes no read
API for run data yet — only the append a promotion needs, the schema it
creates on first use, and the path helper the isolation assertions resolve.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from reckon import _store

# The single durable-field declaration: (column, SQL type). The runs table's
# CREATE and the durable-row INSERT are both built from this tuple, so a field
# added here surfaces in the schema and in every written durable row with no
# second copy to keep in step.
DURABLE_FIELDS = (
    ("run_id", "TEXT"),
    ("project", "TEXT"),
    ("member", "TEXT"),
    ("node", "TEXT"),
    ("plan", "TEXT"),
    ("section", "TEXT"),
    ("gate", "TEXT"),
    ("completed_at", "TEXT"),
    ("backend", "TEXT"),
    ("base_sha", "TEXT"),
)

# The durable columns a query routes through; each gets its own index.
_DURABLE_INDEXED = ("member", "node", "project", "completed_at")


def store_path() -> Path:
    """Return the store's SQLite path under the crew config home."""
    return _store._config_home() / "crew" / "run_store.db"


class RunStore:
    """One embedded SQLite store, creating its schema on first use."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else store_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._create()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _create(self) -> None:
        """Create the tables and indexes, derived from the field declaration."""
        durable = ", ".join(f'"{name}" {sql_type}' for name, sql_type in DURABLE_FIELDS)
        indexes = "".join(
            f'CREATE INDEX IF NOT EXISTS "idx_runs_{name}" ON "runs" ("{name}");'
            for name in _DURABLE_INDEXED
        )
        self._conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS "runs" (
                {durable},
                PRIMARY KEY ("run_id")
            );
            CREATE TABLE IF NOT EXISTS "run_details" (
                "run_id" TEXT PRIMARY KEY,
                "detail" TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS "members" (
                "member_id" TEXT PRIMARY KEY,
                "payload" TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS "holds" (
                "hold_id" TEXT PRIMARY KEY,
                "payload" TEXT NOT NULL
            );
            {indexes}
            """
        )

    def append(self, project: str, record: Mapping[str, Any]) -> None:
        """Insert one run's durable row and its detail in one transaction.

        Only the new run id is written; no existing row is read or rewritten.
        A second insert for the same run id raises (the store's own primary
        keys), the same refusal the committed file makes for a double
        promotion. The transaction covers both inserts, so a failure in either
        leaves the store untouched rather than half-written.
        """
        payload = dict(record)
        payload.setdefault("project", project)
        durable_names = tuple(name for name, _sql_type in DURABLE_FIELDS)
        durable_values = tuple(payload.get(name) for name in durable_names)
        columns = ", ".join(f'"{name}"' for name in durable_names)
        marks = ", ".join("?" for _name in durable_names)
        with self._conn:
            # The interpolated names come only from this module's own field
            # declaration, never from caller input, so there is no injection
            # surface; the flag is the heuristic not seeing that.
            self._conn.execute(
                f'INSERT INTO "runs" ({columns}) VALUES ({marks})',  # noqa: S608
                durable_values,
            )
            self._conn.execute(
                'INSERT INTO "run_details" ("run_id", "detail") VALUES (?, ?)',
                (
                    str(payload["run_id"]),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )

    def append_member(self, member: Mapping[str, Any]) -> None:
        """Record one roster member's full payload under its own id."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO "members" ("member_id", "payload") '
                "VALUES (?, ?)",
                (
                    str(member["id"]),
                    json.dumps(dict(member), sort_keys=True, separators=(",", ":")),
                ),
            )

    def close(self) -> None:
        self._conn.close()

    @property
    def path(self) -> Path:
        return self._path


def append(project: str, record: Mapping[str, Any]) -> None:
    """Append one run to the default store, raising on failure.

    This is the seam a promotion calls and the seam a test injects a failure
    into: a raising store write is recorded by the caller, never propagated,
    so the committed file row a promotion already wrote stays the durable
    result.
    """
    with RunStore() as store:
        store.append(project, record)
