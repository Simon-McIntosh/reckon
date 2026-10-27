"""Compute, store and export each session's obligations once per change.

The station is the node's one writer, so the derivation that every coordinator's
prompt hook and every ``crew view obligations`` call used to run for itself now
runs here: the fleet is read once, each session's duties are sliced from that
one reading, and the result is kept in the station's node-local store. A reader
on the node queries that store and pays no derivation at all.

A reader off the node cannot open the store -- it is node-local by
:mod:`reckon.crew.station_store`'s guard -- so the station republishes one
snapshot per project into the crew home by atomic replace. The snapshot carries
when it was taken (``as_of``) and the station's own heartbeat, so an off-node
reader can tell a quiet fleet from a station that stopped: a heartbeat older
than :data:`STALE_HEARTBEAT_SECONDS` is reported stale, with its age, rather
than being read as a current answer.

Two clocks bound the writing. The derivation runs when the station sees a change,
when no row is stored yet, and on a floor beat for duties that fall due by the
clock alone; the export is rewritten only when what it would carry has moved, and
at most once every :data:`EXPORT_MIN_INTERVAL_SECONDS`, so an unchanged fleet
leaves the file untouched however long it runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew import obligation_snapshot

# How long a stored row may stand before the derivation runs again with no
# change: duties age on the clock and a floor beat is what lets a row's ages
# and a newly due duty show. This is the snapshot producer's own floor cadence,
# kept as one number so the station and the producer do not drift.
REFRESH_FLOOR_SECONDS = obligation_snapshot.FLOOR_TICK_SECONDS

# The export is rewritten at most this often. A station ticks every second and
# a fleet can move on several ticks in a row; a reader off the node needs the
# answer within one polling interval, not on every tick.
EXPORT_MIN_INTERVAL_SECONDS = 10.0

# A station's heartbeat older than this is reported stale rather than trusted.
# The station writes it on every tick, so this is the snapshot producer's own
# freshness window: long enough to survive a stalled machine and short enough
# that a stopped station is not read as a quiet fleet.
STALE_HEARTBEAT_SECONDS = obligation_snapshot.FRESHNESS_WINDOW_SECONDS


@dataclass
class _Memory:
    """What one station process remembers between its own passes."""

    at: datetime
    identity: str = ""


_REFRESHES: dict[tuple[str, str], _Memory] = {}
_EXPORTS: dict[tuple[str, str], _Memory] = {}


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def live_sessions(project: str) -> list[str]:
    """The sessions a follower is currently delivering for, oldest name first.

    The same registrations the producer's sweep publishes for: a released
    registration keeps its file after its follower goes, and deriving for one
    would spend the station on a session nobody is coordinating under.
    """
    from reckon.crew import runs

    return sorted(
        {
            str(row.get("session") or "")
            for row in runs.list_followers(project)
            if row.get("live") and row.get("session")
        }
    )


def payload_identity(payload: Mapping[str, Any]) -> str:
    """A digest of a payload that does not move as its duties age.

    Ages are measured from the read instant, so a fleet that has not changed
    still derives a payload whose ``age_seconds`` differ from the stored one.
    The identity is what "changed_at" means here: the duties themselves, with
    every age removed, so an unchanged fleet is recognised as unchanged.
    """
    trimmed = json.loads(json.dumps(dict(payload)))
    for key in ("obligations", "acknowledged"):
        rows = trimmed.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    row.pop("age_seconds", None)
                    row.pop("age_since", None)
    summary = trimmed.get("summary")
    if isinstance(summary, dict):
        summary.pop("oldest_age_seconds", None)
        summary.pop("oldest_age_since", None)
    return hashlib.sha256(
        json.dumps(trimmed, sort_keys=True).encode("utf-8")
    ).hexdigest()


def stored_document(row: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The document inside one stored row's payload wrapper, if it carries one."""
    if not isinstance(row, Mapping):
        return None
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    document = payload.get("document")
    return document if isinstance(document, Mapping) else None


def stored_identity(row: Mapping[str, Any] | None) -> str:
    if not isinstance(row, Mapping):
        return ""
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("identity") or "")


