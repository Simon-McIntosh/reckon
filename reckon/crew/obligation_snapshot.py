"""Publish and read one session's obligations snapshot.

A coordinator's prompt hook must answer inside a session's first turn and must
not import the plan, backend or ledger derivation modules, so the derivation
moves to the producer: the watch producer recomputes every registered session's
:func:`reckon.crew.obligations.obligations` payload and writes it to
``<config-home>/crew/obligations/<project>/<session>.json`` by writing a
temporary file and renaming it over the old one, so a reader never sees a
partial file. The derivation is imported inside the writer and never at module
level, so importing this module loads no derivation module and costs one stat
on a turn's opening.

Ages are stored as the UTC instant they are measured from rather than a count
of seconds, so a reader computes an age as now minus that instant and a
snapshot never shows a frozen age.

A snapshot is ``fresh`` when all three hold: a process with the recorded pid is
alive and carries the recorded start time (so a reused pid does not pass), its
code stamp equals the stamp computed for the source a restarted producer would
run, and ``computed_at`` is within :data:`FRESHNESS_WINDOW_SECONDS`. Otherwise
it reads as one of three fixed words: ``no-producer`` when no snapshot was
published or its producer is not running, ``producer-stale-code`` when the
producer runs source other than what a restarted one would run, and
``stale-snapshot`` when the snapshot is older than the window.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# The prompt hook reads a snapshot on every turn's opening, so the window that
# keeps a live producer's snapshot trusted is short enough to notice a stalled
# producer and long enough to survive one skipped sweep.
FRESHNESS_WINDOW_SECONDS = 120.0

# The producer republishes on a floor cadence with no other event, so duties
# that become due by the passage of time appear without a pointer or file move.
FLOOR_TICK_SECONDS = 60.0

FRESH = "fresh"
NO_PRODUCER = "no-producer"
STALE_CODE = "producer-stale-code"
STALE_SNAPSHOT = "stale-snapshot"

_NOT_FRESH_REASONS = (NO_PRODUCER, STALE_CODE, STALE_SNAPSHOT)

# The keys this module adds to the derived payload, and the ones it removes
# from a stored snapshot on the way back to the payload's shape.
_AGE_SECONDS = "age_seconds"
_AGE_SINCE = "age_since"
_OLDEST_AGE_SECONDS = "oldest_age_seconds"
_OLDEST_AGE_SINCE = "oldest_age_since"
_SNAPSHOT_KEYS = ("computed_at", "stream_offset", "producer")


def _config_home() -> Path:
    """Resolve the config home the way the rest of the tool resolves it.

    Repeated here rather than imported: importing the store pulls the package
    that carries every derivation with it, which is the cost this module exists
    to avoid on a prompt hook's path.
    """
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def obligations_home() -> Path:
    """Directory holding every project's published session snapshots."""
    return _config_home() / "crew" / "obligations"


def _project_token(project: str) -> str:
    """A filesystem-safe rendering of a project name."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"


def _session_token(session: str) -> str:
    """A filesystem-safe, collision-free rendering of a session name.

    A session name is arbitrary text, so a name that is not already a safe
    path component is shortened to a readable stem plus a digest of the whole
    name: two names that sanitise alike must not share one snapshot.
    """
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", session).strip("-") or "session"
    if readable == session:
        return readable
    digest = hashlib.sha256(session.encode()).hexdigest()[:12]
    return f"{readable}-{digest}"


def snapshot_dir(project: str) -> Path:
    """Directory holding one project's published session snapshots."""
    return obligations_home() / _project_token(project)


def snapshot_path(project: str, session: str) -> Path:
    """Path of one session's published snapshot."""
    return snapshot_dir(project) / f"{_session_token(session)}.json"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_instant(value: Any) -> datetime | None:
    """Read one stored instant, treating a missing zone as UTC."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


# ── Reader ──────────────────────────────────────────────────────────────────


def read_snapshot(project: str, session: str) -> dict[str, Any] | None:
    """Read one session's snapshot: one stat and one small JSON read.

    An absent or unreadable file is None rather than an error: a session whose
    producer has never published has no snapshot, and the hook that reads this
    speaks a line naming the remedy instead of raising into the session.
    """
    try:
        text = snapshot_path(project, session).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _process_stat_fields(pid: Any) -> list[str]:
    """The space-separated per-process stat fields, or [] when unreadable.

    The comm field is parenthesised and may itself hold spaces and closing
    parentheses, so the split begins after the final ``)``.
    """
    try:
        value = int(pid)
        stat = Path(f"/proc/{value}/stat").read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return []
    return stat[stat.rfind(")") + 2 :].split()


def process_start_time(pid: Any) -> str | None:
    """The kernel start tick that distinguishes a reused process id, or None."""
    fields = _process_stat_fields(pid)
    return fields[19] if len(fields) > 19 else None


def _process_is_running(pid: Any) -> bool:
    """Whether this host's table currently holds that pid as a live process."""
    fields = _process_stat_fields(pid)
    if not fields:
        return False
    # A zombie answers a zero signal while its process has already exited, and
    # a producer is not running once it is one.
    return fields[0] != "Z"


