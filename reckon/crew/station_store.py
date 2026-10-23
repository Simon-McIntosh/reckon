"""The station's node-local SQLite store.

The station keeps what every consumer of the fleet used to re-derive from the
shared crew home: each live pointer's classified fields, each stream's folded
observation, the dated transition events, the per-subscriber cursors, the
computed obligations and the station's own identity. It is one database per
node, in WAL mode, so a reader on the node reads it without touching GPFS.

The root is the node-local runtime root a session host already resolves
(:func:`reckon.crew.dispatch_watch.session_host_runtime_root`), so the station
and the session host that starts it agree on one directory. A root reached only
through ``TMPDIR`` is refused: ``TMPDIR`` differs per session on this fleet, so
a database there would be a per-session store rather than the node's.

WAL needs shared memory that a network filesystem cannot provide across nodes,
so the root's own mount type is read and any type in
:data:`reckon.host._SHARED_FILESYSTEMS` is refused. A type that cannot be
determined is refused too: the guard fails closed, because it cannot prove such
a root is node-local. The mount type is read directly rather than through
``host._is_node_local``, which accepts the runtime root by path prefix before
it consults the mount type -- and the runtime root is exactly the root this
store uses.

Events are written one file per UTC day under the root's ``events`` directory
and attached for reads, so a day older than the retention window is unlinked
whole and its rows leave with it.

The stat identity stored for a pointer or a stream is
:func:`reckon.file_memo.file_signature`'s, so the station keys a file exactly as
every in-process memo does and a pointer is re-parsed only when that identity
moves.

``StationStoreError`` is this module's own: a refusal here is not a dispatch
refusal, so it joins no entry of :data:`reckon.crew.refusals.DISPATCH_REFUSAL_REMEDIES`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Self

from reckon import _backends, file_memo, host
from reckon.crew import dispatch_watch
from reckon.crew.run_paths import crew_home

DATABASE_NAME = "station.sqlite3"
DAY_DIRECTORY = "events"
DAY_FILE_PREFIX = "events-"
DAY_FILE_SUFFIX = ".sqlite3"

# How many UTC days of events the store keeps. A configuration value: the
# station reads it once and hands it to the store.
RETENTION_DAYS = 7

# Keys the station keeps about itself in the ``station`` table.
HEARTBEAT_KEY = "heartbeat"
CODE_IDENTITY_KEY = "code_identity"
STARTED_AT_KEY = "started_at"
EVENT_COUNTER_KEY = "events_counter"
EVENT_GENERATION_KEY = "events_generation"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pointers (
    path       TEXT PRIMARY KEY,
    signature  TEXT NOT NULL,
    run_id     TEXT,
    project    TEXT,
    session    TEXT,
    phase      TEXT,
    fields     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streams (
    path        TEXT PRIMARY KEY,
    inode       TEXT NOT NULL,
    consumed    INTEGER NOT NULL,
    fold_state  TEXT NOT NULL,
    observation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    audience   TEXT PRIMARY KEY,
    cursor     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS obligations (
    session    TEXT PRIMARY KEY,
    changed_at TEXT,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_DAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    time       TEXT NOT NULL,
    project    TEXT,
    audience   TEXT NOT NULL,
    run        TEXT,
    kind       TEXT,
    from_state TEXT,
    to_state   TEXT,
    detail     TEXT,
    duty       TEXT
);
CREATE INDEX IF NOT EXISTS events_by_audience ON events (audience, id);
CREATE INDEX IF NOT EXISTS events_by_project ON events (project, id);
"""


class StationStoreError(RuntimeError):
    """The station's store cannot be opened or used as a node-local store."""


def _refuse(message: str) -> NoReturn:
    raise StationStoreError(message)


def runtime_root_source(
    *, environ: Mapping[str, str] | None = None, run_user_root: str | Path | None = None
) -> tuple[Path | None, str]:
    """The node-local runtime root and which candidate produced it.

    The candidate order is the session host's, taken from
    :mod:`reckon.crew.dispatch_watch` rather than restated, so the station and
    the host resolve one directory. Only the first two candidates --
    ``$XDG_RUNTIME_DIR`` and then ``/run/user/<uid>`` -- are shared by every
    process on the node; the last, ``TMPDIR``, differs per session.
    """
    return dispatch_watch.session_host_runtime_candidate(
        environ=environ, run_user_root=run_user_root
    )


def filesystem_type(
    root: str | Path, *, mounts: list[tuple[Path, str]] | None
) -> str | None:
    """The mount type covering ``root``, read through the host's own reader.

    Kept as a named function so the mount table can be supplied by a caller
    that has already read it, and so a test can steer the answer to each type
    the guard must refuse.
    """
    table = mounts if mounts is not None else host._mount_table(host.MOUNTINFO_PATH)
    return host._filesystem_type(root, table)


