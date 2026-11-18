"""Stored run history with indexed identity queries and rotating wide detail.

Ledger readers query the store directly. Explicit import and comparison read
committed documents, allowing the store to be rebuilt without asking it for its
own input. ``DETAIL_FIELDS`` names the wide fields rotation may remove; every
other field is durable. Refusal stamps are independent durable records.

Project-scoped roster members, budget holds and their version are authoritative
mutable state, imported once and independent of run-index rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from reckon import _store, run_record

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
    "outside_scope_paths",
)

# The query keys a reader routes through. Each is a real indexed column on the
# runs row, extracted from the record at insert, so a question about one run
# is answered by an index search rather than by the scan-plus-parse the flat
# file costs. run_id is also a query key and is indexed by the table's
# primary key alone. The declaration is the only place the key list is
# spelled out; it drives both the schema and the insert, so the two cannot
# drift.
QUERY_KEYS = ("member", "node", "project", "completed_at", "backend", "plan")


def _query_values(
    payload: Mapping[str, Any],
    detail: Mapping[str, Any],
    *,
    project: str | None = None,
) -> tuple[Any, ...]:
    """Index the same identity the reader derives from the complete record.

    The project is not a field of the record: the runs column carries it and
    every read filters on it, so a write names the project it is filing the row
    under rather than reading one out of the payload. A payload that states a
    project of its own — a row written before the split stopped injecting one,
    or a record a caller stamped itself — still supplies it when the caller
    names none, so a rewrite of an existing row keeps the column it had.
    """
    from reckon.ledger import row_plan

    values = []
    for key in QUERY_KEYS:
        if key == "plan":
            values.append(row_plan({**payload, **detail}))
        elif key == "project" and project is not None:
            values.append(project)
        else:
            values.append(payload.get(key))
    return tuple(values)


# The commit a run's own record file entered the tree at. Nothing routes a
# question through it, so it is not a query key and carries no index; it is a
# declared column of its own because it is a property of the tree rather than
# of the record, and a reader that once read the file out of the tree needs it
# to read the same file again out of history. The record itself cannot supply
# it: a promotion's ``promoted_revision`` is the tip of the run's own work,
# while the commit that adds the record file is the promotion's own commit, so
# the two are different commits by design. Only :func:`backfill_revisions`
# writes the column, from the project's own history.
ROW_REVISION = "revision"

# The runs table built from QUERY_KEYS. Precomputed once at import so the
# schema, the indexes and the insert all come from the same declaration and
# no caller input ever reaches a string.
_RUNS_COLUMNS = frozenset(
    ("run_id", "payload", ROW_REVISION, "activity_seconds", *QUERY_KEYS)
)
_RUNS_COLUMN_DEFS = ", ".join(
    [
        '"run_id" TEXT PRIMARY KEY',
        *(f'"{name}" TEXT' for name in QUERY_KEYS),
        f'"{ROW_REVISION}" TEXT',
        '"payload" TEXT NOT NULL',
        '"activity_seconds" REAL',
    ]
)
_RUNS_CREATE = f'CREATE TABLE IF NOT EXISTS "runs" ({_RUNS_COLUMN_DEFS});'
# Each declared index is named once, so the script that creates it and the
# check that asks whether a store already carries it read the same declaration.
_RUNS_INDEX_DEFS = (
    *((f"idx_runs_{name}", f'"{name}"') for name in QUERY_KEYS),
    ("idx_runs_project_activity", '"project", "activity_seconds"'),
    ("idx_runs_activity", '"activity_seconds"'),
)
_RUNS_INDEX_NAMES = tuple(name for name, _columns in _RUNS_INDEX_DEFS)
_RUNS_INDEXES = "".join(
    f'CREATE INDEX IF NOT EXISTS "{name}" ON "runs" ({columns});'
    for name, columns in _RUNS_INDEX_DEFS
)
_ARCHIVE_RUNS_INDEXES = _RUNS_INDEXES.replace('EXISTS "', 'EXISTS archive."')
_RUNS_SCHEMA = _RUNS_CREATE + _RUNS_INDEXES
# The retired-history tables live in a file of their own, created from the same
# declarations so a retirement moves a row without losing its revision.
_ARCHIVE_RUNS_SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS archive.runs ({_RUNS_COLUMN_DEFS});"
    "CREATE TABLE IF NOT EXISTS archive.run_details "
    '("run_id" TEXT PRIMARY KEY, "detail" TEXT NOT NULL);'
)
_RUNS_INSERT_COLUMNS = ", ".join(
    [
        '"run_id"',
        *(f'"{name}"' for name in QUERY_KEYS),
        f'"{ROW_REVISION}"',
        '"payload"',
        '"activity_seconds"',
    ]
)
_RUNS_INSERT_MARKS = (
    ", ".join("?" for _name in ("run_id", *QUERY_KEYS, ROW_REVISION, "payload"))
    + f", run_activity_seconds(?{len(QUERY_KEYS) + 3})"
)
_RUNS_INSERT = (
    f'INSERT INTO "runs" ({_RUNS_INSERT_COLUMNS}) VALUES ({_RUNS_INSERT_MARKS})'  # noqa: S608
)
_RUNS_ARCHIVE_INSERT = (
    f"INSERT INTO archive.runs ({_RUNS_INSERT_COLUMNS}) "  # noqa: S608
    f"SELECT {_RUNS_INSERT_COLUMNS} FROM main.runs WHERE run_id = ?"
)
# The migration backfills every column a row is missing. The SET list and the
# values share the QUERY_KEYS declaration, so the two cannot drift. The revision
# is COALESCEd rather than assigned: a record that cannot name the commit its
# file entered the tree at must not erase a revision a backfill already found.
_RUNS_UPDATE_SET = ", ".join(
    [
        '"payload" = ?',
        *(f'"{name}" = ?' for name in QUERY_KEYS),
        f'"{ROW_REVISION}" = COALESCE(?, "{ROW_REVISION}")',
        '"activity_seconds" = run_activity_seconds(?1)',
    ]
)
_RUNS_UPDATE = f'UPDATE "runs" SET {_RUNS_UPDATE_SET} WHERE "run_id" = ?'  # noqa: S608

# Mutable collections are keyed by project as well as identity because the
# store is shared across projects. The schema record beside them identifies
# the declaration the run rows were last brought up to.
_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS "run_details" (
    "run_id" TEXT PRIMARY KEY,
    "detail" TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS "members" (
    "project" TEXT NOT NULL DEFAULT '',
    "member_id" TEXT NOT NULL,
    "payload" TEXT NOT NULL,
    PRIMARY KEY ("project", "member_id")
);
CREATE TABLE IF NOT EXISTS "holds" (
    "project" TEXT NOT NULL DEFAULT '',
    "hold_id" TEXT NOT NULL,
    "payload" TEXT NOT NULL,
    PRIMARY KEY ("project", "hold_id")
);
CREATE TABLE IF NOT EXISTS "refusals" (
    "refused_at" TEXT PRIMARY KEY,
    "payload" TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS "retired_runs" (
    "run_id" TEXT PRIMARY KEY,
    "project" TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS "idx_retired_runs_project" ON "retired_runs" ("project");
CREATE TABLE IF NOT EXISTS "store_meta" (
    "key" TEXT PRIMARY KEY,
    "value" TEXT NOT NULL
);
"""

# The store_meta key holding the schema declaration version the store was last
# migrated to.
_SCHEMA_RECORD_KEY = "schema_version"

# The version recorded once a migration has run. It hashes every declaration
# the migration and the schema creation are built from, so a store carrying a
# matching version is already at the shape this code expects and the pass is
# skipped; a changed declaration changes the version, so the pass runs again
# against the store the new code must handle. Hashing the declarations rather
# than carrying a hand-maintained number is what stops the record from
# outliving a schema change: there is no separate constant to forget to bump.
_STORE_SCHEMA_VERSION = hashlib.sha256(
    f"{_AUX_SCHEMA}\n{_RUNS_SCHEMA}\n{_RUNS_UPDATE}".encode()
).hexdigest()


class UnreadableRunStoreError(RuntimeError):
    """A store that exists but cannot answer for itself.

    Raised by the read-only readers — the ones that cannot migrate, rebuild or
    repair — so a damaged file, an unattachable archive or a runs table that
    lacks a column a read selects reaches the caller as the failure it is,
    rather than as a project that happens to hold no runs.
    """


