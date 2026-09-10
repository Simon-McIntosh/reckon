"""The run ledger's embedded store, shadowing the committed file.

The committed ledger file (``docs/state/<project>/crew.json``) is the store
every reader reads. This module keeps the same rows in an embedded SQLite
database beside it — for now a shadow that nothing reads, whose writes must
never break the file write. A run row is split into a durable half and a wide
detail half keyed to it; that split is what a later rotation stage can use to
wash wide detail out while every durable field survives.

The classification is inverted so that the failure boundary sits where it
keeps data rather than deleting it: the single ``DETAIL_FIELDS`` declaration
names the wide fields a later rotation may wash out, and every other field on
a run record is durable by default and lands in the runs payload. Forgetting
to classify a new field therefore keeps it forever rather than deleting it,
which is the direction that cannot lose a permanent record. The declaration
is the only place the list of washable fields is spelled out; the durable set
is defined as its complement.

Nothing here is read by any existing reader, and this module exposes no read
API for run data yet beyond the durable/detail readers the rotation contract
asserts against — only the append a promotion needs, the schema it creates on
first use, and the path helper the isolation assertions resolve.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from reckon import _store

# The single detail-field declaration. A field on a run record named here is
# wide detail a later rotation stage may wash out once its derived value
# exists; every field not named here is durable by default and lives in the
# runs row's payload, so an unclassified field is kept forever rather than
# deleted. These are the thirteen wide fields measured at 78.3 percent of the
# ledger's payload at authoring.
DETAIL_FIELDS = (
    "node_definition",
    "budget",
    "unreconciled_override",
    "gate_check",
    "execution_fit",
    "throughput",
    "worktree_retention",
    "lane_receipt",
    "suite_delta",
    "resume_remedy",
    "failure_attribution",
    "shadow_patch",
    "follow_on_paths",
)

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
        """Create the tables and indexes."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS "runs" (
                "run_id" TEXT PRIMARY KEY,
                "payload" TEXT NOT NULL
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
            """
        )

    def append(self, project: str, record: Mapping[str, Any]) -> None:
        """Insert one run's durable payload and its wide detail in one transaction.

        Only the new run id is written; no existing row is read or rewritten.
        A second insert for the same run id raises (the store's own primary
        keys), the same refusal the committed file makes for a double
        promotion. The transaction covers both inserts, so a failure in either
        leaves the store untouched rather than half-written.

        Every field not named in the detail declaration is durable and is
        stored in the runs payload; exactly the declared wide fields the
        record carries are stored as the washable detail hanging off the
        durable half.
        """
        payload = dict(record)
        payload.setdefault("project", project)
        run_id = str(payload["run_id"])
        detail = {name: payload.pop(name) for name in DETAIL_FIELDS if name in payload}
        with self._conn:
            self._conn.execute(
                'INSERT INTO "runs" ("run_id", "payload") VALUES (?, ?)',
                (run_id, json.dumps(payload, sort_keys=True, separators=(",", ":"))),
            )
            self._conn.execute(
                'INSERT INTO "run_details" ("run_id", "detail") VALUES (?, ?)',
                (run_id, json.dumps(detail, sort_keys=True, separators=(",", ":"))),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run's durable half as a dict, or None when absent.

        The durable half is every field the record carried except the declared
        wide detail, so this is the part a rotation must never touch: after a
        rotation deletes the detail row, every field this answers is still
        answerable.
        """
        row = self._conn.execute(
            'SELECT "payload" FROM "runs" WHERE "run_id" = ?', (run_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def get_detail(self, run_id: str) -> dict[str, Any] | None:
        """Return one run's wide detail as a dict, or None when absent.

        The detail is the subset of the declared wide fields the record
        carried; it is what rotation washes out, so it returns None once the
        detail row has been rotated away.
        """
        row = self._conn.execute(
            'SELECT "detail" FROM "run_details" WHERE "run_id" = ?', (run_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

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
