"""Bound operational reads to live runs and promoted runs still owing cleanup.

Live pointers are the start/end index for running work. The ledger projection
keeps promoted rows addressable by identity, with a separate indexed population
for retained worktrees. History readers continue to use the complete ledger.
An existing installation bootstraps the projection once from the ledger; every
subsequent ledger writer updates it in the same operation as its run record.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from functools import partial, wraps
from pathlib import Path
from typing import Any

_READERS: ContextVar[tuple[Mapping[str, Any], ...] | None] = ContextVar(
    "open_run_readers", default=None
)

_PROJECT_READERS: ContextVar[dict[str, tuple[Mapping[str, Any], ...]] | None] = (
    ContextVar("open_run_project_readers", default=None)
)

_VALIDATED: ContextVar[set[Path] | None] = ContextVar(
    "open_run_validated_indexes", default=None
)


def pointers(*, project: str | None = None) -> tuple[Mapping[str, Any], ...] | None:
    """The held live listing, narrowed to a project without another decode."""
    snapshot = _READERS.get()
    if snapshot is None or project is None:
        return snapshot
    projects = _PROJECT_READERS.get()
    if projects is None:
        return tuple(
            row for row in snapshot if str(row.get("project") or "") == project
        )
    if project not in projects:
        projects[project] = tuple(
            row for row in snapshot if str(row.get("project") or "") == project
        )
    return projects[project]


@contextlib.contextmanager
def read_scope(
    record: Mapping[str, Any] | None = None,
    *,
    live: Sequence[Mapping[str, Any]] | None = None,
) -> Iterator[None]:
    """Share live identities and an explicitly reconstructed target's record.

    A caller supplying its own listing makes it the population of this pass.
    Project selections keep their identity throughout the scope, so readers
    sharing a project also share the review index built from that selection.
    The tokens are reset on exit, including an exception, so the next pass
    takes a fresh listing and sees pointers written during this one.
    """
    held = pointers()
    snapshot = tuple(live) if live is not None else held
    if snapshot is None:
        from reckon.crew import runs

        snapshot = tuple(runs.list_live())
    if (
        record is not None
        and record.get("run_id")
        and not any(item.get("run_id") == record["run_id"] for item in snapshot)
    ):
        snapshot = (*snapshot, record)
    token = _READERS.set(snapshot)
    projects_token = _PROJECT_READERS.set({}) if snapshot is not held else None
    validated_token = _VALIDATED.set(set()) if _VALIDATED.get() is None else None
    try:
        yield
    finally:
        _READERS.reset(token)
        if projects_token is not None:
            _PROJECT_READERS.reset(projects_token)
        if validated_token is not None:
            _VALIDATED.reset(validated_token)


def operational_read(function=None, *, live_reader=None):
    """Keep an operational entry point inside the open-run read population."""
    if function is None:
        return partial(operational_read, live_reader=live_reader)

    @wraps(function)
    def bounded(*args, **kwargs):
        live = kwargs.get("pointers")
        if live is None and pointers() is None and live_reader is not None:
            live = live_reader()
        with read_scope(record=kwargs.get("record"), live=live):
            return function(*args, **kwargs)

    return bounded


def _publish(function):
    """A refused cache write leaves the authoritative writer's result intact."""

    @wraps(function)
    def publish(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, sqlite3.Error):
            return None

    return publish


def _read_fallback(history_reader):
    """A missing cache capability keeps the established authoritative reader."""

    def decorate(function):
        @wraps(function)
        def read(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (OSError, sqlite3.Error):
                return history_reader(*args, **kwargs)

        return read

    return decorate


def _history_rows(project, root=None, *, retained_only=False):
    from reckon import ledger

    return ledger.runs(project, root=root)


def _history_row(project, run_id, root=None):
    return next(
        (row for row in _history_rows(project, root) if row.get("run_id") == run_id),
        None,
    )


def _history_dispatched_rows(project, session=None, root=None):
    rows = _history_rows(project, root)
    if session is None:
        return rows
    return [row for row in rows if str(row.get("session") or "") == str(session)]


def _history_session(project, session, root=None):
    rows = [
        row for row in _history_rows(project, root) if row.get("session_id") == session
    ]
    return max(
        rows,
        key=lambda row: (
            str(row.get("completed_at") or ""),
            str(row.get("run_id") or ""),
        ),
        default=None,
    )


def _history_membership(project, run_id, root=None):
    return _history_row(project, run_id, root) is not None


def _history_version(project, root=None):
    from reckon import ledger

    return ledger.load_aggregate(project, root)[1]


def _history_tree_rows(project, trees, root=None):
    return _history_rows(project, root)


def index_path(project: str, root: str | Path | None = None) -> Path:
    """One projection per authoritative ledger path, including private trees."""
    from reckon import ledger

    source = str(ledger.ledger_path(project, root).resolve())
    key = hashlib.sha256(source.encode()).hexdigest()
    return ledger._index_directory() / "open-run-index" / f"{key}.sqlite"


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS records "
        "(run_id TEXT PRIMARY KEY, node TEXT, session TEXT, completed TEXT, "
        "owed INTEGER, payload TEXT NOT NULL)"
    )
    for column in ("owed", "node", "session"):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS by_{column} ON records({column}, completed)"
        )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tips (run_id TEXT PRIMARY KEY, tip TEXT)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS by_tip ON tips(tip)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, stamp TEXT)"
    )
    return connection


