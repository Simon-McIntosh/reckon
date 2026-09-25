"""A rebuildable SQLite index of the committed run records.

Run files and remaining aggregate rows, read through ``ledger.load``, are the
authority. A keyed reader checks membership against those sources and rebuilds
an absent or out-of-date index before answering. A cache rebuild cannot make
an authoritative answer depend on a successful cache write.

``DETAIL_FIELDS`` names the wide fields a rotation may remove. Every other
field is durable by default. Refusal stamps are independent durable records:
a refused dispatch has no promoted run file, so rebuilding run rows must never
remove those stamps.
"""

from __future__ import annotations

import json
import logging
import os
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
CREATE TABLE IF NOT EXISTS "refusals" (
    "refused_at" TEXT PRIMARY KEY,
    "payload" TEXT NOT NULL
);
"""


# The single place the store's location is resolved. The default keeps the
# database beside the committed ledger under the crew config home; the
# override moves it wholesale, so a deployment can hold the single-writer
# database on a host-local filesystem while the committed ledger file remains
# the only cross-host truth.
_RUN_STORE_ENV = "RECKON_RUN_STORE"


def store_path() -> Path:
    """Return the store's SQLite path, resolved in one place.

    The default is ``<config-home>/crew/run_store.db`` (the crew config home,
    shared across projects). ``RECKON_RUN_STORE`` overrides the location
    wholesale and always wins, matching the shared override precedence used
    elsewhere in reckon (e.g. ``RECKON_HOME``).
    """
    override = os.environ.get(_RUN_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
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


def _durable_record(project: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """The durable content the store holds for a run record.

    The store never holds the promotion record: ``store_write`` is a record of
    what happened at one promotion, not a statement of current state, so it is
    dropped here and by every store write. ``project`` is injected when the
    record does not carry it, matching the append. The declared wide detail
    lives in the run_details half, so it is excluded from the durable row.
    This is the reference the equality check compares a stored payload against,
    so it must agree field for field with what ``sync_run`` and ``append``
    store.
    """
    record_copy = dict(record)
    record_copy.pop("store_write", None)
    record_copy.setdefault("project", project)
    return {
        name: value for name, value in record_copy.items() if name not in DETAIL_FIELDS
    }


def _split_durable(project: str, record: Mapping[str, Any]) -> tuple[str, dict, dict]:
    """Split a run record into its durable payload and its wide detail.

    The durable payload is the record minus the promotion-record field
    ``store_write``, minus the declared wide detail, with ``project`` injected;
    the detail is exactly the declared wide fields the record carries. Both
    the append and the idempotent sync build their row from this one split so
    the two cannot drift, and the equality check's reference derives from the
    same rules without the split.
    """
    payload = dict(record)
    payload.pop("store_write", None)
    payload.setdefault("project", project)
    run_id = str(payload["run_id"])
    detail = {name: payload.pop(name) for name in DETAIL_FIELDS if name in payload}
    return run_id, payload, detail


class RunStore:
    """One embedded SQLite store, creating its schema on first use."""

    def __init__(
        self, path: str | Path | None = None, *, root: str | Path | None = None
    ) -> None:
        self._root = Path(root).expanduser().resolve() if root is not None else None
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
            # Added columns are nullable because SQLite cannot add a NOT NULL
            # payload without rebuilding the table; the backfill supplies it.
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
        durable record row for row. The durable payload never carries
        ``store_write``: that field is a record of what happened at one
        promotion, not store content.
        """
        run_id, payload, detail = _split_durable(project, record)
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

    def sync_run(self, project: str, record: Mapping[str, Any]) -> str:
        """Ensure one run's row matches the record, returning what happened.

        The outcome is one of ``inserted`` (the run was absent), ``updated``
        (the run was present but its durable payload disagreed and was
        rewritten to match), or ``unchanged`` (the run was present and already
        byte-identical, so it was left untouched). The last case is what makes
        a corpus import re-runnable: once the store holds the committed
        content, a second pass does no work at all. ``store_write`` is never
        part of the stored payload, exactly as in ``append``.
        """
        run_id, payload, detail = _split_durable(project, record)
        encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        encoded_detail = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        with self._conn:
            existing = self._conn.execute(
                'SELECT "payload" FROM "runs" WHERE "run_id" = ?', (run_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    _RUNS_INSERT,
                    (
                        run_id,
                        *(payload.get(name) for name in QUERY_KEYS),
                        encoded_payload,
                    ),
                )
                self._conn.execute(
                    'INSERT INTO "run_details" ("run_id", "detail") VALUES (?, ?)',
                    (run_id, encoded_detail),
                )
                return "inserted"
            if existing[0] == encoded_payload:
                return "unchanged"
            self._conn.execute(
                _RUNS_UPDATE,
                (encoded_payload, *(payload.get(name) for name in QUERY_KEYS), run_id),
            )
            self._conn.execute(
                'INSERT INTO "run_details" ("run_id", "detail") VALUES (?, ?) '
                'ON CONFLICT("run_id") DO UPDATE SET "detail" = excluded."detail"',
                (run_id, encoded_detail),
            )
            return "updated"

    def _ledger_roots(self) -> dict[str, Path | None]:
        """Find ledger owners through state routing and the mount registry."""
        from reckon import flight, ledger

        state = self._root / "docs" / "state" if self._root else _store._state_root()
        roots = (
            {
                entry.name: self._root
                for entry in sorted(state.iterdir())
                if entry.is_dir() and ledger._SAFE_ID.fullmatch(entry.name)
            }
            if state.is_dir()
            else {}
        )
        if self._root is None and not os.environ.get("RECKON_STATE_ROOT"):
            for project, docs in flight.mounted_project_docs().items():
                if project not in roots and (docs / "state" / project).is_dir():
                    roots[project] = docs.parent
        return roots

    def _indexed_records(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT r.run_id, r.payload, d.detail FROM runs r "
            "LEFT JOIN run_details d ON r.run_id = d.run_id"
        )
        return {
            str(run_id): {
                **json.loads(payload),
                **(json.loads(detail) if detail else {}),
            }
            for run_id, payload, detail in rows
        }

    def _refresh(self, project: str | None = None) -> dict[str, dict[str, Any]] | None:
        """Return file-backed answers when membership forced a cache rebuild.

        Only filenames and aggregate ids are read on the matching-index path.
        Unregistered standalone stores retain their own rows: no absent mount
        or unrelated project can authorise deleting a project's cached history.
        """
        from reckon import ledger

        roots = self._ledger_roots()
        changed = {}
        for name, root in roots.items():
            if project is not None and name != project:
                continue
            indexed = {
                str(row[0])
                for row in self._conn.execute(
                    "SELECT run_id FROM runs WHERE project = ?", (name,)
                )
            }
            if indexed != ledger.run_ids(name, root):
                data, _version = ledger.load(name, root)
                changed[name] = data["runs"]
        if not changed:
            return None
        records = {
            run_id: record
            for run_id, record in self._indexed_records().items()
            if record.get("project") not in changed
        }
        for name, rows in changed.items():
            for row in rows:
                run_id, payload, detail = _split_durable(name, row)
                records[run_id] = {**payload, **detail}
        try:
            with self._conn:
                for name, rows in changed.items():
                    self._conn.execute(
                        "DELETE FROM run_details WHERE run_id IN "
                        "(SELECT run_id FROM runs WHERE project = ?)",
                        (name,),
                    )
                    self._conn.execute("DELETE FROM runs WHERE project = ?", (name,))
                    for record in rows:
                        run_id, payload, detail = _split_durable(name, record)
                        self._conn.execute(
                            _RUNS_INSERT,
                            (
                                run_id,
                                *(payload.get(key) for key in QUERY_KEYS),
                                json.dumps(
                                    payload, sort_keys=True, separators=(",", ":")
                                ),
                            ),
                        )
                        self._conn.execute(
                            "INSERT INTO run_details (run_id, detail) VALUES (?, ?)",
                            (
                                run_id,
                                json.dumps(
                                    detail, sort_keys=True, separators=(",", ":")
                                ),
                            ),
                        )
        except sqlite3.Error as exc:
            logging.getLogger(__name__).warning(
                "Cannot rebuild run index %s; answering from committed files: %s",
                self._path,
                exc,
            )
        return records

    def records(self) -> dict[str, dict[str, Any]]:
        """Read complete records for census consumers, refreshing the cache."""
        records = self._refresh()
        return records if records is not None else self._indexed_records()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run's durable half as a dict, or None when absent.

        The durable half is every field the record carried except the declared
        wide detail, so this is the part a rotation must never touch: after a
        rotation deletes the detail row, every field this answers is still
        answerable.
        """
        records = self._refresh()
        if records is not None:
            record = records.get(run_id)
            return _durable_record(str(record["project"]), record) if record else None
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
        records = self._refresh()
        if records is not None:
            record = records.get(run_id)
            return _split_durable(str(record["project"]), record)[2] if record else None
        row = self._conn.execute(
            'SELECT "detail" FROM "run_details" WHERE "run_id" = ?', (run_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def durable_rows(
        self, project: str, *, refresh: bool = True
    ) -> dict[str, dict[str, Any]]:
        """Return {run_id: durable payload} for one project's stored runs.

        The database is shared across every project on this config home, so a
        comparison against one project's committed ledger must scope by the
        project column rather than trusting that all rows belong to the same
        file.
        """
        records = self._refresh(project) if refresh else None
        if records is not None:
            return {
                run_id: _durable_record(project, record)
                for run_id, record in records.items()
                if record.get("project") == project
            }
        rows = self._conn.execute(
            'SELECT "run_id", "payload" FROM "runs" WHERE "project" = ?', (project,)
        ).fetchall()
        return {
            str(run_id): json.loads(payload)
            for run_id, payload in rows
            if payload is not None
        }

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

    def stamp_refusal(self, lane: str, refused_at: str, returns_at: str) -> None:
        """Record one rate-limit refusal with the return time it stated.

        A refused dispatch kills the run, so the refusal is one of the events
        a killed run guarantees never reaches the ledger. The stamp lives in
        this store — outside the reclaimable run directory and outside the
        committed ledger file — keyed by the refusal's own time, so each
        refusal is its own row and a re-stamp of the same refusal is an
        idempotent replace. The return time is stored verbatim as the refusal
        stated it.
        """
        with self._conn:
            self._conn.execute(
                'INSERT INTO "refusals" ("refused_at", "payload") VALUES (?, ?) '
                'ON CONFLICT("refused_at") DO UPDATE SET "payload" = excluded."payload"',
                (
                    refused_at,
                    json.dumps(
                        {
                            "lane": lane,
                            "refused_at": refused_at,
                            "returns_at": returns_at,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

    def refusal_stamps(self) -> list[dict[str, Any]]:
        """Return every recorded refusal stamp, oldest refusal first.

        The stamps are durable: one written before a run directory is
        reclaimed is still answerable afterwards, which is how a later reader
        learns that a lane refused even though the run it killed left no row.
        """
        rows = self._conn.execute(
            'SELECT "payload" FROM "refusals" ORDER BY "refused_at"'
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

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


def indexed_run_ids(project: str) -> set[str] | None:
    """Read current index membership without creating, migrating or rebuilding it.

    None distinguishes a missing index from an existing empty one. A read-only
    connection avoids RunStore's schema setup and reader-triggered refresh, so
    observing lag cannot repair or conceal it.
    """
    path = store_path()
    if not path.exists():
        return None
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT run_id FROM runs WHERE project = ?", (project,)
            )
        }
    finally:
        connection.close()


def import_ledger(
    project: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Bring a project's committed ledger into the store, re-runnable.

    The ledger is resolved by project name through the ordinary ledger loader
    and never opened directly here, so whichever checkout the config home
    routes that project to is the file this reads. Every committed run that is
    absent from the store is inserted; a run already present whose durable
    record disagrees is corrected to match; a run already present and identical
    is left byte-for-byte untouched. The historical ``store_write`` field is
    never written to the store — promotion records stay on the committed file
    rows — and the two reported numbers are the per-pass actions taken, so once
    the store holds the corpus a second pass reports zero of both.

    Returns counts keyed ``rows_imported`` (inserted), ``rows_already_present``
    (present but divergent, corrected) and ``rows_unchanged`` (present and
    already identical).
    """
    from reckon import ledger

    data, _version = ledger.load(project, root)
    counts = {"rows_imported": 0, "rows_already_present": 0, "rows_unchanged": 0}
    with RunStore() as store:
        for record in data["runs"]:
            try:
                outcome = store.sync_run(project, record)
            except sqlite3.IntegrityError:
                # A concurrent promotion landed this run's row between the
                # absent-check and the insert; it is present now, so the retry
                # reports it as already there rather than crashing the import.
                outcome = store.sync_run(project, record)
            if outcome == "inserted":
                counts["rows_imported"] += 1
            elif outcome == "updated":
                counts["rows_already_present"] += 1
            else:
                counts["rows_unchanged"] += 1
    return {"project": project, **counts}


def compare(
    project: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Compare a project's committed ledger against its store rows.

    The file side is the ledger resolved by project name through the ordinary
    ledger loader. The store side is only that project's rows, because the
    database is shared across every project on this config home. Each run's
    durable content — every field the store holds except the declared wide
    detail and the promotion record ``store_write`` — is compared field for
    field, so a field the file carries but the store dropped, or the store
    carries but the file dropped, is a disagreement rather than a chosen
    subset. Reports the three divergence directions as counts (with the run
    ids for diagnosis):

    - ``rows_in_file_absent_from_store``
    - ``rows_in_store_absent_from_file``
    - ``rows_present_in_both_disagreeing``

    The check is the standing measure the dual-write stage is believed on:
    cheap enough for every promotion, and zero on all three directions only
    once the import has brought the corpus in.
    """
    from reckon import ledger

    data, _version = ledger.load(project, root)
    file_durable = {
        str(record.get("run_id")): _durable_record(project, record)
        for record in data["runs"]
    }
    with RunStore() as store:
        store_durable = store.durable_rows(project, refresh=False)
    file_ids = set(file_durable)
    store_ids = set(store_durable)
    file_only = file_ids - store_ids
    store_only = store_ids - file_ids
    disagreeing = {
        run_id
        for run_id in file_ids & store_ids
        if file_durable[run_id] != store_durable[run_id]
    }
    return {
        "rows_in_file_absent_from_store": len(file_only),
        "rows_in_store_absent_from_file": len(store_only),
        "rows_present_in_both_disagreeing": len(disagreeing),
        "file_only_run_ids": sorted(file_only),
        "store_only_run_ids": sorted(store_only),
        "disagreeing_run_ids": sorted(disagreeing),
    }
