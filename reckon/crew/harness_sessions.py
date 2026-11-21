"""The harness's own records of its sessions, and the address each one is messaged by.

The harness keeps one JSON record per live session, under a directory this host
shares, naming the session's process and the name a peer message addresses. A
session can be recorded more than once because a resumed session starts a new
process, and the record left by the finished process is not removed; the live
record is the address a peer can use, so it wins, and where no record is live
the newest one names the session that has gone.

Reachability is the record's process, not the record's existence: a session
whose process has ended is still named, and reads unreachable rather than
absent: a session that has gone is a fact a reader wants told. A session with
no record reads as neither, which is why :func:`addresses` answers nulls for
the name and reachability rather than omitting the session.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon.crew.process_liveness import process_alive

#: Where the harness keeps one record per live session; a test points it at a
#: directory of its own so nothing reads this host's real sessions.
HARNESS_SESSIONS_DIR_ENV = "RECKON_HARNESS_SESSIONS_DIR"

#: The harness's own default location for those records.
DEFAULT_HARNESS_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

#: A session the harness keeps no record for has no name and no reachability,
#: which is distinct from a record naming a process that has ended.
NO_ADDRESS: dict[str, Any] = {"name": None, "reachable": None}


def sessions_dir() -> Path:
    """The directory this process reads session records from."""
    return Path(
        os.environ.get(HARNESS_SESSIONS_DIR_ENV) or DEFAULT_HARNESS_SESSIONS_DIR
    )


def _records() -> list[tuple[Path, Mapping[str, Any]]]:
    """Every readable record, in file order, that names its session and process."""
    found: list[tuple[Path, Mapping[str, Any]]] = []
    try:
        paths = sorted(sessions_dir().glob("*.json"))
    except OSError:
        return found
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping):
            continue
        if not str(record.get("sessionId") or ""):
            continue
        if not isinstance(record.get("pid"), int):
            continue
        found.append((path, record))
    return found


def _preference(record: Mapping[str, Any], alive: bool) -> tuple[bool, int]:
    """The order one record beats another: a live process, then the newest start."""
    started = record.get("startedAt")
    return (alive, started if isinstance(started, int) else 0)


def addresses(sessions: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Each session's peer name and whether its harness process is still alive.

    ``sessions`` bounds the answer to the ids named; ``None`` reads every record
    on the host. A session recorded by both a live and an ended process resolves
    to the live one, whose name is the one a peer message reaches.
    """
    wanted = (
        None
        if sessions is None
        else {str(session) for session in sessions if str(session or "").strip()}
    )
    chosen: dict[str, tuple[tuple[bool, int], dict[str, Any]]] = {}
    for _path, record in _records():
        session = str(record.get("sessionId") or "")
        if wanted is not None and session not in wanted:
            continue
        alive = process_alive(record.get("pid")) is True
        order = _preference(record, alive)
        name = str(record.get("name") or "") or None
        prior = chosen.get(session)
        if prior is None or order >= prior[0]:
            chosen[session] = (order, {"name": name, "reachable": alive})
    return {session: held[1] for session, held in chosen.items()}


def live_sessions() -> list[tuple[str, str]]:
    """Every session whose process is still running, as (session id, peer name)."""
    return sorted(
        (session, str(address["name"] or session))
        for session, address in addresses().items()
        if address["reachable"]
    )