def _values(row: Mapping[str, Any]) -> tuple[Any, ...]:
    node = row.get("node") or ""
    if isinstance(node, Mapping):
        node = node.get("id") or ""
    release = row.get("release") or {}
    released = isinstance(release, Mapping) and release.get("worktree_released")
    owed = bool(row.get("worktree_retention")) and not released
    return (
        str(row["run_id"]),
        str(node),
        str(row.get("session_id") or ""),
        str(row.get("completed_at") or ""),
        int(owed),
        json.dumps(dict(row)),
    )


@_publish
def record_written(project: str, row: Mapping[str, Any], root=None) -> None:
    """Advance the projection after its authoritative run file was written."""
    with (
        contextlib.closing(_connect(index_path(project, root))) as connection,
        connection,
    ):
        connection.execute(
            "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?)", _values(row)
        )
        from reckon import ledger

        connection.execute(
            "INSERT OR REPLACE INTO tips VALUES (?, ?)",
            (str(row["run_id"]), ledger._run_tip(row)),
        )
        _source_written(connection, ledger.run_path(project, str(row["run_id"]), root))
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('generation', ?)",
            (json.dumps(ledger.ledger_generation(project)),),
        )


def _stamp_text(identity: Any) -> str:
    """The text a source stamp is recorded as, and the one it is read back as.

    One encoder for both sides. A writer and a comparison that spelled the same
    identity two ways would refuse every stamp the writer left.
    """
    return json.dumps(identity)


def _source_written(connection, path: Path) -> None:
    from reckon import file_memo

    try:
        stamp = file_memo.file_signature(path)
    except OSError:
        return
    connection.execute(
        "INSERT OR REPLACE INTO sources VALUES (?, ?)",
        (str(path.resolve()), _stamp_text(stamp)),
    )


@_publish
def aggregate_written(
    project: str, rows: Sequence[Mapping[str, Any]], root=None, *, version: int = 0
) -> None:
    """Index aggregate rows without dropping records held in their own files."""
    with (
        contextlib.closing(_connect(index_path(project, root))) as connection,
        connection,
    ):
        connection.executemany(
            "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?)",
            [_values(row) for row in rows if row.get("run_id")],
        )
        from reckon import ledger

        connection.executemany(
            "INSERT OR REPLACE INTO tips VALUES (?, ?)",
            [
                (str(row["run_id"]), ledger._run_tip(row))
                for row in rows
                if row.get("run_id")
            ],
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (str(version),)
        )
        _source_written(connection, ledger.ledger_path(project, root))
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('generation', ?)",
            (json.dumps(ledger.ledger_generation(project)),),
        )
        for row in rows:
            if row.get("run_id"):
                _source_written(
                    connection, ledger.run_path(project, str(row["run_id"]), root)
                )


def _store_signature() -> str | None:
    from reckon import file_memo, run_store

    path = run_store.store_path()
    if not path.exists():
        return None
    return json.dumps(
        [
            file_memo.file_signature(path),
            *[
                file_memo.file_signature(extra) if extra.exists() else None
                for extra in (
                    run_store._archive_path(path),
                    Path(str(path) + "-wal"),
                )
            ],
        ]
    )


def _hydrate(project: str, rows: list[dict]) -> list[dict]:
    """Fetch full records only for identities the operational projection selected."""
    from reckon import run_store

    stored = run_store.stored_run_rows(project, run_ids=[row["run_id"] for row in rows])
    return rows if stored is None else stored


