"""A follower's durable place in its project's watch stream.

A follower already carried a checkpoint, but only through the environment an
in-place process reload hands to its replacement. A re-arm is a new process with
no such environment, so it began from nothing: it replayed a baseline row for
every live run, each stamped with the moment it attached, and it started reading
at the stream's current end, so every margin of transitions written while
nothing was attached was skipped. A batch of rows under one timestamp followed
by a gap is what that defect describes.

This module makes the place durable: one small record per project and session,
written into the follower directory as lines are delivered and read back by the
next arming of that session. It holds the stream path, enough of the stream's
identity to tell a replaced or truncated file from the same one, the byte offset
after the last delivered line, and the state last reported for each run.

The record is written atomically — a temporary file in the same directory,
flushed and renamed over the target — so a reader never sees a half-written
checkpoint, and a crash mid-write leaves the previous one intact.

Beside that place sits the pane's memory: a log of the rows the follower
rendered, each one's text exactly as it was first drawn and the stamp it was
drawn under. A re-arm replays that log above its own fresh rows, so a reader
who re-arms a Monitor every half hour keeps the view rather than watching it
empty. The log is capped — most recent rows, most recent hours, whichever
admits fewer — so a session left armed for a week does not replay a week.

The log is one row per line, appended: recording a row must not cost the pane
the line it is recording, so the file is rewritten only when it has grown well
past its cap rather than on every row.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# The record's own schema version, so a later shape change is recognised rather
# than misread as the current one.
CHECKPOINT_VERSION = 1

_SUFFIX = ".checkpoint.json"

_HISTORY_SUFFIX = ".history.json"

# The pane's memory, before a flight config layer overrides it.
DEFAULT_HISTORY_ROWS = 100
DEFAULT_HISTORY_SECONDS = 4 * 60 * 60

# A row the follower drew, and a row that marks the drawing style changing. Both
# live in one log so a replay keeps them in the order a reader saw them: the
# rows above the marker were drawn by the format that preceded it.
ROW_KIND = "row"
FORMAT_CHANGED_KIND = "format-changed"
FORMAT_CHANGED_TEXT = "── ticker format updated ──"


def _session_token(session: str | None) -> str:
    """The file-name token for one session, stable across arms.

    Mirrors the registration lock's own naming, so a session's checkpoint sits
    beside its neighbours under a readable stem plus a digest, and a session
    name carrying punctuation still yields a safe, collision-resistant name.
    """
    text = session or ""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", text).strip("-") or "session"
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"{readable}-{digest}"


def checkpoint_path(project: str, session: str | None) -> Path:
    """The durable checkpoint for one project and session."""
    from reckon.crew import runs

    return runs.follower_dir(project) / f"{_session_token(session)}{_SUFFIX}"


def stream_identity(path: str | Path) -> dict[str, Any]:
    """Describe a stream file well enough to recognise the same one again.

    Device and inode together answer *replaced*: an append leaves both alone,
    while a fresh file at the same path — a producer that rotated or was reset —
    changes the inode. Size is carried beside them but is not part of identity,
    because a stream grows as it is written and a size mismatch is not evidence
    of a different file; the caller compares the recorded offset against the
    current size to detect *truncated*.

    This reads the path, so it describes whatever the path names at this instant.
    A follower recording an offset it took from an already-open file must use
    :func:`identity_of` on that handle instead: between the read and the
    recording the path may name a replacement, and the offset belongs to the
    file the reader actually has.
    """
    stat = Path(path).stat()
    return {"dev": int(stat.st_dev), "ino": int(stat.st_ino)}


def identity_of(handle: Any) -> dict[str, Any]:
    """Describe the open file behind ``handle``, not the path it was opened at.

    An offset recorded against a file the caller holds open must be paired with
    that file's identity. Re-deriving the identity from the path can pick up a
    replacement that landed in between, and the resulting record pairs the old
    offset with the new file's inode — a place that reads as continuable and is
    not, so the next arming seeks into the wrong stream instead of restarting.
    """
    stat = os.fstat(handle.fileno())
    return {"dev": int(stat.st_dev), "ino": int(stat.st_ino)}


def continues(record: Mapping[str, Any], path: str | Path) -> bool:
    """Whether a checkpoint can be continued against the stream at ``path``.

    Continuable means all of: the record names this same stream path, the file
    at that path is the one the record was written against (same device and
    inode), and the file has not been truncated below the recorded offset. Any
    other case — a replaced file, a truncated one, a missing one — is the
    caller's signal to fall back to state rather than seek to a byte offset that
    no longer means what it did.
    """
    if str(record.get("stream_path") or "") != str(path):
        return False
    offset = record.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return False
    recorded = record.get("stream_identity")
    if not isinstance(recorded, Mapping):
        return False
    try:
        current = stream_identity(path)
    except OSError:
        return False
    if recorded.get("dev") != current["dev"] or recorded.get("ino") != current["ino"]:
        return False
    try:
        size = Path(path).stat().st_size
    except OSError:
        return False
    return offset <= size


def read(project: str, session: str | None) -> dict[str, Any]:
    """Return the durable checkpoint for one session, or ``{}`` when none.

    An absent, unreadable or older-shaped record is no checkpoint: the caller
    then arms as though this were a first attachment. A record whose shape does
    not match what this code writes is never partially trusted, because the
    fields it lacks are exactly the ones that say where to resume.
    """
    path = checkpoint_path(project, session)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    if record.get("version") != CHECKPOINT_VERSION:
        return {}
    if str(record.get("project") or "") != project:
        return {}
    if str(record.get("session") or "") != (session or ""):
        return {}
    if not isinstance(record.get("reported"), Mapping):
        return {}
    return record


def write(
    project: str,
    session: str | None,
    *,
    stream_path: str | Path,
    offset: int,
    reported: Mapping[str, str],
    identity: Mapping[str, Any] | None = None,
) -> None:
    """Atomically record a follower's place after the last delivered line.

    Written to a sibling temporary file and renamed over the target, so a reader
    either sees the previous checkpoint whole or the new one whole, never a
    partly written record that would resume from a meaningless offset. A failed
    write is not fatal to the follower: it keeps delivering, and the cost is
    only that a later re-arm resumes from an older place.

    ``identity`` is the stream's own identity when the caller read the offset
    from an open file (:func:`identity_of`), so the recorded identity is the
    file the offset describes. Without it the identity is read from the path,
    which is correct only for an offset taken from the path itself.
    """
    target = checkpoint_path(project, session)
    target.parent.mkdir(parents=True, exist_ok=True)
    if identity is not None:
        recorded_identity: Mapping[str, Any] = {
            str(key): value for key, value in dict(identity).items()
        }
    else:
        try:
            recorded_identity = stream_identity(stream_path)
        except OSError:
            recorded_identity = {}
    record = {
        "version": CHECKPOINT_VERSION,
        "project": project,
        "session": session or "",
        "stream_path": str(stream_path),
        "stream_identity": recorded_identity,
        "offset": int(offset),
        "reported": {str(k): str(v) for k, v in dict(reported).items()},
    }
    payload = json.dumps(record, sort_keys=True)
    _replace_atomically(target, payload)


def _replace_atomically(target: Path, payload: str) -> None:
    """Write ``payload`` to ``target`` so a reader sees old or new, never half.

    A sibling temporary file carries the bytes and is renamed over the target,
    which is atomic within one directory. The file is flushed and fsynced first
    and the directory fsynced after, so the rename itself survives a crash
    rather than leaving the name pointing at nothing.
    """
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def history_path(project: str, session: str | None) -> Path:
    """The rendered-row log for one project and session, beside its checkpoint."""
    from reckon.crew import runs

    return runs.follower_dir(project) / f"{_session_token(session)}{_HISTORY_SUFFIX}"


def _parse_history(raw: str) -> list[dict[str, Any]]:
    """Return the log's rows in order, skipping any line that is not one.

    One row per line, so a row is added by appending it rather than by rewriting
    the file. A line that cannot be read — a tail written when the process died
    mid-line, or a shape this code no longer writes — is skipped rather than
    discarding the rows around it, because a reader's view is worth more than the
    strictness of a memory that is only a convenience.
    """
    kept: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, Mapping):
            continue
        text = row.get("text")
        at = row.get("at")
        if not isinstance(text, str):
            continue
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        kept.append(
            {
                "kind": str(row.get("kind") or ROW_KIND),
                "at": float(at),
                "text": text,
                "run_id": str(row.get("run_id") or ""),
                "state": str(row.get("state") or ""),
            }
        )
    return kept


def seed_states(rows: list[dict[str, Any]]) -> dict[str, str]:
    """The state the pane last showed each run, read from the log's last row.

    A re-arm reads this to continue a run's chain of transitions: the row it
    draws next prints as ``abandoned → working`` rather than starting again from
    the producer's own record, which knows nothing of what the previous arming
    already put on screen. The last row per run wins, because that is the one a reader
    last saw; a row carrying no run or no state (a marker, an inventory row)
    contributes nothing.
    """
    latest: dict[str, str] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "")
        state = str(row.get("state") or "")
        if run_id and state:
            latest[run_id] = state
    return latest


def collapse_format_markers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a format marker that directly follows another, keeping the first.

    A marker says the drawing style changed at this point, so two with no row
    between them say it changed once. A log can hold such a pair — written
    before the append collapsed new ones, or by another writer — and a replay
    that drew both would claim a second switch that never happened. Adjacency is
    the whole test: a marker after an intervening row is a genuine second switch
    and is kept. The repair is idempotent, so applying it on every read is safe.
    """
    kept: list[dict[str, Any]] = []
    for row in rows:
        if (
            row["kind"] == FORMAT_CHANGED_KIND
            and kept
            and kept[-1]["kind"] == FORMAT_CHANGED_KIND
        ):
            continue
        kept.append(row)
    return kept