def _refuse_nonlocal_root(root: Path, *, mounts: list[tuple[Path, str]] | None) -> None:
    """Refuse a root that is not proved node-local, naming the path and why."""
    kind = filesystem_type(root, mounts=mounts)
    if kind is None:
        _refuse(
            f"refusing the station's node-local root {root}: its filesystem type "
            "could not be determined from the mount table, so it cannot be proved "
            "node-local, and SQLite WAL needs shared memory only this node provides"
        )
    if kind in host._SHARED_FILESYSTEMS:
        _refuse(
            f"refusing the station's node-local root {root}: it sits on {kind}, a "
            "shared filesystem whose bytes belong to another host, and SQLite WAL "
            "needs shared memory only this node provides"
        )


def _read_pointer_record(path: Path) -> dict[str, Any]:
    """Parse one live pointer's text into its recorded fields.

    A named step rather than an inline ``json.loads``, so a reader can count how
    many times a pointer is actually parsed.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _whole_lines(payload: bytes) -> tuple[list[str], int]:
    """Split ``payload`` up to its last whole record, with the bytes consumed."""
    end = payload.rfind(b"\n")
    if end < 0:
        return [], 0
    text = payload[: end + 1].decode("utf-8", errors="replace")
    return text.splitlines(), end + 1


def _day_file_name(day: date) -> str:
    return f"{DAY_FILE_PREFIX}{day.isoformat()}{DAY_FILE_SUFFIX}"


def _day_from_name(name: str) -> date | None:
    """The UTC day a day file's name encodes, or None for a foreign name."""
    if not name.startswith(DAY_FILE_PREFIX) or not name.endswith(DAY_FILE_SUFFIX):
        return None
    stamp = name[len(DAY_FILE_PREFIX) : -len(DAY_FILE_SUFFIX)]
    try:
        return date.fromisoformat(stamp)
    except ValueError:
        return None