def _current(connection: sqlite3.Connection, project: str, root) -> bool:
    """Validate the complete projection using only reads."""
    from reckon import file_memo, ledger

    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    signature = _store_signature()
    if signature is not None:
        return metadata.get("ready") == "1" and metadata.get("store") == signature
    aggregate = ledger.ledger_path(project, root).resolve()
    recorded = connection.execute(
        "SELECT stamp FROM sources WHERE name=?", (str(aggregate),)
    ).fetchone()
    try:
        actual = _stamp_text(file_memo.file_signature(aggregate))
    except FileNotFoundError:
        actual = None
    generation = ledger.ledger_generation(project)
    return (
        metadata.get("ready") == "1"
        and generation is not None
        and metadata.get("generation") == json.dumps(generation)
        and (recorded[0] if recorded else None) == actual
    )


def _ensure(project: str, root=None) -> Path:
    """Validate once per operational pass; reuse a complete index while busy."""
    from reckon import ledger

    path = index_path(project, root)
    validated = _VALIDATED.get()
    if validated is not None and path in validated:
        return path

    ready = False
    if path.exists():
        with contextlib.closing(_connect(path, read_only=True)) as connection:
            ready = (
                connection.execute(
                    "SELECT 1 FROM metadata WHERE key='ready' AND value='1'"
                ).fetchone()
                is not None
            )
            if ready and _current(connection, project, root):
                if validated is not None:
                    validated.add(path)
                return path

    # A complete projection needs no schema writes. Acquiring the rebuild's
    # transaction never waits: another writer leaves the last committed rows
    # available to this reader until the next operational pass revalidates.
    with contextlib.closing(
        sqlite3.connect(path, timeout=0) if ready else _connect(path)
    ) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if not ready or error.sqlite_errorcode not in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise
        else:
            with connection:
                if not _current(connection, project, root):
                    generation = ledger.ledger_generation(project)
                    aggregate = ledger.ledger_path(project, root)
                    from reckon import run_store

                    signature = _store_signature()
                    if signature is not None:
                        rows = run_store.stored_run_rows(
                            project,
                            fields=(
                                "run_id",
                                "node",
                                "session_id",
                                "completed_at",
                                "worktree_retention",
                                "release",
                                "commits",
                                "base_sha",
                                "no_commit",
                                "session",
                            ),
                        )
                        data = {"runs": rows or []}
                        _version = ledger._aggregate_headers(project, root)[1]
                    elif not aggregate.exists() and not any(
                        (aggregate.parent / "runs").glob("*.json")
                    ):
                        data, _version = {"runs": []}, 0
                    else:
                        token = _READERS.set(None)
                        try:
                            data, _version = ledger.load(project, root)
                        finally:
                            _READERS.reset(token)
                    connection.execute("DELETE FROM records")
                    connection.execute("DELETE FROM tips")
                    connection.execute("DELETE FROM sources")
                    connection.executemany(
                        "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?)",
                        [_values(row) for row in data["runs"] if row.get("run_id")],
                    )
                    connection.executemany(
                        "INSERT OR REPLACE INTO tips VALUES (?, ?)",
                        [
                            (str(row["run_id"]), ledger._run_tip(row))
                            for row in data["runs"]
                            if row.get("run_id")
                        ],
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('version', ?)",
                        (str(_version),),
                    )
                    _source_written(connection, ledger.ledger_path(project, root))
                    if signature is None:
                        for source in (
                            ledger.ledger_path(project, root).parent / "runs"
                        ).glob("*.json"):
                            _source_written(connection, source)
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('store', ?)",
                        (signature,),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('generation', ?)",
                        (json.dumps(generation),),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('ready', '1')"
                    )
    if validated is not None:
        validated.add(path)
    return path


@_publish
def bootstrap_complete(project: str, root=None) -> None:
    """Mark a full aggregate write complete when no per-run files predate it."""
    from reckon import ledger

    if (
        next((ledger.ledger_path(project, root).parent / "runs").glob("*.json"), None)
        is not None
    ):
        return
    with (
        contextlib.closing(_connect(index_path(project, root))) as connection,
        connection,
    ):
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('ready', '1')")