def read_history(project: str, session: str | None) -> list[dict[str, Any]]:
    """Every row in a session's log, oldest first. Unreadable reads as empty.

    Adjacent format markers are collapsed on the way out, so every reader —
    the replay and the append's own tail check alike, because :func:`append_history`
    reads through here — sees one marker where the log holds a pair.
    """
    path = history_path(project, session)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return collapse_format_markers(_parse_history(raw))


def exists(project: str, session: str | None) -> bool:
    """Whether this session's checkpoint file is present on disk.

    A follower that wrote a place and later finds the file gone must rewrite it
    rather than assume its own recorded place still stands, so its write gate
    asks this instead of trusting its memory of the write.
    """
    return checkpoint_path(project, session).exists()


def cap_history(
    rows: list[dict[str, Any]],
    *,
    now: float,
    max_rows: int,
    max_seconds: float,
) -> list[dict[str, Any]]:
    """Trim a log to the most recent rows within the most recent window.

    Both caps apply: the window drops rows older than ``max_seconds``, then the
    count keeps the last ``max_rows`` of what remains. Applying the window first
    matters because a burst of rows written inside one second would otherwise
    let a long-idle session replay a window's worth it has already aged out.
    """
    cutoff = float(now) - float(max_seconds)
    fresh = [row for row in rows if row["at"] >= cutoff]
    if max_rows == 0:
        # A cap of zero admits nothing. It cannot ride the slice below, where
        # ``fresh[-0:]`` is the whole list and a configuration asking for no
        # rows would return every one of them.
        return []
    if max_rows > 0 and len(fresh) > max_rows:
        return fresh[-max_rows:]
    return fresh