def _records_the_live_producer(producer: Mapping[str, Any]) -> bool:
    """Whether the recorded pid is alive *and* carries the recorded start time.

    The start time is required rather than optional: it is the whole reason it
    is recorded, so a snapshot naming a pid with no start time, or one whose
    start time is unreadable, is not evidence of a live producer.
    """
    recorded = str(producer.get("pid_start_time") or "")
    if not recorded:
        return False
    return process_start_time(producer.get("pid")) == recorded


# ── Code stamp ──────────────────────────────────────────────────────────────

_PACKAGE_DIR = Path(__file__).resolve().parent.parent


def _content_digest(source: Path) -> str:
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        return ""


def source_code_stamp(package_dir: Path | None = None) -> str:
    """A stamp that advances when the source a restarted producer would run changes.

    Keyed on file content rather than mtime and size alone, so a rewritten file
    with identical bytes is not new code. The file set matches what the watch
    producer's own stamp digests, so the producer's recorded stamp and the one
    computed here are comparable.
    """
    root = Path(package_dir) if package_dir is not None else _PACKAGE_DIR
    sources = [root / "cli.py", *sorted((root / "crew").glob("*.py"))]
    stamp = hashlib.sha256()
    for source in sources:
        try:
            source.stat()
        except OSError:
            continue
        stamp.update(str(source.relative_to(root)).encode())
        stamp.update(f":{_content_digest(source)}\n".encode())
    return stamp.hexdigest()


def freshness(
    document: Mapping[str, Any] | None,
    *,
    current_stamp: str | None = None,
    now: datetime | None = None,
) -> str:
    """Classify a stored snapshot: ``fresh`` or one of three fixed reasons."""
    if not isinstance(document, Mapping) or not document:
        return NO_PRODUCER
    producer = document.get("producer")
    producer = producer if isinstance(producer, Mapping) else {}
    if not _records_the_live_producer(producer):
        return NO_PRODUCER
    stamp = source_code_stamp() if current_stamp is None else current_stamp
    if str(producer.get("code_stamp") or "") != stamp:
        return STALE_CODE
    computed = _parse_instant(document.get("computed_at"))
    if computed is None:
        return STALE_SNAPSHOT
    instant = _utc_now() if now is None else now
    if (instant - computed).total_seconds() > FRESHNESS_WINDOW_SECONDS:
        return STALE_SNAPSHOT
    return FRESH


# ── Ages ────────────────────────────────────────────────────────────────────


def _age_in_seconds(value: Any, instant: datetime) -> int:
    stamp = _parse_instant(value)
    if stamp is None:
        return 0
    return max(0, int((instant - stamp).total_seconds()))


def _row_measured_from(row: Any, computed_at: datetime) -> Any:
    """One duty row with its age replaced by the instant it is measured from."""
    if not isinstance(row, Mapping):
        return row
    measured = dict(row)
    measured[_AGE_SINCE] = _measured_from(measured.pop(_AGE_SECONDS, 0), computed_at)
    return measured