@_read_fallback(_history_rows)
def read_rows(
    project: str, root=None, *, retained_only: bool = False
) -> list[dict[str, Any]]:
    """Read indexed retained rows and only the identities the live index names."""
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        found = {
            row["run_id"]: row
            for (payload,) in connection.execute(
                "SELECT payload FROM records WHERE owed=1"
            )
            for row in [json.loads(payload)]
        }
        if not retained_only:
            for pointer in pointers() or ():
                if pointer.get("project") != project:
                    continue
                ids = [str(pointer.get("run_id") or "")]
                lineage = pointer.get("lineage") or {}
                if isinstance(lineage, Mapping):
                    ids.append(str(lineage.get("primary_run_id") or ""))
                ids.append(str(pointer.get("predecessor_run_id") or ""))
                base = str(pointer.get("base_sha") or pointer.get("base") or "")
                if base:
                    candidates = connection.execute(
                        "SELECT r.payload FROM records r JOIN tips t ON r.run_id=t.run_id WHERE t.tip=? AND r.run_id!=? LIMIT 2",
                        (base, ids[0]),
                    )
                    for (payload,) in candidates:
                        parsed = json.loads(payload)
                        found[parsed["run_id"]] = parsed
                node = pointer.get("node") or {}
                if isinstance(node, Mapping):
                    ids.extend(
                        str(node.get(key) or "") for key in ("repairs", "predecessor")
                    )
                    identity = str(node.get("id") or "")
                    if identity:
                        row = connection.execute(
                            "SELECT payload FROM records WHERE node=? ORDER BY completed DESC LIMIT 1",
                            (identity,),
                        ).fetchone()
                        if row:
                            parsed = json.loads(row[0])
                            found[parsed["run_id"]] = parsed
                for run_id in ids:
                    row = connection.execute(
                        "SELECT payload FROM records WHERE run_id=?", (run_id,)
                    ).fetchone()
                    if row:
                        parsed = json.loads(row[0])
                        found[parsed["run_id"]] = parsed
                session = str(pointer.get("session_id") or "")
                if session:
                    row = connection.execute(
                        "SELECT payload FROM records WHERE session=? ORDER BY completed DESC LIMIT 1",
                        (session,),
                    ).fetchone()
                    if row:
                        parsed = json.loads(row[0])
                        found[parsed["run_id"]] = parsed
    if _store_signature() is not None:
        return sorted(
            _hydrate(project, list(found.values())),
            key=lambda row: str(row.get("completed_at") or ""),
        )
    # The projection locates rows; the open run's own document remains the
    # authority for in-place edits and a writer interrupted before indexing.
    from reckon import ledger

    live_ids = {
        str(pointer.get("run_id") or "")
        for pointer in pointers() or ()
        if pointer.get("project") == project
    }
    for run_id in live_ids | {key for key, row in found.items() if _values(row)[4]}:
        source = ledger.run_path(project, run_id, root)
        if source.exists():
            current = ledger._read_run(source)
            if run_id not in live_ids and not _values(current)[4]:
                found.pop(run_id, None)
            else:
                found[run_id] = current
    return sorted(found.values(), key=lambda row: str(row.get("completed_at") or ""))


def review_paths(directory: Path) -> list[Path] | None:
    """The review files open work can refer to, or None for a history read.

    A store that keeps a writer-maintained index answers its own record lookups
    from that index and never reaches here, so this is the path a directory
    without one takes -- the committed reviews tree, which the project's
    checkout tracks. What the live pointers can narrow to on their own is a
    directory that is itself named by open work; anything finer needs the
    store's own entries, and a directory without an index has none to consult.
    """
    snapshot = pointers()
    if snapshot is None:
        return None
    ids = {str(row.get("run_id") or "") for row in snapshot} - {""}
    for row in snapshot:
        node = row.get("node") or {}
        if isinstance(node, Mapping):
            subject = re.fullmatch(
                r"review-of-(.+)-[0-9a-f]{8,40}(?:-figure)?", str(node.get("id") or "")
            )
            if subject:
                ids.add(subject[1])
            ids.update(
                str(node.get(key) or "") for key in ("reviewed_run_id", "repairs")
            )
    ids.discard("")
    if directory.name in ids:
        return sorted(directory.glob("*.json"))
    return None


@_read_fallback(_history_version)
def ledger_version(project: str, root=None) -> int:
    """The aggregate version captured by its writer or initial bootstrap."""
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='version'"
        ).fetchone()
        return int(row[0]) if row else 0


@_read_fallback(_history_row)
def row_for_run(project: str, run_id: str, root=None) -> dict[str, Any] | None:
    """Lookup one promoted identity without decoding unrelated history."""
    from reckon import ledger, run_store

    stored = run_store.stored_run_rows(project, run_ids=[run_id])
    if stored is not None:
        return stored[0] if stored else None
    source = ledger.run_path(project, run_id, root)
    if source.exists():
        return ledger._read_run(source)
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        row = connection.execute(
            "SELECT payload FROM records WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        if connection.execute(
            "SELECT 1 FROM sources WHERE name=?", (str(source.resolve()),)
        ).fetchone():
            return next(
                (
                    item
                    for item in ledger.load_aggregate(project, root)[0]["runs"]
                    if item.get("run_id") == run_id
                ),
                None,
            )
        return json.loads(row[0])


