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

The runs row carries that whole durable record as one JSON payload and, beside
it, the five query keys — run id and the four in ``QUERY_KEYS`` — as real
indexed columns. A question routed through one of the keys is answered by an
index search rather than by the whole-table scan plus per-row JSON parse the
flat file costs, while an unclassified field is still kept forever in the
payload. A store created under an older schema is rebuilt in place on open
rather than failing its next write, because ``CREATE TABLE IF NOT EXISTS``
leaves an existing table exactly as the version that created it left it.

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

# The query keys a reader routes through. Each is a real indexed column on the
# runs row, extracted from the payload at insert, so a question about one run
# is answered by an index search rather than by the scan-plus-parse the flat
# file costs. run_id is the fifth query key and is indexed by the table's
# primary key alone. The declaration is the only place the key list is
# spelled out; it drives both the schema and the insert, so the two cannot
# drift.
QUERY_KEYS = ("member", "node", "project", "completed_at")

# The runs table built from QUERY_KEYS. Precomputed once at import so the
# schema, the indexes and the insert all come from the same declaration and
# no caller input ever reaches a string.
_RUNS_COLUMNS = frozenset(("run_id", "payload", *QUERY_KEYS))
_RUNS_COLUMN_DEFS = ", ".join(
    [
        '"run_id" TEXT PRIMARY KEY',
        *(f'"{name}" TEXT' for name in QUERY_KEYS),
        '"payload" TEXT NOT NULL',
    ]
)
_RUNS_CREATE = f'CREATE TABLE IF NOT EXISTS "runs" ({_RUNS_COLUMN_DEFS});'
_RUNS_INDEXES = "".join(
    f'CREATE INDEX IF NOT EXISTS "idx_runs_{name}" ON "runs" ("{name}");'
    for name in QUERY_KEYS
)
_RUNS_SCHEMA = _RUNS_CREATE + _RUNS_INDEXES
_RUNS_INSERT_COLUMNS = ", ".join(
    ['"run_id"', *(f'"{name}"' for name in QUERY_KEYS), '"payload"']
)
_RUNS_INSERT_MARKS = ", ".join("?" for _name in ("run_id", *QUERY_KEYS, "payload"))
_RUNS_INSERT = (
    f'INSERT INTO "runs" ({_RUNS_INSERT_COLUMNS}) VALUES ({_RUNS_INSERT_MARKS})'  # noqa: S608
)
# The migration backfills every column a row is missing. The SET list and the
# values share the QUERY_KEYS declaration, so the two cannot drift.
_RUNS_UPDATE_SET = ", ".join(
    ['"payload" = ?', *(f'"{name}" = ?' for name in QUERY_KEYS)]
)
_RUNS_UPDATE = f'UPDATE "runs" SET {_RUNS_UPDATE_SET} WHERE "run_id" = ?'  # noqa: S608

# The three companion tables have stable shapes of their own and never need
# migrating; only the runs row has drifted across declarations.
_AUX_SCHEMA = """
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


def store_path() -> Path:
    """Return the store's SQLite path under the crew config home."""
    return _store._config_home() / "crew" / "run_store.db"


def _legacy_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild a run's durable record from a row an older runs shape wrote.

    A store that already carried the payload blob keeps that record verbatim;
    a store from the original ten narrow columns has no blob, so the durable
    half is reconstructed from the columns it does hold, dropping the NULLs
    that recorded an absent field. Either way the earlier rows remain
    readable through the durable reader after the migration.
    """
    stored = row.get("payload")
    if stored is not None:
        return json.loads(stored)
    return {name: value for name, value in row.items() if value is not None}


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
        """Create the tables and the run query-key indexes, migrating older runs in place.

        The index claim is literal: each of the query keys in ``QUERY_KEYS``
        gets its own index, so a reader routing through one of them is
        answered by an index search rather than by a scan plus a per-row JSON
        parse, and run_id is indexed by the table's primary key.
        """
        self._conn.executescript(_AUX_SCHEMA)
        self._migrate_runs()

    def _runs_columns(self) -> frozenset[str]:
        return frozenset(
            str(row[1]) for row in self._conn.execute('PRAGMA table_info("runs")')
        )

    def _migrate_runs(self) -> None:
        """Ensure the runs table has the current columns and indexes.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table exactly as the
        version that created it left it, so a store created under an older
        schema — the live store holds 22 rows under the original ten narrow
        columns — would fail every later write with a missing-column error.
        When the columns are not the current set, each missing one is added
        and the whole table is backfilled inside one transaction: a row that
        holds a payload blob feeds the query-key columns from it, and a
        payloadless row from the original narrow shape is rebuilt from its
        own column values. The earlier rows stay readable through the durable
        reader afterwards.
        """
        columns = self._runs_columns()
        if not columns:
            self._conn.executescript(_RUNS_SCHEMA)
            return
        if columns == _RUNS_COLUMNS:
            self._conn.executescript(_RUNS_INDEXES)
            return
        names = [
            str(desc[0])
            for desc in self._conn.execute('SELECT * FROM "runs" LIMIT 0').description
        ]
        rows = self._conn.execute('SELECT * FROM "runs"').fetchall()
        with self._conn:
            # The added columns are nullable, unlike a fresh table's payload;
            # nothing reads the store yet, so the constraint is not worth the
            # rebuild an ALTER that adds NOT NULL would cost.
            for name in (*QUERY_KEYS, "payload"):
                if name not in columns:
                    self._conn.execute(f'ALTER TABLE "runs" ADD COLUMN "{name}" TEXT')
            for row in rows:
                record = _legacy_payload(dict(zip(names, row, strict=True)))
                self._conn.execute(
                    _RUNS_UPDATE,
                    (
                        json.dumps(record, sort_keys=True, separators=(",", ":")),
                        *(record.get(name) for name in QUERY_KEYS),
                        record["run_id"],
                    ),
                )
            self._conn.executescript(_RUNS_INDEXES)

    def append(self, project: str, record: Mapping[str, Any]) -> None:
        """Insert one run's durable record and its wide detail in one transaction.

        Only the new run id is written; no existing row is read or rewritten.
        A second insert for the same run id raises (the store's own primary
        keys), the same refusal the committed file makes for a double
        promotion. The transaction covers both inserts, so a failure in either
        leaves the store untouched rather than half-written.

        Every field not named in the detail declaration is durable and is
        stored in the runs payload; exactly the declared wide fields the
        record carries are stored as the washable detail hanging off the
        durable half. The query keys are real columns beside the payload,
        extracted from it at insert, so the indexed answers agree with the
        durable record row for row.
        """
        payload = dict(record)
        payload.setdefault("project", project)
        run_id = str(payload["run_id"])
        detail = {name: payload.pop(name) for name in DETAIL_FIELDS if name in payload}
        with self._conn:
            self._conn.execute(
                _RUNS_INSERT,
                (
                    run_id,
                    *(payload.get(name) for name in QUERY_KEYS),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
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