def _unreadable(path: Path, cause: str) -> UnreadableRunStoreError:
    """The one refusal message: which store, and why it could not be read."""
    return UnreadableRunStoreError(f"cannot read the run store at {path}: {cause}")


def _table_columns(
    connection: sqlite3.Connection, schema: str = "main"
) -> frozenset[str]:
    """The columns one runs table carries, empty when the table is absent.

    Only an absent table reads as no columns. Any other refusal, a store
    another process holds locked among them, is raised: a table that could not
    be described has not been shown to lack anything.
    """
    try:
        described = connection.execute(f'SELECT * FROM "{schema}"."runs" LIMIT 0')  # noqa: S608 - a fixed table name
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return frozenset()
    return frozenset(str(desc[0]) for desc in described.description)


def _refuse_owed_migration(connection: sqlite3.Connection, path: Path) -> bool:
    """Refuse a store that owes its migration rather than answer it partially.

    The read-only readers cannot run the backfill themselves: opening the store
    through :class:`RunStore` is a write, and that is what migrates it. A store
    whose ``runs`` table predates the current declaration would otherwise
    answer as though it held fewer runs than it does — a runs table without the
    ``payload`` column reports no record at all while the id columns still list
    every id.

    True means the table is readable. False means neither the table nor a
    schema record exists yet, so creation has no runs to answer with. A schema
    record is written only after the table exists; a recorded store without
    that table is damaged. Complete columns read correctly without a record,
    including when migration stops after committing its backfill.
    """
    try:
        columns = _table_columns(connection)
        if not columns:
            try:
                recorded = connection.execute(
                    "SELECT 1 FROM store_meta WHERE key = ?", (_SCHEMA_RECORD_KEY,)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if str(exc) != "no such table: store_meta":
                    raise
                recorded = None
            if recorded is None:
                return False
            # Creation can finish between the table check and the record read.
            columns = _table_columns(connection)
    except sqlite3.Error as exc:
        raise _unreadable(path, f"{type(exc).__name__}: {exc}") from exc
    missing = sorted(_RUNS_COLUMNS - columns)
    if not missing:
        return True
    raise _unreadable(
        path,
        "it owes the schema migration "
        f"(the runs table has no {', '.join(missing)} column, current "
        f"{_STORE_SCHEMA_VERSION[:12]}); open it through RunStore, which "
        "migrates it before answering",
    )


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing store read-only, refusing one that will not open."""
    try:
        return sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=RECORD_BUSY_TIMEOUT_SECONDS,
        )
    except (OSError, sqlite3.Error) as exc:
        raise _unreadable(path, f"{type(exc).__name__}: {exc}") from exc


# The single place the store's location is resolved. The default keeps the
# database beside the committed ledger under the crew config home; the
# override moves it wholesale, so a deployment can hold the single-writer
# database on a host-local filesystem while the committed ledger file remains
# the only cross-host truth.
_RUN_STORE_ENV = "RECKON_RUN_STORE"

# How long a connection opened for a retried write waits on another writer's
# lock before handing the refusal back. Kept short because the retry owns the
# bound: a five-second busy timeout spends that bound inside the driver's first
# attempt, so the backoff never runs a second one. Every other connection keeps
# the driver's own patience, so a one-off writer behaves exactly as before.
INDEX_BUSY_TIMEOUT_SECONDS = 0.2

#: How long a connection that states no bound of its own waits for a lock
#: another connection holds. The store keeps a rollback journal, so one reader
#: scanning it holds off every writer for as long as the scan runs, and a scan
#: of the whole store takes several seconds. A record write that gave up before
#: such a scan ended lost a promotion; the wait is set well past the longest
#: scan so the write lands once the reader is done.
RECORD_BUSY_TIMEOUT_SECONDS = 120.0

# The bound on retrying an append that lost a race for the store's lock. A
# concurrent writer holds it for milliseconds, but the store keeps a rollback
# journal, so one reader scanning it holds off every writer for as long as the
# scan runs, and a scan of the whole store takes several seconds. A bound
# shorter than one scan refused a promotion on every scan it met; the bound is
# set well past the longest scan so the row is written once the reader is done.
INDEX_RETRY_SECONDS = 120.0
INDEX_RETRY_MAX_DELAY_SECONDS = 0.2

# A scheduled pass bounds both its row transfers and its physical page moves.
RETIREMENT_BATCH_SIZE = 2000
INCREMENTAL_VACUUM_PAGES = 256


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


#: The database header's length up to and including SQLite's file change
#: counter (four bytes at offset 24), which every transaction that writes the
#: file bumps. Read rather than queried, because ``PRAGMA data_version`` counts
#: changes for one connection and a reader that opens its own connection has no
#: answer to compare with the last read.
_STAMPED_HEADER_BYTES = 28


def store_stamp(path: str | Path | None = None) -> tuple[int, ...] | None:
    """A token that moves whenever the store's contents may have changed.

    A reader that keeps an answer derived from the store holds this token
    beside it and reads the store again only when the token is not the one it
    was built from. Two markers rather than one, because each alone leaves a
    hole: a commit that writes into a freed page can land inside the
    filesystem's timestamp tick with the file's size unchanged, and an inserted
    row is that case: the header's counter moves where the stat does not. A
    store replaced wholesale moves the stat even when the replacement's counter
    starts where the old one left off.

    ``None`` means the store is absent, so a caller caches nothing and reads
    uncached — an absent store's answer is not a store's answer.
    """
    target = store_path() if path is None else Path(path)
    try:
        info = target.stat()
        with target.open("rb") as handle:
            header = handle.read(_STAMPED_HEADER_BYTES)
    except OSError:
        return None
    counter = (
        int.from_bytes(header[24:28], "big")
        if len(header) == _STAMPED_HEADER_BYTES
        else -1
    )
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        counter,
    )


def enable_incremental_vacuum(path: str | Path | None = None) -> dict[str, Any]:
    """Prepare an existing store during a separate, one-time maintenance job.

    Converting a populated store requires a full rewrite. Only the explicit
    module command calls this function; a scheduled retention pass never does.
    """
    target = Path(path) if path is not None else store_path()
    if not target.is_file():
        raise FileNotFoundError(target)
    connection = sqlite3.connect(str(target))
    try:
        before = connection.execute("PRAGMA main.auto_vacuum").fetchone()[0]
        pages_before = connection.execute("PRAGMA main.page_count").fetchone()[0]
        if before != 2:
            connection.execute("PRAGMA main.auto_vacuum = INCREMENTAL")
        if before == 0:
            connection.execute("VACUUM main")
        after = connection.execute("PRAGMA main.auto_vacuum").fetchone()[0]
        if after != 2:
            raise sqlite3.DatabaseError(
                "store was not prepared for incremental compaction"
            )
        return {
            "path": str(target),
            "rewritten": before == 0,
            "auto_vacuum_before": before,
            "auto_vacuum_after": after,
            "pages_before": pages_before,
            "pages_after": connection.execute("PRAGMA main.page_count").fetchone()[0],
        }
    finally:
        connection.close()


def _archive_path(path: Path) -> Path:
    """The separate SQLite file holding one store's retired history."""
    return path.with_name(f"{path.stem}.archive{path.suffix}")


def _record_rows(
    connection: sqlite3.Connection,
    *,
    schemas: Iterable[str] = ("main",),
    project: str | None = None,
    run_ids: Iterable[str] | None = None,
    plan: str | None = None,
    node: str | None = None,
    fields: tuple[str, ...] | None = None,
    activity_since: float | None = None,
) -> Iterator[tuple[Any, ...]]:
    """Read selected records through the single durable/detail join.

    Plan and activity selection search their indexed columns. Activity
    predicates consume a buffered narrow projection after its cursor closes,
    then only matching identities fetch the requested payload.
    Python selection, decoding and consumption therefore hold no read lock.
    Encoded rows are buffered per query; identity batches stay below driver limits.
    """
    selected = None if run_ids is None else sorted(set(map(str, run_ids)))
    batches = (
        [None]
        if selected is None
        else [selected[index : index + 500] for index in range(0, len(selected), 500)]
    )
    filtering = activity_since is not None
    if filtering:
        from reckon import ledger

    for schema in schemas:
        if schema not in ("main", "archive"):
            raise ValueError(f"unknown store schema {schema!r}")
        indexed_activity = activity_since is not None and any(
            column[0] == "activity_seconds"
            for column in connection.execute(
                f'SELECT * FROM "{schema}".runs LIMIT 0'  # noqa: S608 - fixed schema
            ).description
        )
        for batch in batches:
            params: list[Any] = []
            columns = []
            if filtering:
                pairs = []
                if activity_since is not None:
                    pairs.extend(
                        f"'{key}', json_extract(r.payload, '$.{key}')"
                        for key in (
                            "launched_at",
                            "created_at",
                            "dispatched_at",
                            "completed_at",
                        )
                    )
                columns = [f"json_object({','.join(pairs)})", "NULL"]
            else:
                for column in ("r.payload", "d.detail"):
                    if fields == ("run_id",) or (
                        column == "d.detail"
                        and fields is not None
                        and not any(field in DETAIL_FIELDS for field in fields)
                    ):
                        columns.append("NULL")
                    elif fields is None:
                        columns.append(column)
                    else:
                        marks = ",".join("?" for _ in fields)
                        columns.append(
                            "(SELECT json_group_object(key, CASE "  # noqa: S608 - fixed columns and parameter markers
                            "WHEN type IN ('object','array') THEN json(value) "
                            "WHEN type IN ('true','false') THEN json(type) ELSE value END) "
                            f"FROM json_each({column}) WHERE key IN ({marks}))"
                        )
                        params.extend(fields)
            query = (
                f'SELECT r.run_id, {columns[0]}, {columns[1]} FROM "{schema}".runs r '  # noqa: S608 - fixed schema and column names
            )
            if plan and batch is None and not node:
                query += "INDEXED BY idx_runs_plan "
            elif indexed_activity and batch is None and not node:
                # Force the range index rather than a project-only scan when
                # the planner has no statistics for the recent population.
                query += (
                    "INDEXED BY idx_runs_project_activity "
                    if project is not None
                    else "INDEXED BY idx_runs_activity "
                )
            query += f'LEFT JOIN "{schema}".run_details d ON r.run_id = d.run_id'
            clauses = []
            if project is not None:
                # Identity and node lookups must not fall back to walking the
                # project's history after the activity range selected its ids.
                clauses.append(
                    "+r.project = ?" if batch is not None or node else "r.project = ?"
                )
                params.append(project)
            if batch is not None:
                clauses.append(f"r.run_id IN ({','.join('?' for _ in batch)})")
                params.extend(batch)
            if node:
                # The node column is indexed, so one node's rows are found
                # without visiting any other row's payload.
                clauses.append("r.node = ?")
                params.append(node)
            if plan:
                clauses.append("r.plan = ?")
                params.append(plan)
            if indexed_activity:
                clauses.append("r.activity_seconds >= ?")
                params.append(activity_since)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            # Range indexes also compete for unbounded project reads. Keep
            # insertion order explicit whichever index supplies the rows.
            query += " ORDER BY r.rowid"
            cursor = connection.execute(query, params)
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if filtering:
                identities = []
                for identity, payload, _detail in rows:
                    record = json.loads(payload)
                    if activity_since is not None:
                        moment = ledger.run_activity_seconds(record)
                        if moment is None or moment < activity_since:
                            continue
                    identities.append(identity)
                # Fetch by identity only after predicate work has released the
                # cursor, retaining the scan order even across identity batches.
                matching = {
                    row[0]: row
                    for row in _record_rows(
                        connection,
                        schemas=(schema,),
                        project=project,
                        run_ids=identities,
                        node=node,
                        fields=fields,
                    )
                }
                rows = [
                    matching[identity]
                    for identity in identities
                    if identity in matching
                ]
            yield from rows


def record_file_additions(
    checkout: str | Path,
    project: str,
    *,
    base: str | None = None,
    head: str | None = None,
) -> list[tuple[str, str, int]]:
    """Each commit that added a run's own record file, oldest first.

    A run's ``docs/state/<project>/runs/<run_id>.json`` is added once, and a
    path-limited log lists those additions without generating a patch, so this
    is one git call for the whole question rather than one per run. The result
    is ``(run_id, commit, epoch)`` per addition, in log order, where ``epoch``
    is the adding commit's own timestamp: the backfill only needs the commit,
    while a windowed reader compares the epoch against its bounds before it
    trusts an appearance.

    ``base`` and ``head`` narrow the log to ``base..head``; omitted, the whole
    first-parent history is read. A checkout that cannot answer — no git, or no
    history for the path — returns nothing, so a caller reports an unresolved
    run rather than stamping a caller's guess.
    """
    argv = [
        "git",
        "-C",
        str(checkout),
        "log",
        "--first-parent",
        "--reverse",
        "--diff-merges=first-parent",
        "--format=%x1e%H%x09%ct",
        "--diff-filter=A",
        "--name-only",
    ]
    if base is not None and head is not None:
        argv.append(f"{base}..{head}")
    argv.extend(["--", f"docs/state/{project}/runs"])
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    additions: list[tuple[str, str, int]] = []
    commit, epoch = "", 0
    # Newlines only: ``str.splitlines`` also splits on the record separator the
    # format string leads each commit with, which would drop the marker the
    # commit sha sits behind.
    for line in completed.stdout.split("\n"):
        if line.startswith("\x1e"):
            commit, value = line[1:].split("\t")
            epoch = int(value)
            continue
        name = line.strip()
        if commit and name.endswith(".json"):
            additions.append((name.rsplit("/", 1)[-1][: -len(".json")], commit, epoch))
    return additions


def _record_file_revisions(checkout: Path, project: str) -> dict[str, str]:
    """Map each run id to the commit its own record file entered the tree at.

    The scan is :func:`record_file_additions`; this only keeps the first
    appearance per run, which is the adding commit because the log runs
    oldest first.
    """
    revisions: dict[str, str] = {}
    for run_id, commit, _epoch in record_file_additions(checkout, project):
        revisions.setdefault(run_id, commit)
    return revisions


def backfill_revisions(
    project: str,
    *,
    root: str | Path | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Stamp each stored run with the commit its own record file entered the tree at.

    No record can name it either: a promotion's ``promoted_revision`` is the tip
    of the run's own work, and the promotion commits the record file in its own
    commit, so the record and the tree name different commits by design. Every
    row therefore starts empty and this pass, because the adding commit is a
    property of the project's history rather than of the record, is the only
    thing that writes the column — read from the checkout and stamped on those
    rows.

    ``root`` names that checkout and falls back to the store's own routing for
    the project. Reads are idempotent: a row that already names a revision is
    left alone, so a repeated pass stamps nothing.

    Returns counts keyed ``revision_stamped`` (rows this pass placed) and
    ``revision_unresolved`` (rows whose file never entered the tree this
    checkout can see).
    """
    with RunStore(path) as store:
        missing = store.revisionless_rows(project)
        if not missing:
            return {"revision_stamped": 0, "revision_unresolved": 0}
        checkout = (
            Path(root) if root is not None else store._ledger_roots().get(project)
        )
        if checkout is None or not Path(checkout).is_dir():
            return {"revision_stamped": 0, "revision_unresolved": len(missing)}
        found = _record_file_revisions(Path(checkout), project)
        stamped = store.stamp_revisions(
            project, {run_id: found[run_id] for run_id in missing if run_id in found}
        )
        return {
            "revision_stamped": stamped,
            "revision_unresolved": len(missing) - stamped,
        }


def _read_attached_tables(connection: sqlite3.Connection, path: Path) -> list[str]:
    """The tables one store's runs may answer from, hot first then retired.

    An archive that exists but cannot be attached raises rather than being
    passed over. Continuing against the main database alone would answer this
    project's history without its retired rows, which is the loss the read is
    meant to prevent; the attach is also probed, because SQLite attaches a file
    lazily and a damaged archive would otherwise fail inside the record read
    with the failure charged to the main store.
    """
    tables = ["main"]
    archive = _archive_path(path)
    if archive.is_file():
        try:
            connection.execute(
                "ATTACH DATABASE ? AS archive",
                (f"{archive.resolve().as_uri()}?mode=ro",),
            )
            connection.execute("SELECT 1 FROM archive.runs LIMIT 0").fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise _unreadable(
                path, f"its archive {archive} cannot be attached: {exc}"
            ) from exc
        tables.append("archive")
    return tables


def stored_run_revisions(
    project: str,
    *,
    path: str | Path | None = None,
) -> dict[str, str] | None:
    """Each stored run's revision, read without writing.

    ``None`` distinguishes an absent store from one holding no revision for
    this project's runs. The connection is opened read-only, so a history read
    never creates, migrates or rebuilds the store it reads: a caller resolving
    a revision must not change the store that holds it.

    The revision is the commit a run's own record file entered the tree at, so
    a reader whose tree no longer carries the file can still read it from
    history. A row that names none is absent from the answer rather than
    answered with an empty string, so a caller can tell a run the store cannot
    place from one it placed at the empty tree.
    """
    target = store_path() if path is None else Path(path)
    if not target.is_file():
        return None
    connection = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    revisions: dict[str, str] = {}
    try:
        for table in _read_attached_tables(connection, target):
            try:
                rows = connection.execute(
                    f"SELECT run_id, revision FROM {table}.runs "  # noqa: S608 - fixed table names
                    "WHERE project = ? AND revision IS NOT NULL",
                    (project,),
                ).fetchall()
            except sqlite3.Error:
                # A store whose runs table predates the column names no
                # revision, and one that has no runs table holds no run.
                continue
            for run_id, revision in rows:
                if revision:
                    revisions.setdefault(str(run_id), str(revision))
    finally:
        connection.close()
    return revisions


def stored_run_payloads(
    project: str,
    *,
    path: str | Path | None = None,
    activity_since: float | None = None,
) -> tuple[tuple[Any, ...], Iterator[tuple[Any, ...]]] | None:
    """Return a change token and lazy, encoded durable/detail rows.

    A caller memoising a derivation can inspect the token without opening a
    connection, and consume the rows only when that token changes. The token
    includes both the hot store and its archive. Each row is the run id and
    the two JSON strings, through the same join as :func:`stored_run_rows`.
    Indexed activity selection reads no timestamp JSON; stores without that
    column use the shared reader's timestamp predicate.

    None means the store is absent. An unreadable store or archive raises on
    consumption. The entire selection is buffered and the connection closed
    before yielding any row, so consumers hold no read lock while decoding.
    """
    target = store_path() if path is None else Path(path)
    if not target.exists():
        return None
    token = (store_stamp(target), store_stamp(_archive_path(target)))

    def read() -> Iterator[tuple[Any, ...]]:
        connection = None
        try:
            connection = sqlite3.connect(
                f"{target.resolve().as_uri()}?mode=ro", uri=True
            )
            rows = []
            for schema in _read_attached_tables(connection, target):
                parameters: dict[str, Any] = {"activity_since": activity_since}
                if activity_since is not None:
                    columns = connection.execute(
                        f'SELECT * FROM "{schema}".runs LIMIT 0'  # noqa: S608 - store-owned schema names
                    ).description
                    if any(column[0] == "activity_seconds" for column in columns):
                        identities = [
                            row[0]
                            for row in connection.execute(
                                f'SELECT run_id FROM "{schema}".runs '  # noqa: S608 - store-owned schema names
                                "WHERE project = ? AND activity_seconds >= ?",
                                (project, activity_since),
                            ).fetchall()
                        ]
                        parameters = {"run_ids": identities}
                rows.extend(
                    _record_rows(
                        connection, schemas=(schema,), project=project, **parameters
                    )
                )
        except sqlite3.Error as error:
            raise RuntimeError(
                f"cannot read the run store at {target}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        yield from rows

    return token, read()


def stored_run_rows(
    project: str,
    *,
    path: str | Path | None = None,
    run_ids: Iterable[str] | None = None,
    plan: str | None = None,
    node: str | None = None,
    fields: tuple[str, ...] | None = None,
    activity_since: float | None = None,
) -> list[dict[str, Any]] | None:
    """Read selected stored history without writing or consulting run files.

    None means the store is absent. An existing empty store answers an empty
    list; an unreadable store raises :class:`UnreadableRunStoreError` naming the
    store and the cause rather than looking like an empty project. Retired rows
    retain both halves of their record in the attached archive.

    ``path`` names the store to read and falls back to the resolved location,
    so a caller comparing two stores — or a probe holding its own fixture —
    reads the one it named rather than whichever store this machine routes to.
    """
    target = store_path() if path is None else Path(path)
    if not target.exists():
        return None
    connection = _open_readonly(target)
    try:
        if not _refuse_owed_migration(connection, target):
            return []
        schemas = _read_attached_tables(connection, target)
        records = []
        for run_id, payload, detail in _record_rows(
            connection,
            schemas=schemas,
            project=project,
            run_ids=run_ids,
            plan=plan,
            node=node,
            fields=fields,
            activity_since=activity_since,
        ):
            if fields == ("run_id",):
                records.append({"run_id": str(run_id)})
            else:
                records.append(
                    run_record.normalize_outside_scope_paths(
                        {
                            **json.loads(payload),
                            **(json.loads(detail) if detail else {}),
                        }
                    )
                )
        return records
    except sqlite3.Error as error:
        raise _unreadable(target, f"{type(error).__name__}: {error}") from error
    finally:
        connection.close()


def _decoded_record(stored: str) -> dict[str, Any]:
    """Decode a stored payload, mapping a retired outside-scope key on the way in.

    A row written before the ledger key was renamed carries the old spelling;
    the store's reader presents it under the current key, so a caller sees one
    key whichever revision wrote the row.
    """
    return run_record.normalize_outside_scope_paths(json.loads(stored))


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
        return _decoded_record(stored)
    return run_record.normalize_outside_scope_paths(
        {name: value for name, value in row.items() if value is not None}
    )


def _answer_identity(record: Mapping[str, Any], project: str) -> dict[str, Any]:
    """A stored record answered as it was written, with the project it is filed under.

    The payload is the record the promotion wrote, so it is answered as it
    stands rather than re-split: a row written before the write stopped
    injecting the project states one of its own, and a row written since does
    not. The column supplies it when the record does not, so both answer the
    identity the file is filed under.
    """
    answer = dict(record)
    answer.setdefault("project", project)
    return answer


def _durable_record(project: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """The durable content the store holds for a run record.

    The store never holds the promotion record: ``store_write`` is a record of
    what happened at one promotion, not a statement of current state, so it is
    dropped here and by every store write. The project a row is filed under is
    the runs column rather than a field of the record, so ``project`` is
    declared onto the answer here — the file the promotion wrote does not carry
    it, and a reader comparing the two needs the store to say which project's
    history it is answering. The declared wide detail
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


def _split_durable(record: Mapping[str, Any]) -> tuple[str, dict, dict]:
    """Split a run record into its durable payload and its wide detail.

    The durable payload is the record minus the promotion-record field
    ``store_write``, minus the declared wide detail; the detail is exactly the
    declared wide fields the record carries. The payload is the record and
    nothing more: the project a row is filed under is the runs column, written
    from the caller's own argument, so a record promoted without one is stored
    without one. Both the append and the idempotent sync build their row from
    this one split so the two cannot drift.
    """
    payload = dict(record)
    payload.pop("store_write", None)
    run_id = str(payload["run_id"])
    detail = {name: payload.pop(name) for name in DETAIL_FIELDS if name in payload}
    return run_id, payload, detail


def _encoded_auxiliary_rows(
    connection: sqlite3.Connection, project: str
) -> dict[str, list[tuple[str]]]:
    encoded = {}
    for table in ("members", "holds"):
        cursor = connection.execute(
            f'SELECT "payload" FROM "{table}" WHERE "project" = ? ORDER BY rowid',  # noqa: S608 - fixed auxiliary table names
            (project,),
        )
        try:
            encoded[table] = cursor.fetchall()
        finally:
            cursor.close()
    return encoded


def _decode_auxiliary_rows(
    rows: Mapping[str, list[tuple[str]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [json.loads(row[0]) for row in encoded]
        for table, encoded in rows.items()
    }


def _auxiliary_rows(
    connection: sqlite3.Connection, project: str
) -> dict[str, list[dict[str, Any]]]:
    return _decode_auxiliary_rows(_encoded_auxiliary_rows(connection, project))


def read_auxiliary(project: str) -> tuple[dict[str, Any], int] | None:
    """Read an imported mutable snapshot without creating or migrating a store.

    An absent import marker leaves the aggregate as the readable projection.
    Explicit mutable access imports it before any registration or hold write.
    """
    path = store_path()
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'store_meta'"
        ).fetchone():
            return None
        found = connection.execute(
            'SELECT "value" FROM "store_meta" WHERE "key" = ?',
            (f"auxiliary:{project}",),
        ).fetchone()
        if found is None:
            return None
        encoded = _encoded_auxiliary_rows(connection, project)
        version = int(found[0])
    finally:
        connection.close()
    return _decode_auxiliary_rows(encoded), version


def _payload_activity_seconds(payload: str) -> float | None:
    """Cache the ledger's timestamp semantics once per inserted or amended row."""
    from reckon import ledger

    return ledger.run_activity_seconds(json.loads(payload))


class RunStore:
    """One embedded SQLite store, creating its schema on first use."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        root: str | Path | None = None,
        busy_timeout: float | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve() if root is not None else None
        self._path = Path(path) if path is not None else store_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            timeout=RECORD_BUSY_TIMEOUT_SECONDS
            if busy_timeout is None
            else busy_timeout,
        )
        self._conn.create_function(
            "run_activity_seconds", 1, _payload_activity_seconds, deterministic=True
        )
        if self._conn.execute("PRAGMA main.page_count").fetchone()[0] == 0:
            self._conn.execute("PRAGMA main.auto_vacuum = INCREMENTAL")
        self._archive_attached = False
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

        The run migration is owed for a store that lacks something this code
        declares. Once such a pass completes it records
        ``_STORE_SCHEMA_VERSION`` beside the store's rows, and every later open
        on a store carrying that record skips the question: the companion
        tables are cheap to re-assert, while the runs backfill decodes and
        rewrites every row and is the whole cost of opening the store.

        A store recorded against any other version is asked what it lacks,
        and a store that lacks nothing is left exactly as it is, recorded
        version included. That case is not hypothetical: a process running
        older code opens a store newer code has already migrated forward, so
        the store carries every column this code declares plus the ones added
        since. Migrating it would decode and rewrite every row to produce the
        rows it already holds, and rewriting its record would stamp the store
        as this older code's declaration. Only a missing column or index — or
        a missing table — is work.
        """
        self._migrate_auxiliary()
        self._conn.executescript(_AUX_SCHEMA)
        if self._recorded_schema_version() == _STORE_SCHEMA_VERSION:
            return
        if not self._lacks_declaration():
            return
        self._migrate_runs()
        if self.archive_path.is_file():
            self._attach_archive(create=True)
        self._record_schema_version()

    def _migrate_auxiliary(self) -> None:
        """Preserve unscoped rows while making mutable identities project-local."""
        tables = (("members", "member_id"), ("holds", "hold_id"))

        def needs_migration(table: str) -> bool:
            columns = {
                row[1] for row in self._conn.execute(f'PRAGMA table_info("{table}")')
            }
            return bool(columns) and "project" not in columns

        if not any(needs_migration(table) for table, _identity in tables):
            return
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            for table, identity in tables:
                if not needs_migration(table):
                    continue
                self._conn.execute(
                    f'ALTER TABLE "{table}" RENAME TO "unscoped_{table}"'
                )
                self._conn.execute(
                    f'CREATE TABLE "{table}" ("project" TEXT NOT NULL DEFAULT \'\', '
                    f'"{identity}" TEXT NOT NULL, "payload" TEXT NOT NULL, '
                    f'PRIMARY KEY ("project", "{identity}"))'
                )
                self._conn.execute(
                    f'INSERT INTO "{table}" ("project", "{identity}", "payload") '  # noqa: S608 - fixed auxiliary table names
                    f'SELECT \'\', "{identity}", "payload" FROM "unscoped_{table}"'
                )
                self._conn.execute(f'DROP TABLE "unscoped_{table}"')

    def _auxiliary_rows(self, project: str) -> dict[str, list[dict[str, Any]]]:
        return _auxiliary_rows(self._conn, project)

    def _replace_auxiliary(self, project: str, data: Mapping[str, Any]) -> None:
        for table, identity, field in (
            ("members", "member_id", "id"),
            ("holds", "hold_id", "hold_id"),
        ):
            if table not in data:
                continue
            self._conn.execute(
                f'DELETE FROM "{table}" WHERE "project" = ?',  # noqa: S608 - fixed auxiliary table names
                (project,),
            )
            self._conn.executemany(
                f'INSERT INTO "{table}" ("project", "{identity}", "payload") VALUES (?, ?, ?)',  # noqa: S608 - fixed table and column names
                [
                    (project, str(row[field]), json.dumps(dict(row), sort_keys=True))
                    for row in data[table]
                ],
            )

    def auxiliary(
        self, project: str, seed: Callable[[], tuple[dict[str, Any], int]]
    ) -> tuple[dict[str, Any], int]:
        """Import once, then read roster, holds and their version in one transaction.

        The marker survives an empty roster or hold history. An aggregate in
        another checkout can therefore never resurrect deliberately removed rows.
        """
        key = f"auxiliary:{project}"
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            found = self._conn.execute(
                'SELECT "value" FROM "store_meta" WHERE "key" = ?', (key,)
            ).fetchone()
            if found is None:
                data, version = seed()
                self._replace_auxiliary(project, data)
                self._conn.execute(
                    'INSERT INTO "store_meta" ("key", "value") VALUES (?, ?)',
                    (key, str(version)),
                )
            else:
                version = int(found[0])
            encoded = _encoded_auxiliary_rows(self._conn, project)
        return _decode_auxiliary_rows(encoded), version

    def write_auxiliary(
        self, project: str, data: Mapping[str, Any], expected_version: int
    ) -> int:
        """Compare and replace mutable collections atomically, never run records."""
        key = f"auxiliary:{project}"
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            found = self._conn.execute(
                'SELECT "value" FROM "store_meta" WHERE "key" = ?', (key,)
            ).fetchone()
            if found is None:
                raise ValueError(
                    f"mutable state for {project!r} must be imported first"
                )
            version = int(found[0])
            if expected_version != version:
                encoded = _encoded_auxiliary_rows(self._conn, project)
            else:
                self._replace_auxiliary(project, data)
                self._conn.execute(
                    'UPDATE "store_meta" SET "value" = ? WHERE "key" = ?',
                    (str(version + 1), key),
                )
        if expected_version != version:
            raise _store.VersionConflict(
                expected_version, version, _decode_auxiliary_rows(encoded)
            )
        return version + 1

    def _recorded_schema_version(self) -> str | None:
        """Return the declaration version this store was last migrated to."""
        row = self._conn.execute(
            'SELECT "value" FROM "store_meta" WHERE "key" = ?', (_SCHEMA_RECORD_KEY,)
        ).fetchone()
        return None if row is None else str(row[0])

    def _record_schema_version(self) -> None:
        """Record that the store now carries the shape this code expects.

        Written only after the migration returns, in its own transaction, so
        an interrupted pass leaves no record and the next open runs it again.
        """
        with self._conn:
            self._conn.execute(
                'INSERT INTO "store_meta" ("key", "value") VALUES (?, ?) '
                'ON CONFLICT("key") DO UPDATE SET "value" = excluded."value"',
                (_SCHEMA_RECORD_KEY, _STORE_SCHEMA_VERSION),
            )

    def _runs_columns(self, table: str = "runs") -> frozenset[str]:
        """The columns of one runs table, empty when the table is absent."""
        try:
            described = self._conn.execute(f"SELECT * FROM {table} LIMIT 0")  # noqa: S608 - a fixed table name
        except sqlite3.OperationalError:
            return frozenset()
        return frozenset(str(desc[0]) for desc in described.description)

    def _lacks_declaration(self) -> bool:
        """True when the store lacks a runs column or index this code declares.

        The question is coverage, not equality. A store written by newer code
        holds every column this code declares beside the ones added since, and
        that store is complete for this code: there is nothing to add and
        nothing to backfill. Equality would call such a store different and
        rewrite all of it, which is how older code came to migrate a newer
        store. A runs table that is absent entirely is also coverage the store
        lacks, so its answer is True rather than an error.
        """
        for table, schema in (("runs", "main"), ("archive.runs", "archive")):
            if schema == "archive" and not self._attach_archive():
                continue
            columns = self._runs_columns(table)
            if not columns >= _RUNS_COLUMNS:
                return True
            present = {
                str(row[0])
                for row in self._conn.execute(
                    # The schema name is this method's own literal, not caller input.
                    f'SELECT "name" FROM {schema}.sqlite_master WHERE "type" = ?',  # noqa: S608
                    ("index",),
                )
            }
            if not set(_RUNS_INDEX_NAMES) <= present:
                return True
        return False

    def _migrate_runs(self, table: str = "runs") -> None:
        """Ensure one runs table has the columns this code expects.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table exactly as the
        version that created it left it, so a store created under an older
        schema — the live store holds 22 rows under the original ten narrow
        columns — would fail every later write with a missing-column error.
        When a declared column is missing, it is added and the whole table is
        backfilled inside one transaction: a row that holds a payload blob
        feeds the query-key columns from it, and a payloadless row from the
        original narrow shape is rebuilt from its own column values. The
        earlier rows stay readable through the durable reader afterwards.

        A store whose columns are a superset of the declaration — every one
        this code declares, plus ones newer code added — is not missing
        anything, so no row is decoded and no row is rewritten. Only the
        indexes are re-asserted, which is what a store can be missing without
        any of its rows changing.
        """
        columns = self._runs_columns(table)
        indexes = _RUNS_INDEXES if table == "runs" else _ARCHIVE_RUNS_INDEXES
        if not columns:
            self._conn.executescript(
                _RUNS_SCHEMA if table == "runs" else _ARCHIVE_RUNS_SCHEMA
            )
            self._conn.executescript(indexes)
            return
        names = [
            str(desc[0])
            for desc in self._conn.execute(
                f"SELECT * FROM {table} LIMIT 0"  # noqa: S608 - a fixed table name
            ).description
        ]
        if columns >= _RUNS_COLUMNS:
            self._conn.executescript(indexes)
            return
        details = "run_details" if table == "runs" else "archive.run_details"
        rows = self._conn.execute(
            f"SELECT r.*, d.detail FROM {table} r "  # noqa: S608 - fixed table names
            f"LEFT JOIN {details} d ON r.run_id = d.run_id"
        ).fetchall()
        update = (
            _RUNS_UPDATE
            if table == "runs"
            else _RUNS_UPDATE.replace('"runs"', "archive.runs")
        )
        with self._conn:
            # DDL needs an explicit transaction: implicit transactions begin
            # only at the first data write, leaving added columns behind if
            # the backfill fails and making a retry mistake shape for completion.
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            # Added columns are nullable because SQLite cannot add a NOT NULL
            # payload without rebuilding the table; the backfill supplies it.
            for name in (*QUERY_KEYS, ROW_REVISION, "payload", "activity_seconds"):
                if name not in columns:
                    kind = "REAL" if name == "activity_seconds" else "TEXT"
                    self._conn.execute(
                        f'ALTER TABLE {table} ADD COLUMN "{name}" {kind}'
                    )
            for row in rows:
                # The one per-row decode a backfill performs, named so a check
                # can count the rows a migration rewrote.
                held = dict(zip(names, row[:-1], strict=True))
                record = _legacy_payload(held)
                self._conn.execute(
                    update,
                    (
                        json.dumps(record, sort_keys=True, separators=(",", ":")),
                        *_query_values(
                            record,
                            json.loads(row[-1] or "{}"),
                            # The row already states the project it is filed
                            # under; a payload written since the append stopped
                            # injecting one does not, so the column is carried
                            # across unchanged rather than re-derived.
                            project=held.get("project"),
                        ),
                        None,
                        record["run_id"],
                    ),
                )
            self._conn.executescript(indexes)

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
        run_id, payload, detail = _split_durable(record)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._is_archived(run_id):
                raise sqlite3.IntegrityError(f"run {run_id} is already archived")
            self._conn.execute(
                _RUNS_INSERT,
                (
                    run_id,
                    *_query_values(payload, detail, project=project),
                    None,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._conn.execute(
                'INSERT INTO "run_details" ("run_id", "detail") VALUES (?, ?)',
                (run_id, json.dumps(detail, sort_keys=True, separators=(",", ":"))),
            )

    def sync_run(self, project: str, record: Mapping[str, Any]) -> str:
        """Ensure one run's row matches the record, returning what happened.

        The outcome is ``inserted`` when the store held no row for this run,
        ``updated`` when it held one that disagreed, and ``unchanged`` when it
        already held the record byte for byte. ``store_write`` is never part of
        the stored payload, exactly as in ``append``.

        An ``unchanged`` answer is what makes a corpus import and a repeated
        amendment no work at all, so the row is compared whole: the durable
        payload and the wide detail. The detail is exactly the fields a
        rotation may wash out — a retention, a lane receipt, a gate check — and
        an amendment confined to one of them moves without its durable payload
        moving. Comparing the payload alone reports that amendment as
        ``unchanged`` and leaves the store holding the revision it superseded.
        """
        run_id, payload, detail = _split_durable(record)
        encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        encoded_detail = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            table = "archive.runs" if self._is_archived(run_id) else "runs"
            details = (
                "archive.run_details" if table == "archive.runs" else "run_details"
            )
            # Both names are this class's own declarations rather than anything
            # a caller supplies, so the statement is fixed text.
            select = (
                f"SELECT stored.payload, wide.detail FROM {table} AS stored "  # noqa: S608
                f"LEFT JOIN {details} AS wide ON wide.run_id = stored.run_id "
                "WHERE stored.run_id = ?"
            )
            existing = self._conn.execute(select, (run_id,)).fetchone()
            if existing is None:
                self._conn.execute(
                    _RUNS_INSERT,
                    (
                        run_id,
                        *_query_values(payload, detail, project=project),
                        None,
                        encoded_payload,
                    ),
                )
                self._conn.execute(
                    'INSERT INTO "run_details" ("run_id", "detail") VALUES (?, ?)',
                    (run_id, encoded_detail),
                )
                return "inserted"
            if existing[0] == encoded_payload and (existing[1] or "") == encoded_detail:
                return "unchanged"
            self._conn.execute(
                _RUNS_UPDATE.replace('"runs"', table),
                (
                    encoded_payload,
                    *_query_values(payload, detail, project=project),
                    None,
                    run_id,
                ),
            )
            self._conn.execute(
                (
                    'INSERT INTO run_details ("run_id", "detail") VALUES (?, ?) '
                    'ON CONFLICT("run_id") DO UPDATE SET "detail" = excluded."detail"'
                ).replace("INSERT INTO run_details", f"INSERT INTO {details}"),
                (run_id, encoded_detail),
            )
            return "updated"

    @property
    def archive_path(self) -> Path:
        """The separate SQLite file holding retired history."""
        return _archive_path(self._path)

    def _attach_archive(self, *, create: bool = False) -> bool:
        """Attach the retired-history file, creating and migrating it on demand.

        ``create`` asks for the archive to hold this code's declared shape, and
        that is owed whether this Open attached it now or an earlier check
        attached it to ask what it lacks.
        """
        if not self._archive_attached and (create or self.archive_path.is_file()):
            self._conn.execute(
                "ATTACH DATABASE ? AS archive", (str(self.archive_path),)
            )
            self._archive_attached = True
        if create and self._archive_attached:
            self._conn.executescript(_ARCHIVE_RUNS_SCHEMA)
            # Both stores are migrated before the schema version is
            # recorded, so an unchanged open owes neither backfill.
            self._migrate_runs("archive.runs")
        return self._archive_attached

    def _archived_ids(self, project: str | None = None) -> set[str]:
        # Membership stays narrow and hot so current readers never open history.
        query = "SELECT run_id FROM retired_runs"
        return {
            str(row[0])
            for row in self._conn.execute(
                query + (" WHERE project = ?" if project is not None else ""),
                (project,) if project is not None else (),
            )
        }

    def _is_archived(self, run_id: str) -> bool:
        present = (
            self._conn.execute(
                "SELECT 1 FROM retired_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            is not None
        )
        if present and not self._attach_archive():
            raise FileNotFoundError(f"retired history is missing: {self.archive_path}")
        return present

    def retire(
        self,
        *,
        before: datetime,
        protected: set[str],
        projects: set[str],
        apply: bool,
        can_retire: Callable[[Mapping[str, Any]], bool] | None = None,
        limit: int = RETIREMENT_BATCH_SIZE,
        vacuum_pages: int = INCREMENTAL_VACUUM_PAGES,
    ) -> dict[str, Any]:
        """Archive one bounded batch and reclaim at most a fixed number of pages.

        An attached-database transaction commits the copy and deletion together.
        Unknown completion times stay hot. The cutoff uses parsed instants so
        equivalent stamps with different offsets receive the same treatment.
        The gate a caller passes sees the record the promotion wrote with the
        project it is filed under declared from the runs column, as every other
        identity read answers it: the payload of a row written since the append
        stopped injecting the key carries no ``project`` of its own.
        """
        from reckon._timestamps import parse_utc

        if limit < 1 or vacuum_pages < 1:
            raise ValueError(
                "retirement row and compaction page limits must be positive"
            )
        with self._conn:
            if apply:
                self._conn.execute("BEGIN IMMEDIATE")
            candidates = []
            for run_id, project, completed, payload in self._conn.execute(
                "SELECT run_id, project, completed_at, payload FROM runs"
            ):
                ended = parse_utc(completed)
                if (
                    project in projects
                    and run_id not in protected
                    and ended
                    and ended < before
                    and (
                        can_retire is None
                        or can_retire(
                            _answer_identity(
                                _decoded_record(payload), str(project or "")
                            )
                        )
                    )
                ):
                    candidates.append(str(run_id))
                    if len(candidates) >= limit:
                        break
            pages_before = self._conn.execute("PRAGMA main.page_count").fetchone()[0]
            if candidates and apply:
                self._attach_archive(create=True)
                for run_id in candidates:
                    self._conn.execute(
                        _RUNS_ARCHIVE_INSERT,
                        (run_id,),
                    )
                    self._conn.execute(
                        "INSERT INTO archive.run_details SELECT * FROM main.run_details "
                        "WHERE run_id = ?",
                        (run_id,),
                    )
                    self._conn.execute(
                        "INSERT INTO retired_runs (run_id, project) "
                        "SELECT run_id, project FROM runs WHERE run_id = ?",
                        (run_id,),
                    )
                    self._conn.execute(
                        "DELETE FROM main.run_details WHERE run_id = ?", (run_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM main.runs WHERE run_id = ?", (run_id,)
                    )
        incremental_ready = (
            self._conn.execute("PRAGMA main.auto_vacuum").fetchone()[0] == 2
        )
        if apply and incremental_ready:
            # Fetching the pragma's rows completes the bounded page moves.
            self._conn.execute(
                f"PRAGMA main.incremental_vacuum({int(vacuum_pages)})"
            ).fetchall()
        return {
            "offered": candidates,
            "retired": candidates if apply else [],
            "archive": str(self.archive_path),
            "pages_before": pages_before,
            "pages_after": self._conn.execute("PRAGMA main.page_count").fetchone()[0],
            "batch_limit": limit,
            "vacuum_page_limit": vacuum_pages,
            "maintenance_required": not incremental_ready,
        }

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

    def _indexed_records(
        self, *, include_archive: bool = False
    ) -> dict[str, dict[str, Any]]:
        schemas = ["main"]
        if include_archive and self._attach_archive():
            schemas.append("archive")
        rows = _record_rows(self._conn, schemas=schemas)
        return {
            str(run_id): run_record.normalize_outside_scope_paths(
                {**json.loads(payload), **(json.loads(detail) if detail else {})}
            )
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
        stale: set[str] = set()
        for name, root in roots.items():
            if project is not None and name != project:
                continue
            indexed = {
                str(row[0])
                for row in self._conn.execute(
                    "SELECT run_id FROM runs WHERE project = ?", (name,)
                )
            } | self._archived_ids(name)
            if indexed != ledger.run_ids(name, root):
                data, _version = ledger.load(name, root, use_index=False)
                changed[name] = data["runs"]
                stale |= indexed
        if not changed:
            return None
        # The payload is the record, so the rows it replaces are named by id
        # rather than by a project field the payload no longer carries.
        records = {
            run_id: record
            for run_id, record in self._indexed_records(include_archive=True).items()
            if run_id not in stale
        }
        for rows in changed.values():
            for row in rows:
                run_id, payload, detail = _split_durable(row)
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
                    archived = self._archived_ids(name)
                    if archived and not self._attach_archive():
                        raise sqlite3.DatabaseError(
                            f"retired history is missing: {self.archive_path}"
                        )
                    retained = {str(record["run_id"]) for record in rows}
                    for run_id in archived - retained:
                        self._conn.execute(
                            "DELETE FROM archive.run_details WHERE run_id = ?",
                            (run_id,),
                        )
                        self._conn.execute(
                            "DELETE FROM archive.runs WHERE run_id = ?", (run_id,)
                        )
                        self._conn.execute(
                            "DELETE FROM retired_runs WHERE run_id = ?", (run_id,)
                        )
                    for record in rows:
                        run_id, payload, detail = _split_durable(record)
                        if run_id in archived:
                            self._conn.execute(
                                _RUNS_UPDATE.replace('"runs"', "archive.runs"),
                                (
                                    json.dumps(
                                        payload, sort_keys=True, separators=(",", ":")
                                    ),
                                    *_query_values(payload, detail, project=name),
                                    None,
                                    run_id,
                                ),
                            )
                            self._conn.execute(
                                "INSERT INTO archive.run_details (run_id, detail) VALUES (?, ?) "
                                "ON CONFLICT(run_id) DO UPDATE SET detail = excluded.detail",
                                (
                                    run_id,
                                    json.dumps(
                                        detail, sort_keys=True, separators=(",", ":")
                                    ),
                                ),
                            )
                            continue
                        self._conn.execute(
                            _RUNS_INSERT,
                            (
                                run_id,
                                *_query_values(payload, detail, project=name),
                                None,
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
        """Read hot records for current-state consumers, refreshing the cache."""
        records = self._refresh()
        if records is None:
            return self._indexed_records()
        archived = self._archived_ids()
        return {key: value for key, value in records.items() if key not in archived}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run's durable half as a dict, or None when absent.

        The durable half is every field the record carried except the declared
        wide detail, so this is the part a rotation must never touch: after a
        rotation deletes the detail row, every field this answers is still
        answerable. The project a row is filed under is its column rather than
        a field of the record, and it is declared back onto the answer here so
        a caller comparing the file against the store reads the same identity
        on both sides.
        """
        records = self._refresh()
        if records is not None:
            record = records.get(run_id)
            if record is None:
                return None
            return _durable_record(self._row_projects().get(run_id) or "", record)
        row = self._conn.execute(
            'SELECT "payload", "project" FROM "runs" WHERE "run_id" = ?', (run_id,)
        ).fetchone()
        if row is None:
            if not self._is_archived(run_id):
                return None
            row = self._conn.execute(
                "SELECT payload, project FROM archive.runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
        return _answer_identity(_decoded_record(row[0]), str(row[1] or ""))

    def get_detail(self, run_id: str) -> dict[str, Any] | None:
        """Return one run's wide detail as a dict, or None when absent.

        The detail is the subset of the declared wide fields the record
        carried; it is what rotation washes out, so it returns None once the
        detail row has been rotated away.
        """
        records = self._refresh()
        if records is not None:
            record = records.get(run_id)
            return _split_durable(record)[2] if record else None
        row = self._conn.execute(
            'SELECT "detail" FROM "run_details" WHERE "run_id" = ?', (run_id,)
        ).fetchone()
        if row is None:
            if not self._is_archived(run_id):
                return None
            row = self._conn.execute(
                "SELECT detail FROM archive.run_details WHERE run_id = ?", (run_id,)
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
            owners = self._row_projects()
            return {
                run_id: _durable_record(project, record)
                for run_id, record in records.items()
                if owners.get(run_id) == project
            }
        rows = self._conn.execute(
            'SELECT "run_id", "payload" FROM "runs" WHERE "project" = ?', (project,)
        ).fetchall()
        if self._attach_archive():
            rows.extend(
                self._conn.execute(
                    "SELECT run_id, payload FROM archive.runs WHERE project = ?",
                    (project,),
                ).fetchall()
            )
        return {
            str(run_id): _answer_identity(_decoded_record(payload), project)
            for run_id, payload in rows
            if payload is not None
        }

    def _row_projects(self) -> dict[str, str]:
        """The project column of every row the store holds, archived included."""
        projects = {
            str(run_id): str(name)
            for run_id, name in self._conn.execute(
                'SELECT "run_id", "project" FROM "runs"'
            )
        }
        if self._attach_archive():
            projects.update(
                {
                    str(run_id): str(name)
                    for run_id, name in self._conn.execute(
                        "SELECT run_id, project FROM archive.runs"
                    )
                }
            )
        return projects

    def revisionless_rows(self, project: str) -> set[str]:
        """The project's rows that name no revision."""
        if ROW_REVISION not in self._runs_columns():
            return set()
        return {
            str(row[0])
            for row in self._conn.execute(
                'SELECT "run_id" FROM "runs" WHERE "project" = ? '
                'AND ("revision" IS NULL OR "revision" = \'\')',
                (project,),
            )
        }

    def stamp_revisions(self, project: str, revisions: Mapping[str, str]) -> int:
        """Record each named revision on its own row, returning how many it set.

        A row that already names a revision is left alone, so a repeated pass
        costs nothing and history that has since moved never rewrites what an
        earlier pass placed.
        """
        stamped = 0
        with self._conn:
            for run_id, revision in revisions.items():
                cursor = self._conn.execute(
                    'UPDATE "runs" SET "revision" = ? WHERE "run_id" = ? '
                    'AND "project" = ? '
                    'AND ("revision" IS NULL OR "revision" = \'\')',
                    (str(revision), str(run_id), project),
                )
                stamped += cursor.rowcount
        return stamped

    def append_member(self, member: Mapping[str, Any], *, project: str = "") -> None:
        """Record one roster member's full payload under its own id."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO "members" ("project", "member_id", "payload") '
                "VALUES (?, ?, ?)",
                (
                    project,
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


def _locked(exc: BaseException) -> bool:
    """Whether a store write lost a race for the database's write lock.

    SQLite reports a held write lock as ``database is locked`` or, for a table
    locked inside a transaction, ``database table is locked``. A duplicate-row
    or malformed-record refusal is never retried: the same write would fail
    again for the same reason.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _append_backoff(attempt: int) -> None:
    """Yield to the writer holding the store lock before the next attempt."""
    delay = min(INDEX_RETRY_MAX_DELAY_SECONDS, 0.1 * (2**attempt))
    time.sleep(random.uniform(delay / 2, delay))  # noqa: S311 - scheduling jitter


def _retrying(write: Callable[[RunStore], None]) -> None:
    """Run one store write, retrying a lock with a bounded backoff.

    The retry lives below the callers' seams, inside one call, so a caller
    counting writes still sees one per record however many attempts a lock
    took. A lock outlasting the bound, and a failure of any other kind, is
    raised to the caller.
    """
    deadline = time.monotonic() + INDEX_RETRY_SECONDS
    attempt = 0
    while True:
        try:
            with RunStore(busy_timeout=INDEX_BUSY_TIMEOUT_SECONDS) as store:
                write(store)
            return
        except sqlite3.OperationalError as exc:
            if not _locked(exc) or time.monotonic() >= deadline:
                raise
            _append_backoff(attempt)
            attempt += 1


def append(project: str, record: Mapping[str, Any]) -> None:
    """Append one run to the default store, raising on failure.

    A locked store is retried with a backoff bounded by wall clock, because a
    lock held by a concurrent writer lasts milliseconds and the row it costs is
    invisible until a reader rebuilds the whole index. The retry lives below
    this seam, inside one call, so a caller counting appends sees one append per
    run however many attempts the lock took. A lock outlasting the bound, and a
    failure of any other kind, is raised.

    This is the seam a promotion calls and the seam a test injects a failure
    into: the store is the durable authority for a promoted run, so the caller
    turns a raise here into a refusal and takes back the run file it had
    already written, leaving neither store holding a row.
    """
    _retrying(lambda store: store.append(project, record))


def sync(project: str, record: Mapping[str, Any]) -> str | None:
    """Bring the store's own row for one run up to date with ``record``.

    Returns the store's outcome — ``inserted``, ``updated`` or ``unchanged`` —
    or ``None`` when the store is absent or holds no row for this run. An
    amendment therefore reaches the store through this seam rather than through
    :func:`append`, which writes a new row and refuses one already there, and a
    store that does not hold the run is left exactly as it was: a project with
    no store does not acquire one from an amendment, and a run the store has
    never seen is a corpus import's row rather than one write's.

    A locked store is retried inside this one call, as :func:`append` retries
    it, so a concurrent writer's millisecond lock costs the amendment no row.
    A failure of any other kind is raised: the store is the copy a stored read
    answers from, so an amendment it declined would be read as the revision it
    superseded.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    if not stored_run_rows(project, run_ids=(run_id,), fields=("run_id",)):
        return None
    outcomes: list[str] = []
    _retrying(lambda store: outcomes.append(store.sync_run(project, dict(record))))
    return outcomes[-1] if outcomes else None


def runs_for_node(node: str, *, project: str | None = None) -> list[dict[str, Any]]:
    """Durable run records whose indexed node column is ``node``.

    A run's node id is durable on its row whether or not the run still has a
    live pointer, so this is the reader that answers for a run the fleet has
    already let go. ``project`` scopes the answer to one project whenever it is
    given, and the selection widens across projects only for a caller that
    names none, so a reader held to one project never reaches another's run.

    The connection is read-only and a missing store answers with no rows, so a
    read creates, migrates and rebuilds nothing: a caller resolving an identity
    must not change the store it resolves against. A store that cannot be read
    raises to the caller, which decides what an unreadable answer means for the
    question it asked.
    """
    hot = store_path()
    archive = hot.with_name(f"{hot.stem}.archive{hot.suffix}")
    rows = []
    for path in (hot, archive):
        if not path.is_file():
            continue
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if project is None:
                rows.extend(
                    connection.execute(
                        'SELECT "payload" FROM "runs" WHERE "node" = ?', (str(node),)
                    ).fetchall()
                )
            else:
                # A node is selective; keep the project filter from choosing
                # an index that walks every run belonging to the project.
                rows.extend(
                    connection.execute(
                        'SELECT "payload" FROM "runs" WHERE "node" = ? AND +"project" = ?',
                        (str(node), str(project)),
                    ).fetchall()
                )
        finally:
            connection.close()
    records: list[dict[str, Any]] = []
    for (payload,) in rows:
        if payload is None:
            continue
        record = json.loads(payload)
        if isinstance(record, dict):
            records.append(record)
    return records


def indexed_run_ids(project: str) -> set[str] | None:
    """Read current index membership without creating, migrating or rebuilding it.

    None distinguishes a missing index from an existing empty one. A read-only
    connection avoids RunStore's schema setup and reader-triggered refresh, so
    observing lag cannot repair or conceal it.
    """
    path = store_path()
    if not path.exists():
        return None
    connection = _open_readonly(path)
    try:
        if not _refuse_owed_migration(connection, path):
            return set()
        try:
            ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT run_id FROM runs WHERE project = ?", (project,)
                )
            }
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'retired_runs'"
            ).fetchone():
                ids.update(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT run_id FROM retired_runs WHERE project = ?", (project,)
                    )
                )
        except sqlite3.Error as exc:
            raise _unreadable(path, f"{type(exc).__name__}: {exc}") from exc
        return ids
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
    (present but divergent, corrected), ``rows_unchanged`` (present and
    already identical), and the revision stamping this pass performed
    (:func:`backfill_revisions`), which places each row at the commit its own
    record file entered the tree at so a reader can still find that file once
    the tree no longer carries it.
    """
    from reckon import ledger

    data, _version = ledger.load(project, root, use_index=False)
    counts = {"rows_imported": 0, "rows_already_present": 0, "rows_unchanged": 0}

    def write(store: RunStore) -> None:
        # Each attempt recounts from zero: a retried attempt re-walks the whole
        # corpus, and sync_run is idempotent, so leaving a failed attempt's
        # partial counts in place would report rows this pass did not write.
        counts.update(rows_imported=0, rows_already_present=0, rows_unchanged=0)
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

    # A concurrent writer's lock is retried like any other store write, so an
    # import running beside a promotion waits it out instead of surfacing an
    # OperationalError to a caller that only asked to repair the index.
    _retrying(write)
    # The corpus is in the store; where each run's file entered the tree is a
    # property of the project's history, stamped once here rather than studied
    # by every reader.
    counts.update(backfill_revisions(project, root=root))
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

    data, _version = ledger.load(project, root, use_index=False)
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


def main(argv: list[str] | None = None) -> int:
    """Run explicit store maintenance in a separately allocated job."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="One-time run-store maintenance")
    parser.add_argument(
        "--enable-incremental-vacuum", action="store_true", required=True
    )
    parser.add_argument("--store", type=Path, default=store_path())
    args = parser.parse_args(argv)
    sys.stderr.write(f"preparing incremental compaction for {args.store}\n")
    print(json.dumps(enable_incremental_vacuum(args.store)), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