@_read_fallback(_history_tree_rows)
def rows_naming_trees(
    project: str, trees: Sequence[Path], root=None
) -> list[dict[str, Any]]:
    """Locate promoted rows still owing a registered tree by its naming keys."""
    result = {}
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        for tree in trees:
            row = connection.execute(
                "SELECT payload FROM records WHERE node=? AND json_extract(payload, '$.session')=? ORDER BY completed DESC LIMIT 1",
                (tree.name, tree.parent.name),
            ).fetchone()
            if row:
                parsed = json.loads(row[0])
                result[parsed["run_id"]] = parsed
    return _hydrate(project, list(result.values()))


@_read_fallback(_history_dispatched_rows)
def dispatched_rows(
    project: str, session: str | None = None, root=None
) -> list[dict[str, Any]]:
    """The rows one dispatched session owns, read without opening a run's file.

    The projection's ``session`` column holds the worker's own harness session,
    which is a different key from the crew session that dispatched the run, so
    the filter reads the field the stored record carries. Rows are ordered by
    completion, then identity, so two reads of one projection agree.
    """
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        if session is None:
            query, arguments = "SELECT payload FROM records", ()
        else:
            query = (
                "SELECT payload FROM records WHERE json_extract(payload, '$.session')=?"
            )
            arguments = (str(session),)
        rows = [
            json.loads(payload)
            for (payload,) in connection.execute(
                f"{query} ORDER BY completed, run_id", arguments
            )
        ]
    return _hydrate(project, rows) if _store_signature() is not None else rows


@_read_fallback(_history_session)
def latest_session_row(project: str, session: str, root=None) -> dict[str, Any] | None:
    """The prior cumulative-meter reading for one resolved worker session."""
    if not session:
        return None
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        row = connection.execute(
            "SELECT payload FROM records WHERE session=? ORDER BY completed DESC, run_id DESC LIMIT 1",
            (session,),
        ).fetchone()
        rows = _hydrate(project, [json.loads(row[0])]) if row else []
        return rows[0] if rows else None


@_read_fallback(_history_membership)
def has_row(project: str, run_id: str, root=None) -> bool:
    """Test one promoted identity without enumerating historical membership."""
    from reckon import ledger

    if _store_signature() is not None:
        return row_for_run(project, run_id, root) is not None
    if ledger.run_path(project, run_id, root).is_file():
        return True
    with contextlib.closing(sqlite3.connect(_ensure(project, root))) as connection:
        found = connection.execute(
            "SELECT 1 FROM records WHERE run_id=?", (run_id,)
        ).fetchone()
        if found is None:
            return False
        source = ledger.run_path(project, run_id, root).resolve()
        if connection.execute(
            "SELECT 1 FROM sources WHERE name=?", (str(source),)
        ).fetchone():
            return row_for_run(project, run_id, root) is not None
        return True


def matches_sources(project: str, root, signature: tuple) -> bool:
    """Use the held reader's existing stat snapshot to validate the fast path.

    The projection is filed from one of two authorities, and each is validated
    against the identity its own answer depends on.

    Where a run store exists the projection's rows are the store's rows, so the
    store's signature is the identity that says whether the projection still
    describes it, and :func:`_ensure` is asked to bring the projection current
    first. The per-run documents are copies the store already holds, so
    requiring a stamp per document would refuse on every tick of an
    installation whose documents predate the index — a stamp names a source for
    a read that never makes it, and the answer the read would give comes from
    the store.

    Where no store exists the documents are the authority, so every document
    the held listing holds now must be stamped at the identity it holds now. A
    stamp left behind by a document that has since been removed is not
    consulted: nothing prunes the table but a full rebuild, and the read files
    rows from the population it lists, not from the one the table once listed.
    """
    path = index_path(project, root)
    if not path.exists():
        return False
    try:
        store_backed = _store_signature() is not None
        _ensure(project, root)
        if store_backed:
            with contextlib.closing(_connect(path, read_only=True)) as current:
                return _current(current, project, root)
        with contextlib.closing(_connect(path)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            if metadata.get("ready") != "1":
                return False
            stored = dict(connection.execute("SELECT name, stamp FROM sources"))
        aggregate, stamp, documents = signature[:3]
        if stamp is not None and stored.get(aggregate) != _stamp_text(stamp):
            return False
        return all(
            stored.get(name) == _stamp_text(identity) for name, identity in documents[1]
        )
    except (OSError, sqlite3.Error):
        return False