def refresh(
    store: Any,
    *,
    project: str,
    changed: bool = True,
    now: datetime | None = None,
) -> list[str]:
    """Derive and store each live session's obligations when the fleet moved.

    Returns the sessions whose stored row was rewritten. A row whose derived
    identity equals the stored one is left exactly as it stands -- its
    ``changed_at`` and the instants its ages are measured from included -- so
    a reader sees when the duties actually last moved rather than when the
    station last looked.
    """
    instant = _utc_now() if now is None else now
    key = (str(getattr(store, "root", "")), project)
    memory = _REFRESHES.get(key)
    due = (
        changed
        or memory is None
        or (instant - memory.at).total_seconds() >= REFRESH_FLOOR_SECONDS
    )
    if not due:
        return []

    sessions = live_sessions(project)
    state = obligation_snapshot.fleet_state(project, now=instant)
    producer = _station_producer()
    written: list[str] = []
    for session in sessions:
        payload = obligation_snapshot.payload_for(state, session)
        identity = payload_identity(payload)
        stored = store.read_obligation(session)
        document = stored_document(stored) or {}
        if (
            stored is not None
            and str(document.get("project") or "") == project
            and stored_identity(stored) == identity
        ):
            continue
        published = obligation_snapshot.document_for(
            payload,
            computed_at=instant,
            stream_offset=0,
            producer=producer,
            findings=state.findings,
        )
        store.write_obligation(
            session,
            {"identity": identity, "document": published},
            changed_at=instant,
        )
        written.append(session)
    _drop_absent(store, project, sessions)
    _REFRESHES[key] = _Memory(at=instant)
    return written


def _drop_absent(store: Any, project: str, sessions: Iterable[str]) -> None:
    """Remove this project's rows for sessions no follower is delivering for."""
    live = set(sessions)
    for row in store.obligation_rows():
        session = str(row.get("session") or "")
        if session in live:
            continue
        document = stored_document(row) or {}
        if str(document.get("project") or "") == project:
            store.delete_obligation(session)


def _station_producer() -> dict[str, Any]:
    """The station's own identity, recorded where a document carries it.

    A stored document is not judged fresh by ``computed_at`` -- it stands until
    the fleet moves -- so the block is identity, not a freshness signal.
    """
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": _iso(_utc_now()),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def export_snapshot(
    store: Any,
    *,
    project: str,
    home: str | Path,
    now: datetime | None = None,
    force: bool = False,
) -> Path | None:
    """Republish one project's snapshot for readers off the node.

    The file is rewritten only when the sessions' documents have moved since
    the last export, and never more often than
    :data:`EXPORT_MIN_INTERVAL_SECONDS`. It carries ``as_of`` -- when this
    reading was taken -- and the station's heartbeat, which is what an off-node
    reader judges staleness by.
    """
    instant = _utc_now() if now is None else now
    key = (str(home), project)
    memory = _EXPORTS.get(key)
    if (
        memory is not None
        and not force
        and (instant - memory.at).total_seconds() < EXPORT_MIN_INTERVAL_SECONDS
    ):
        return None

    documents: dict[str, Any] = {}
    for row in store.obligation_rows():
        document = stored_document(row)
        if document is None:
            continue
        if str(document.get("project") or "") != project:
            continue
        documents[str(row.get("session") or "")] = dict(document)
    identity = hashlib.sha256(
        json.dumps(documents, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if memory is not None and not force and memory.identity == identity:
        _EXPORTS[key] = _Memory(at=instant, identity=identity)
        return None

    from reckon._store import write_atomically

    heartbeat = store.station_value("heartbeat")
    snapshot = {
        "project": project,
        "as_of": _iso(instant),
        "station": {
            "host": socket.gethostname(),
            "job": os.environ.get("SLURM_JOB_ID", ""),
            "heartbeat": heartbeat,
        },
        "sessions": documents,
    }
    path = obligation_snapshot.station_snapshot_path(project, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_atomically(path, lambda handle: handle.write(body), binary=True)
    _EXPORTS[key] = _Memory(at=instant, identity=identity)
    return path


def publish(
    store: Any,
    *,
    project: str,
    home: str | Path,
    changed: bool,
    now: datetime | None = None,
) -> Path | None:
    """Refresh this project's rows, then export them; never raise into a tick.

    One project's unreadable input must not cost the node its station: the
    traceback goes to the station's log, exactly as a failed obligation sweep's
    does, and the next tick tries again.
    """
    try:
        refresh(store, project=project, changed=changed, now=now)
    except Exception:  # noqa: BLE001 - one project's input costs one pass
        _report_failure(project, "refresh")
        return None
    try:
        return export_snapshot(store, project=project, home=home, now=now)
    except Exception:  # noqa: BLE001 - the rows are stored; the export retries
        _report_failure(project, "export")
        return None


def _report_failure(project: str, step: str) -> None:
    print(
        f"station-obligations-failure: {project}: {step} could not complete",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exc(file=sys.stderr)