class StationStore:
    """One node's station store: the schema, its day files and its readers."""

    def __init__(self, root: Path, *, retention_days: int = RETENTION_DAYS) -> None:
        self.root = Path(root)
        self.retention_days = int(retention_days)
        self.database = self.root / DATABASE_NAME
        self.day_directory = self.root / DAY_DIRECTORY
        self._connection: sqlite3.Connection | None = None

    @classmethod
    def at_runtime_root(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        run_user_root: str | Path | None = None,
        mounts: list[tuple[Path, str]] | None = None,
        retention_days: int = RETENTION_DAYS,
    ) -> StationStore:
        """Open a store at the node-local runtime root, refusing a root it cannot prove.

        Two roots are refused beside a shared mount: one reached only through
        ``TMPDIR``, which differs per session, and one whose mount type cannot
        be determined.
        """
        root, source = runtime_root_source(environ=environ, run_user_root=run_user_root)
        if root is None:
            _refuse(
                "no node-local runtime root is available: neither XDG_RUNTIME_DIR, "
                "/run/user/<uid>, nor TMPDIR resolves to a directory"
            )
        if source == "scratch":
            _refuse(
                f"refusing the station's node-local root {root}: it is reached only "
                "through TMPDIR, which differs per session on this node, so a store "
                "there would be per-session rather than the node's"
            )
        _refuse_nonlocal_root(root, mounts=mounts)
        return cls(root, retention_days=retention_days)

    # ── connection and schema ────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """Open (or return) the store's connection, creating schema once."""
        if self._connection is not None:
            return self._connection
        self.root.mkdir(parents=True, exist_ok=True)
        self.day_directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(_SCHEMA)
        connection.commit()
        self._connection = connection
        self._ensure_station_identity()
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ensure_station_identity(self) -> None:
        connection = self.connect()
        now = _utc_now()
        for key, value in (
            (STARTED_AT_KEY, now),
            (CODE_IDENTITY_KEY, _code_identity()),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO station (key, value) VALUES (?, ?)", (key, value)
            )
        connection.execute(
            "INSERT OR IGNORE INTO station (key, value) VALUES (?, '0')",
            (EVENT_COUNTER_KEY,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO station (key, value) VALUES (?, '0')",
            (EVENT_GENERATION_KEY,),
        )
        connection.commit()

    def station_value(self, key: str) -> str | None:
        row = (
            self.connect()
            .execute("SELECT value FROM station WHERE key = ?", (key,))
            .fetchone()
        )
        return None if row is None else str(row["value"])

    def set_station_value(self, key: str, value: str) -> None:
        connection = self.connect()
        connection.execute(
            "INSERT INTO station (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        connection.commit()

    def heartbeat(self, *, now: datetime | None = None) -> None:
        self.set_station_value(HEARTBEAT_KEY, (now or datetime.now(tz=UTC)).isoformat())

    # ── ingestion ────────────────────────────────────────────────────────

    def rebuild(self, *, home: str | Path | None = None) -> None:
        """Rebuild the store from the crew home, discarding what it held."""
        connection = self.connect()
        for table in ("pointers", "streams"):
            connection.execute(f"DELETE FROM {table}")  # noqa: S608 - a fixed table name
        connection.commit()
        self.tick(home=home)

    def tick(self, *, home: str | Path | None = None) -> None:
        """Discover the crew home's live pointers and streams and ingest what moved.

        A pointer is parsed only when its ``file_memo.file_signature`` has moved
        against the stored one; a stream is read only past its stored offset.
        """
        root = Path(home) if home is not None else crew_home()
        for path in sorted((root / "live").glob("*.json")):
            self.ingest_pointer(path)
        for stream in self._pointer_streams(root):
            self.ingest_stream(stream)
        self.heartbeat()

    def _pointer_streams(self, home: Path) -> list[Path]:
        """Every stream named by a pointer already ingested."""
        streams: list[Path] = []
        for row in self.pointer_rows():
            record = json.loads(str(row["fields"]))
            recorded = str(record.get("log_path") or "").strip()
            if recorded:
                candidate = Path(recorded)
            elif record.get("run_id"):
                candidate = home / "runs" / str(record["run_id"]) / "stream.jsonl"
            else:
                continue
            if candidate not in streams:
                streams.append(candidate)
        return streams

    def pointer_is_current(self, path: Path) -> bool:
        """Whether the stored signature matches the pointer's identity now."""
        try:
            signature = list(file_memo.file_signature(path))
        except OSError:
            return False
        row = (
            self.connect()
            .execute("SELECT signature FROM pointers WHERE path = ?", (str(path),))
            .fetchone()
        )
        return row is not None and json.loads(str(row["signature"])) == signature

    def ingest_pointer(self, path: Path) -> bool:
        """Parse and store one pointer, returning whether it was re-parsed."""
        if self.pointer_is_current(path):
            return False
        try:
            signature = list(file_memo.file_signature(path))
        except OSError:
            return False
        try:
            record = _read_pointer_record(path)
        except (OSError, ValueError):
            return False
        self._write_pointer_row(path, signature, record)
        return True

    def _write_pointer_row(
        self, path: Path, signature: list[int], record: Mapping[str, Any]
    ) -> None:
        connection = self.connect()
        connection.execute(
            "INSERT INTO pointers (path, signature, run_id, project, session, phase, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET signature = excluded.signature, "
            "run_id = excluded.run_id, project = excluded.project, "
            "session = excluded.session, phase = excluded.phase, "
            "fields = excluded.fields",
            (
                str(path),
                json.dumps(signature),
                str(record.get("run_id") or ""),
                str(record.get("project") or ""),
                str(record.get("session") or ""),
                str(record.get("phase") or ""),
                json.dumps(record, sort_keys=True),
            ),
        )
        connection.commit()

    def ingest_stream(self, path: Path) -> None:
        """Fold one stream from its stored offset, storing the new observation."""
        try:
            info = path.stat()
        except OSError:
            return
        connection = self.connect()
        row = connection.execute(
            "SELECT inode, consumed, fold_state FROM streams WHERE path = ?",
            (str(path),),
        ).fetchone()
        inode = str(info.st_ino)
        consumed = 0
        state: Mapping[str, Any] | None = None
        if row is not None and str(row["inode"]) == inode:
            stored = int(row["consumed"])
            if stored <= info.st_size:
                consumed = stored
                state = json.loads(str(row["fold_state"])) or None
        with path.open("rb") as handle:
            handle.seek(consumed)
            payload = handle.read()
        lines, advanced = _whole_lines(payload)
        observation = self._fold_stream(path, lines, state)
        connection.execute(
            "INSERT INTO streams (path, inode, consumed, fold_state, observation) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, "
            "consumed = excluded.consumed, fold_state = excluded.fold_state, "
            "observation = excluded.observation",
            (
                str(path),
                inode,
                consumed + advanced,
                json.dumps(observation.stream_state, sort_keys=True),
                json.dumps(observation.as_dict(), sort_keys=True),
            ),
        )
        connection.commit()

    def _fold_stream(
        self, path: Path, lines: Iterable[str], state: Mapping[str, Any] | None
    ) -> Any:
        record = self._pointer_for_stream(path)
        dialect = str(record.get("dialect") or "claude").strip() or "claude"
        return _backends.observe_stream(
            backend_name=str(record.get("backend") or dialect),
            backend={"command": dialect, "dialect": dialect},
            lines=lines,
            state=state,
        )

    def _pointer_for_stream(self, path: Path) -> dict[str, Any]:
        for row in self.pointer_rows():
            record = json.loads(str(row["fields"]))
            if str(record.get("log_path") or "") == str(path):
                return record
        return {}

    # ── readers ──────────────────────────────────────────────────────────

    def pointer_rows(self) -> list[dict[str, Any]]:
        rows = (
            self.connect()
            .execute(
                "SELECT path, signature, run_id, project, session, phase, fields "
                "FROM pointers ORDER BY path"
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def stream_rows(self) -> list[dict[str, Any]]:
        rows = (
            self.connect()
            .execute(
                "SELECT path, inode, consumed, fold_state, observation "
                "FROM streams ORDER BY path"
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def observation_rows(self) -> dict[str, dict[str, Any]]:
        """Each stream's stored observation, keyed by the stream's path."""
        return {
            str(row["path"]): json.loads(str(row["observation"]))
            for row in self.stream_rows()
        }

    # ── events, day files and retention ──────────────────────────────────

    def day_files(self) -> list[Path]:
        if not self.day_directory.is_dir():
            return []
        return sorted(self.day_directory.glob(f"{DAY_FILE_PREFIX}*{DAY_FILE_SUFFIX}"))

    def _day_path(self, day: date) -> Path:
        return self.day_directory / _day_file_name(day)

    def _open_day(self, day: date) -> sqlite3.Connection:
        self.day_directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._day_path(day))
        connection.row_factory = sqlite3.Row
        connection.executescript(_DAY_SCHEMA)
        return connection

    def write_event(
        self,
        *,
        audience: str,
        at: datetime,
        project: str | None = None,
        run: str | None = None,
        kind: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        detail: str | None = None,
        duty: str | None = None,
    ) -> int:
        """Append one event to its UTC day's file, returning its sequence id."""
        moment = at.astimezone(UTC)
        day = moment.date()
        counter = self.connect()
        next_id = int(self.station_value(EVENT_COUNTER_KEY) or "0") + 1
        with self._open_day(day) as day_connection:
            day_connection.execute(
                "INSERT INTO events (id, time, project, audience, run, kind, from_state, "
                "to_state, detail, duty) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    next_id,
                    moment.isoformat(),
                    project,
                    audience,
                    run,
                    kind,
                    from_state,
                    to_state,
                    detail,
                    duty,
                ),
            )
            day_connection.commit()
        counter.execute(
            "UPDATE station SET value = ? WHERE key = ?",
            (str(next_id), EVENT_COUNTER_KEY),
        )
        counter.commit()
        # The generation counter lives in the main database and is committed
        # after the day file, because ``PRAGMA data_version`` moves only for
        # commits to the main database; a subscriber wakes on this value.
        self.set_station_value(EVENT_GENERATION_KEY, str(next_id))
        return next_id

    def events(
        self, *, audience: str | None = None, after_id: int = 0
    ) -> list[dict[str, Any]]:
        """Read the events after ``after_id``, attaching each existing day file."""
        connection = self.connect()
        collected: list[dict[str, Any]] = []
        for path in self.day_files():
            connection.execute("ATTACH DATABASE ? AS day", (str(path),))
            try:
                query = (
                    "SELECT id, time, project, audience, run, kind, from_state, to_state, "
                    "detail, duty FROM day.events WHERE id > ?"
                )
                parameters: list[Any] = [after_id]
                if audience is not None:
                    query += " AND audience = ?"
                    parameters.append(audience)
                collected.extend(
                    dict(row)
                    for row in connection.execute(query, parameters).fetchall()
                )
            finally:
                connection.execute("DETACH DATABASE day")
        collected.sort(key=lambda event: int(event["id"]))
        return collected

    def sweep_retention(self, *, now: datetime | None = None) -> list[Path]:
        """Unlink every day file older than the retention window.

        Returns the files removed. The window is measured in whole UTC days
        against ``now``'s own day, so a file survives until its day falls
        outside it.
        """
        today = (now or datetime.now(tz=UTC)).astimezone(UTC).date()
        cutoff = today - timedelta(days=self.retention_days)
        removed: list[Path] = []
        for path in self.day_files():
            day = _day_from_name(path.name)
            if day is None or day >= cutoff:
                continue
            path.unlink()
            removed.append(path)
        return removed


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _code_identity() -> str:
    """The station's code identity: the installed version and the tree it serves."""
    from reckon import __version__

    return f"{__version__}:{Path(__file__).resolve()}"


def store_at_runtime_root(**kwargs: Any) -> StationStore:
    """Convenience wrapper over :meth:`StationStore.at_runtime_root`."""
    return StationStore.at_runtime_root(**kwargs)


def default_store(**kwargs: Any) -> StationStore:
    """The store at this node's runtime root with the environment's root order."""
    return StationStore.at_runtime_root(environ=os.environ, **kwargs)
