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
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Self

from reckon import _backends, file_memo, host
from reckon._timestamps import parse_utc
from reckon.crew import dispatch_watch, recovery
from reckon.crew.run_paths import crew_home

DATABASE_NAME = "station.sqlite3"
DAY_DIRECTORY = "events"
DAY_FILE_PREFIX = "events-"
DAY_FILE_SUFFIX = ".sqlite3"

# How many bytes before a stream's consumed offset are kept as its tail anchor.
# A stream that grows is re-read from here, so a rewrite whose leading bytes
# changed while its offset stayed put is recognised rather than missed.
ANCHOR_BYTES = 64

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
    size        INTEGER NOT NULL DEFAULT -1,
    anchor      BLOB,
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
CREATE TABLE IF NOT EXISTS classifications (
    run_id     TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    reuse_key  TEXT NOT NULL,
    snapshot   TEXT NOT NULL
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
    duty       TEXT,
    payload    TEXT
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


def _read_range(path: Path, offset: int, limit: int | None = None) -> bytes:
    """Read ``limit`` bytes at ``offset``, or to end of file when ``limit`` is None.

    Every byte the store reads from a stream passes through here, so the volume
    it reads per tick is one named step a test can count rather than an
    emergent property of the code below.
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read() if limit is None else handle.read(limit)


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
    """The UTC day a day file's name encodes, or None for a foreign name.

    The day is a calendar date, so the shared parser reads the stamp and the
    name must then be exactly the form that date writes. A stamp carrying a
    time or a zone parses through the shared parser but does not round-trip,
    which keeps the accepted names identical to the ones ``_day_file_name``
    writes rather than widening the domain to every instant.
    """
    if not name.startswith(DAY_FILE_PREFIX) or not name.endswith(DAY_FILE_SUFFIX):
        return None
    stamp = name[len(DAY_FILE_PREFIX) : -len(DAY_FILE_SUFFIX)]
    parsed = parse_utc(stamp)
    if parsed is None or parsed.date().isoformat() != stamp:
        return None
    return parsed.date()


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
        self._ensure_stream_columns(connection)
        connection.commit()
        self._connection = connection
        self._ensure_station_identity()
        return connection

    @staticmethod
    def _ensure_stream_columns(connection: sqlite3.Connection) -> None:
        """Add the columns a store written before them lacks, keeping its rows."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(streams)").fetchall()
        }
        if "size" not in columns:
            connection.execute(
                "ALTER TABLE streams ADD COLUMN size INTEGER NOT NULL DEFAULT -1"
            )
        if "anchor" not in columns:
            connection.execute("ALTER TABLE streams ADD COLUMN anchor BLOB")

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
        present = set((root / "live").glob("*.json"))
        for path in sorted(present):
            self.ingest_pointer(path)
        connection = self.connect()
        connection.executemany(
            "DELETE FROM pointers WHERE path = ?",
            [
                (row["path"],)
                for row in self.pointer_rows()
                if Path(row["path"]) not in present
            ],
        )
        connection.commit()
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
        """Fold one stream from its stored offset, storing the new observation.

        A stream whose size has not moved since the last read is not read at
        all, so a trailing partial record waits for the bytes that complete it
        rather than being re-read every tick. A grown stream is checked against
        the tail anchor stored beside its offset before the new bytes are read;
        a stream whose inode changed, whose size is below its consumed offset
        or below its stored last-read size, or whose anchor no longer matches
        is folded from its start, so a rewrite is refolded rather than folded
        on top of bytes that no longer exist.
        """
        try:
            info = path.stat()
        except OSError:
            return
        connection = self.connect()
        row = connection.execute(
            "SELECT inode, consumed, size, anchor, fold_state FROM streams "
            "WHERE path = ?",
            (str(path),),
        ).fetchone()
        inode = str(info.st_ino)
        size = int(info.st_size)
        consumed = 0
        state: Mapping[str, Any] | None = None
        prefix = b""
        if row is not None and str(row["inode"]) == inode:
            stored_consumed = int(row["consumed"])
            stored_size = int(row["size"])
            stored_anchor = bytes(row["anchor"] or b"")
            if size == stored_size:
                return
            if size >= stored_consumed and size >= stored_size:
                prefix = self._anchor_before(path, stored_consumed)
                if prefix == stored_anchor:
                    consumed = stored_consumed
                    state = json.loads(str(row["fold_state"])) or None
        payload = _read_range(path, consumed) if size > consumed else b""
        lines, advanced = _whole_lines(payload)
        observation = self._fold_stream(path, lines, state)
        new_consumed = consumed + advanced
        anchor = (prefix + payload[:advanced])[-ANCHOR_BYTES:]
        connection.execute(
            "INSERT INTO streams (path, inode, consumed, size, anchor, fold_state, "
            "observation) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, "
            "consumed = excluded.consumed, size = excluded.size, "
            "anchor = excluded.anchor, fold_state = excluded.fold_state, "
            "observation = excluded.observation",
            (
                str(path),
                inode,
                new_consumed,
                size,
                anchor,
                json.dumps(observation.stream_state, sort_keys=True),
                json.dumps(observation.as_dict(), sort_keys=True),
            ),
        )
        connection.commit()

    def _anchor_before(self, path: Path, offset: int) -> bytes:
        """The bytes immediately before ``offset``, up to one anchor's length."""
        start = max(0, offset - ANCHOR_BYTES)
        return _read_range(path, start, offset - start)

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

    # ── classification ───────────────────────────────────────────────────

    def _classification_cache(self) -> dict[str, tuple[str, dict[str, Any]]]:
        """The stored verdicts, keyed as the producer's reuse path keys them."""
        cache: dict[str, tuple[str, dict[str, Any]]] = {}
        for row in self.classification_rows():
            passed = row.get("reuse_key")
            snapshot = row.get("snapshot")
            if not isinstance(passed, str) or not isinstance(snapshot, Mapping):
                continue
            cache[str(row["run_id"])] = (passed, dict(snapshot))
        return cache

    def classification_rows(self) -> list[dict[str, Any]]:
        rows = (
            self.connect()
            .execute(
                "SELECT run_id, path, reuse_key, snapshot FROM classifications "
                "ORDER BY run_id"
            )
            .fetchall()
        )
        return [
            {
                "run_id": str(row["run_id"]),
                "path": str(row["path"]),
                "reuse_key": str(row["reuse_key"]),
                "snapshot": json.loads(str(row["snapshot"])),
            }
            for row in rows
        ]

    def classifications(self) -> dict[str, dict[str, Any]]:
        """Each live run's stored verdict, keyed by its run id."""
        return {
            str(row["run_id"]): row["snapshot"] for row in self.classification_rows()
        }

    def _write_classification(
        self, run_id: str, path: Path, reuse_key: str, snapshot: Mapping[str, Any]
    ) -> None:
        connection = self.connect()
        connection.execute(
            "INSERT INTO classifications (run_id, path, reuse_key, snapshot) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
            "path = excluded.path, reuse_key = excluded.reuse_key, "
            "snapshot = excluded.snapshot",
            (
                run_id,
                str(path),
                reuse_key,
                json.dumps(dict(snapshot), sort_keys=True),
            ),
        )
        connection.commit()

    def _drop_absent_classifications(self, present: set[str]) -> None:
        connection = self.connect()
        for row in connection.execute("SELECT run_id FROM classifications").fetchall():
            stored = str(row["run_id"])
            if stored not in present:
                connection.execute(
                    "DELETE FROM classifications WHERE run_id = ?", (stored,)
                )
        connection.commit()

    def classify_tick(
        self,
        *,
        now_seconds: float | None = None,
        stall_seconds: int = 900,
    ) -> dict[str, dict[str, Any]]:
        """Re-derive each live run's verdict, classifying only the runs that moved.

        A run whose classification inputs are unchanged since its stored verdict
        and whose liveness reading is unchanged takes only the quiet-beat
        reading: its silence is recomputed from the stream instant recorded on
        the stored verdict and the clock, through the same stall reading a
        fresh classification uses. A run whose reuse key moved, or whose
        liveness the process table now reads differently, is classified afresh
        once. The stored verdicts are the station's own across passes, so a
        restarted station serves them exactly as the producer's memory does.

        The verdicts are keyed and reused by
        :func:`reckon.crew.recovery_watch._snapshot_reuse_key`, the identity of
        every input a classification is a function of, so the manifest, the
        run's records, the review store and the tree head are covered as well
        as the pointer and the stream. A worker that finishes by writing its
        manifest moves neither its pointer nor its stream, and its delivery is
        still seen on the tick that manifest lands.
        """
        moment = recovery._utc_seconds() if now_seconds is None else float(now_seconds)
        cache = self._classification_cache()
        verdicts: dict[str, dict[str, Any]] = {}
        present: set[str] = set()
        for row in self.pointer_rows():
            path = Path(str(row["path"]))
            try:
                record = json.loads(str(row["fields"]))
            except ValueError:
                continue
            if not isinstance(record, Mapping) or not record.get("run_id"):
                continue
            run_id = str(record["run_id"])
            present.add(run_id)
            verdict = recovery._watch_snapshot(
                record,
                moment=moment,
                stall_seconds=stall_seconds,
                cache=cache,
            )
            verdicts[run_id] = verdict
            served = cache.get(run_id)
            if served is not None:
                self._write_classification(run_id, path, served[0], served[1])
        self._drop_absent_classifications(present)
        return verdicts

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
                "SELECT path, inode, consumed, size, fold_state, observation "
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
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(events)")
        }
        if "payload" not in columns:
            connection.execute("ALTER TABLE events ADD COLUMN payload TEXT")
            connection.commit()
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
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        """Append one event to its UTC day's file, returning its sequence id."""
        moment = at.astimezone(UTC)
        day = moment.date()
        counter = self.connect()
        next_id = int(self.station_value(EVENT_COUNTER_KEY) or "0") + 1
        with closing(self._open_day(day)) as day_connection:
            day_connection.execute(
                "INSERT INTO events (id, time, project, audience, run, kind, from_state, "
                "to_state, detail, duty, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    json.dumps(dict(payload), sort_keys=True)
                    if payload is not None
                    else None,
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

    def recorded_fleet(self, project: str) -> dict[str, dict[str, Any]]:
        """The last fleet image committed beside this project's event rows."""
        raw = self.station_value(f"fleet:{project}")
        return json.loads(raw) if raw else {}

    def recorded_projects(self) -> set[str]:
        """Projects whose previous tick can still contain a departure."""
        return {
            str(row["key"])[len("fleet:") :]
            for row in self.connect().execute(
                "SELECT key FROM station WHERE key LIKE 'fleet:%'"
            )
        }

    def record_tick(
        self,
        project: str,
        events: Iterable[Mapping[str, Any]],
        fleet: Mapping[str, Mapping[str, Any]],
        *,
        at: datetime,
    ) -> None:
        """Commit events, their sequence and the next fleet image in one tick.

        The day file is attached before the transaction starts. A killed writer
        therefore rolls back an unfinished tick and a restarted writer compares
        against the image committed with the last completed one.
        """
        rows = list(events)
        connection = self.connect()
        with closing(self._open_day(at.astimezone(UTC).date())):
            pass
        connection.execute(
            "ATTACH DATABASE ? AS tick_day",
            (str(self._day_path(at.astimezone(UTC).date())),),
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            sequence = int(self.station_value(EVENT_COUNTER_KEY) or "0")
            for event in rows:
                sequence += 1
                connection.execute(
                    "INSERT INTO tick_day.events (id, time, project, audience, run, kind, "
                    "from_state, to_state, detail, duty, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        at.astimezone(UTC).isoformat(),
                        project,
                        event.get("audience") or "*",
                        event.get("run_id"),
                        event.get("event"),
                        event.get("from_state"),
                        event.get("to_state"),
                        event.get("detail"),
                        event.get("duty"),
                        json.dumps(dict(event), sort_keys=True),
                    ),
                )
            connection.execute(
                "UPDATE station SET value = ? WHERE key = ?",
                (str(sequence), EVENT_COUNTER_KEY),
            )
            if rows:
                connection.execute(
                    "UPDATE station SET value = ? WHERE key = ?",
                    (str(sequence), EVENT_GENERATION_KEY),
                )
            connection.execute(
                "INSERT INTO station (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"fleet:{project}", json.dumps(dict(fleet), sort_keys=True)),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("DETACH DATABASE tick_day")

    def events(
        self, *, audience: str | None = None, after_id: int = 0
    ) -> list[dict[str, Any]]:
        """Read the events after ``after_id``, attaching each existing day file."""
        connection = self.connect()
        collected: list[dict[str, Any]] = []
        for path in self.day_files():
            connection.execute("ATTACH DATABASE ? AS day", (str(path),))
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA day.table_info(events)")
                }
                if "payload" in columns:
                    query = (
                        "SELECT id, time, project, audience, run, kind, from_state, "
                        "to_state, detail, duty, payload FROM day.events WHERE id > ?"
                    )
                else:
                    query = (
                        "SELECT id, time, project, audience, run, kind, from_state, "
                        "to_state, detail, duty, NULL AS payload FROM day.events "
                        "WHERE id > ?"
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
