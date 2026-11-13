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
from functools import wraps
from pathlib import Path
from typing import Any

_READERS: ContextVar[tuple[Mapping[str, Any], ...] | None] = ContextVar(
    "open_run_readers", default=None
)


def pointers() -> tuple[Mapping[str, Any], ...] | None:
    """The live-index snapshot this operational read is using, if any."""
    return _READERS.get()


@contextlib.contextmanager
def read_scope(record: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Share live identities and an explicitly reconstructed target's record."""
    snapshot = pointers()
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
    try:
        yield
    finally:
        _READERS.reset(token)


def operational_read(function):
    """Keep an operational entry point inside the open-run read population."""

    @wraps(function)
    def bounded(*args, **kwargs):
        with read_scope(record=kwargs.get("record")):
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


def _connect(path: Path) -> sqlite3.Connection:
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


def _source_written(connection, path: Path) -> None:
    from reckon import file_memo

    try:
        stamp = file_memo.file_signature(path)
    except OSError:
        return
    connection.execute(
        "INSERT OR REPLACE INTO sources VALUES (?, ?)",
        (str(path.resolve()), json.dumps(stamp)),
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


def _ensure(project: str, root=None) -> Path:
    """Bootstrap an installation that predates the incremental projection."""
    from reckon import ledger

    path = index_path(project, root)
    with contextlib.closing(_connect(path)) as connection:

        def current():
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            from reckon import file_memo

            aggregate = ledger.ledger_path(project, root).resolve()
            recorded = connection.execute(
                "SELECT stamp FROM sources WHERE name=?", (str(aggregate),)
            ).fetchone()
            try:
                actual = json.dumps(file_memo.file_signature(aggregate))
            except FileNotFoundError:
                actual = None
            generation = ledger.ledger_generation(project)
            return (
                metadata.get("ready") == "1"
                and generation is not None
                and metadata.get("generation") == json.dumps(generation)
                and (recorded[0] if recorded else None) == actual
            )

        if current():
            return path
        # Serialize bootstrap with writers; rows written while this starts are
        # either in the authoritative read or applied after this transaction.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if not current():
                generation = ledger.ledger_generation(project)
                aggregate = ledger.ledger_path(project, root)
                if not aggregate.exists() and not any(
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
                for source in (ledger.ledger_path(project, root).parent / "runs").glob(
                    "*.json"
                ):
                    _source_written(connection, source)
                connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('generation', ?)",
                    (json.dumps(generation),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('ready', '1')"
                )
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


def _review_index_path(directory: Path) -> Path:
    from reckon import ledger

    key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
    return ledger._index_directory() / "review-lookup" / f"{key}.sqlite"


@_publish
def review_written(path: Path, record: Mapping[str, Any], *, previous=None) -> None:
    """Advance a complete review projection or leave it marked for recovery."""
    from reckon import ledger

    index = _review_index_path(path.parent)
    existed = index.exists()
    index.parent.mkdir(parents=True, exist_ok=True)
    with (
        contextlib.closing(sqlite3.connect(index, timeout=30)) as connection,
        connection,
    ):
        _review_schema(connection)
        metadata = dict(connection.execute("SELECT key, value FROM review_meta"))
        complete = (
            (
                metadata.get("complete") == "1"
                and metadata.get("directory") == json.dumps(previous)
            )
            if existed
            else not any(item.name != path.name for item in path.parent.glob("*.json"))
        )
        _review_rows(connection, [(path.name, record)])
        connection.executemany(
            "INSERT OR REPLACE INTO review_meta VALUES (?, ?)",
            [
                ("directory", json.dumps(ledger._file_identity(path.parent))),
                ("complete", str(int(complete))),
            ],
        )


def _review_schema(connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS reviews (name TEXT PRIMARY KEY, subject TEXT, producer TEXT)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS by_subject ON reviews(subject)")
    connection.execute("CREATE INDEX IF NOT EXISTS by_producer ON reviews(producer)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS review_meta (key TEXT PRIMARY KEY, value TEXT)"
    )


def _review_rows(connection, rows) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?)",
        [
            (
                name,
                str(row.get("reviewed_run_id") or ""),
                str(row.get("review_run_id") or ""),
            )
            for name, row in rows
        ],
    )


def review_rebuilt(directory: Path, entries, stamp) -> None:
    """Publish the full reader's answer as the next operational lookup."""
    try:
        index = _review_index_path(directory)
        index.parent.mkdir(parents=True, exist_ok=True)
        with (
            contextlib.closing(sqlite3.connect(index, timeout=30)) as connection,
            connection,
        ):
            _review_schema(connection)
            metadata = dict(connection.execute("SELECT key, value FROM review_meta"))
            if metadata.get("complete") == "1" and metadata.get(
                "directory"
            ) == json.dumps(stamp):
                return
            connection.execute("DELETE FROM reviews")
            _review_rows(connection, [(entry["name"], entry) for entry in entries])
            connection.executemany(
                "INSERT OR REPLACE INTO review_meta VALUES (?, ?)",
                [("directory", json.dumps(stamp)), ("complete", "1")],
            )
    except (OSError, sqlite3.Error):
        return


def review_paths(directory: Path) -> list[Path] | None:
    """The review files open work can refer to, or None for a history read."""
    snapshot = pointers()
    if snapshot is None:
        return None
    ids = {str(row.get("run_id") or "") for row in snapshot} - {""}
    declared: set[Path] = set()
    for row in snapshot:
        node = row.get("node") or {}
        if isinstance(node, Mapping):
            for name in node.get("write_paths") or ():
                path = Path(name)
                if (
                    path.is_absolute()
                    and path.parent == directory
                    and path.suffix == ".json"
                ):
                    declared.add(path)
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
    index = _review_index_path(directory)
    if not index.exists():
        return None
    paths: set[Path] = declared
    from reckon import ledger

    try:
        with contextlib.closing(
            sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True)
        ) as connection:
            stamp = connection.execute(
                "SELECT value FROM review_meta WHERE key='directory'"
            ).fetchone()
            if stamp is None or stamp[0] != json.dumps(
                ledger._file_identity(directory)
            ):
                return None
            for run_id in ids:
                paths.update(
                    directory / name
                    for (name,) in connection.execute(
                        "SELECT name FROM reviews WHERE subject=? OR producer=?",
                        (run_id, run_id),
                    )
                )
    except (OSError, sqlite3.Error):
        return None
    return sorted(paths)


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
    from reckon import ledger

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
    return list(result.values())


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
        return json.loads(row[0]) if row else None


@_read_fallback(_history_membership)
def has_row(project: str, run_id: str, root=None) -> bool:
    """Test one promoted identity without enumerating historical membership."""
    from reckon import ledger

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
    """Use the held reader's existing stat snapshot to validate the fast path."""
    path = index_path(project, root)
    if not path.exists():
        return False
    try:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            if not connection.execute(
                "SELECT 1 FROM metadata WHERE key='ready'"
            ).fetchone():
                return False
            stored = dict(connection.execute("SELECT name, stamp FROM sources"))
        aggregate, stamp, documents = signature[:3]
        actual = {aggregate: json.dumps(stamp)} if stamp is not None else {}
        actual.update((name, json.dumps(identity)) for name, identity in documents[1])
        return stored == actual
    except (OSError, sqlite3.Error):
        return False