def append_history(
    project: str,
    session: str | None,
    *,
    text: str,
    at: float,
    kind: str = ROW_KIND,
    run_id: str = "",
    state: str = "",
    now: float | None = None,
    max_rows: int = DEFAULT_HISTORY_ROWS,
    max_seconds: float = DEFAULT_HISTORY_SECONDS,
) -> None:
    """Add one rendered row to the log, appending it and bounding the file.

    The row is appended rather than the file rewritten, because this runs in the
    path of every line the reader is waiting for: a rewrite here would put a
    whole-file write and three fsyncs between the ticker and the pane, and a
    follower that slowed its own delivery to record what was drawn has paid for
    its memory with the very thing the memory exists for. The file is rewritten
    only once it has grown past twice the row cap, which bounds it without
    paying a rewrite per row. A write that fails costs a later re-arm its
    history and never costs this arming its pane, so it is not raised.

    ``run_id`` and ``state`` are the run the row drew and the state it drew it
    in; a row that is not a run's transition — the format marker, another
    arming's inventory — leaves them empty and seeds nothing.
    """
    moment = float(at if now is None else now)
    row = {
        "kind": str(kind),
        "at": float(at),
        "text": str(text),
        "run_id": str(run_id),
        "state": str(state),
    }
    target = history_path(project, session)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    existing = read_history(project, session)
    if (
        kind == FORMAT_CHANGED_KIND
        and existing
        and existing[-1]["kind"] == FORMAT_CHANGED_KIND
    ):
        # Two reloads with no row between them are one format switch, so a
        # marker already at the tail stands for this one. Appending regardless
        # would leave two adjacent markers and a replay would draw the switch
        # twice; a marker after an intervening row is a second genuine switch
        # and is kept.
        return
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        return
    rows = [*existing, row]
    if len(rows) <= max(2 * max_rows, max_rows):
        return
    capped = cap_history(rows, now=moment, max_rows=max_rows, max_seconds=max_seconds)
    payload = "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in capped)
    try:
        _replace_atomically(target, payload)
    except OSError:
        return