def _measured_from(seconds: Any, computed_at: datetime) -> str:
    """The instant an age of ``seconds`` was measured from, as a UTC stamp."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        seconds = 0
    return _iso(computed_at - timedelta(seconds=float(seconds)))


def payload_measured_from(payload: Mapping[str, Any], *, computed_at: datetime) -> dict:
    """The derived payload with every age replaced by its measurement instant."""
    document = dict(payload)
    for key in ("obligations", "acknowledged"):
        rows = document.get(key)
        if isinstance(rows, list):
            document[key] = [_row_measured_from(row, computed_at) for row in rows]
    summary = document.get("summary")
    if isinstance(summary, Mapping):
        summary = dict(summary)
        seconds = summary.pop(_OLDEST_AGE_SECONDS, None)
        if seconds is not None:
            summary[_OLDEST_AGE_SINCE] = _measured_from(seconds, computed_at)
        document["summary"] = summary
    return document


def _row_with_age(row: Any, instant: datetime) -> Any:
    """One duty row with its measurement instant turned back into an age."""
    if not isinstance(row, Mapping):
        return row
    measured = dict(row)
    measured[_AGE_SECONDS] = _age_in_seconds(measured.pop(_AGE_SINCE, None), instant)
    return measured


def live_payload(document: Mapping[str, Any], *, now: datetime | None = None) -> dict:
    """The stored snapshot as the derived payload, ages recomputed at read time.

    This is the shape :func:`reckon.crew.obligations.obligations` returns, so a
    reader hands a fresh snapshot to the formatting it already has. Ages are
    recomputed against the read instant, which is what keeps a snapshot from
    showing a frozen age however long it has been on disk.
    """
    instant = _utc_now() if now is None else now
    payload = {
        key: value for key, value in document.items() if key not in _SNAPSHOT_KEYS
    }
    for key in ("obligations", "acknowledged"):
        rows = payload.get(key)
        if isinstance(rows, list):
            payload[key] = [_row_with_age(row, instant) for row in rows]
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        summary = dict(summary)
        since = summary.pop(_OLDEST_AGE_SINCE, None)
        if since is not None:
            summary[_OLDEST_AGE_SECONDS] = _age_in_seconds(since, instant)
        payload["summary"] = summary
    return payload


# ── Writer ──────────────────────────────────────────────────────────────────


def document_for(
    payload: Mapping[str, Any],
    *,
    computed_at: datetime,
    stream_offset: int,
    producer: Mapping[str, Any],
) -> dict[str, Any]:
    """The payload as a snapshot carries it, plus its producer and provenance.

    The derived fields are carried as the derivation returns them; only the
    ages are stored as the instant they are measured from, so a reader never
    reads a frozen count.
    """
    document = payload_measured_from(payload, computed_at=computed_at)
    document["computed_at"] = _iso(computed_at)
    document["stream_offset"] = int(stream_offset)
    document["producer"] = {
        "pid": producer.get("pid"),
        "pid_start_time": producer.get("pid_start_time"),
        "started_at": producer.get("started_at"),
        "code_stamp": producer.get("code_stamp"),
    }
    return document


def write_snapshot(
    project: str,
    session: str,
    document: Mapping[str, Any],
    *,
    replace: Callable[[str, str], None] = os.replace,
) -> Path:
    """Write one snapshot so a reader never observes a partial file.

    The whole document lands in a unique sibling temporary which is fsynced and
    then renamed over the destination, so the destination is only ever the
    previous snapshot or the whole new one. A writer killed between the two
    leaves the previous snapshot intact and readable, and leaves a temporary
    beside it that nothing reads.

    ``replace`` is the rename, exposed so a test can stop a writer exactly
    between the temporary write and the rename.
    """
    path = snapshot_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    with staging.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    replace(str(staging), str(path))
    return path


# ── Producer sweep ──────────────────────────────────────────────────────────


@dataclass
class _SweepMemory:
    """What one project's producer remembers between its sweeps."""

    identity: dict[str, tuple[int, int]]
    published_at: datetime


_SWEEPS: dict[str, _SweepMemory] = {}


def _stat_identity(directories: Iterable[Any]) -> dict[str, tuple[int, int]]:
    """The stat identity of every file under the given directories.

    Judged per file rather than per directory: an in-place edit that leaves a
    directory's own stat unchanged still moves the file's size or mtime, and
    that is the change the producer republishes on.
    """
    identity: dict[str, tuple[int, int]] = {}
    for directory in directories:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            try:
                if not path.is_file():
                    continue
                metadata = path.stat()
            except OSError:
                continue
            identity[str(path)] = (metadata.st_size, metadata.st_mtime_ns)
    return identity


def sweep(
    project: str,
    *,
    sessions: Iterable[str],
    producer: Mapping[str, Any],
    stream_offset: int,
    transition_fired: bool = False,
    state_dirs: Iterable[Any] = (),
    now: datetime | None = None,
) -> list[Path]:
    """Publish every registered session's snapshot when a trigger fires.

    The three triggers are the producer's own: a pointer transition it has
    already detected, a per-file stat change under the project's state and plan
    directories, and a floor tick so duties that become due by the passage of
    time appear with no other event. Nothing is written when none fires, and
    the first sweep of a producer always writes, so a session has a snapshot
    from the moment its producer takes the seat.

    Returns the paths written, oldest session first.
    """
    instant = _utc_now() if now is None else now
    key = str(snapshot_dir(project))
    memory = _SWEEPS.get(key)
    identity = _stat_identity(state_dirs)
    due = (
        transition_fired
        or memory is None
        or memory.identity != identity
        or (instant - memory.published_at).total_seconds() >= FLOOR_TICK_SECONDS
    )
    if not due:
        return []

    from reckon.crew.obligations import obligations

    written: list[Path] = []
    seen: set[str] = set()
    for session in sessions:
        if not session or session in seen:
            continue
        seen.add(session)
        payload = obligations(project, session)
        document = document_for(
            payload,
            computed_at=instant,
            stream_offset=stream_offset,
            producer=producer,
        )
        written.append(write_snapshot(project, session, document))
    _SWEEPS[key] = _SweepMemory(identity=identity, published_at=instant)
    return written
