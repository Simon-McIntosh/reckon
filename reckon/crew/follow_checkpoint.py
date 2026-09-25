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
    """
    stat = Path(path).stat()
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
) -> None:
    """Atomically record a follower's place after the last delivered line.

    Written to a sibling temporary file and renamed over the target, so a reader
    either sees the previous checkpoint whole or the new one whole, never a
    partly written record that would resume from a meaningless offset. A failed
    write is not fatal to the follower: it keeps delivering, and the cost is
    only that a later re-arm resumes from an older place.
    """
    target = checkpoint_path(project, session)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        identity = stream_identity(stream_path)
    except OSError:
        identity = {}
    record = {
        "version": CHECKPOINT_VERSION,
        "project": project,
        "session": session or "",
        "stream_path": str(stream_path),
        "stream_identity": identity,
        "offset": int(offset),
        "reported": {str(k): str(v) for k, v in dict(reported).items()},
    }
    payload = json.dumps(record, sort_keys=True)
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
