"""Compute, store and export each session's obligations once per change.

The station is the node's one writer, so the derivation that every coordinator's
prompt hook and every ``crew view obligations`` call need is computed here:
the fleet is read once, each session's duties are sliced from that one reading, and the result is kept in the station's node-local store. A reader
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
import shlex
import socket
import sys
import threading
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew import obligation_snapshot, station_store

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

# An unchanged fleet writes no snapshot, so a heartbeat carried by the last
# write would freeze there and an off-node reader would call a healthy quiet
# station stopped. The file is therefore republished when the station's
# heartbeat has moved and that heartbeat is this old: above the sixty seconds an
# unchanged fleet promises to write nothing in, and far enough below
# :data:`STALE_HEARTBEAT_SECONDS` that a reader never sees the carried beat
# reach the window while the station is alive.
EXPORT_HEARTBEAT_REFRESH_SECONDS = 90.0


@dataclass
class _Memory:
    """What one station process remembers between its own passes."""

    at: datetime
    identity: str = ""
    heartbeat: str = ""


_REFRESHES: dict[tuple[str, str], _Memory] = {}
_EXPORTS: dict[tuple[str, str], _Memory] = {}
_SWEEP_REPORT_LOCK = threading.Lock()


def _sweep_failure_key(project: str) -> str:
    return f"sweep-failures:{project}"


def record_sweep_report(
    store: Any, project: str, session: str, report: Mapping[str, Any]
) -> None:
    """Persist run failures until a completed review pass supersedes them.

    A whole-pass error says nothing about whether an earlier failed run now
    succeeds. Keep that run's error until the review pass returns its per-run
    outcomes. Session sweeps run concurrently, so their read-modify-write of
    the project's record is serialised here.
    """
    reviews = report.get("reviews")
    if (
        not isinstance(reviews, Mapping)
        or "error" in reviews
        or "failed" not in reviews
    ):
        return
    failures = list(reviews["failed"] or ())
    key = _sweep_failure_key(project)
    with _SWEEP_REPORT_LOCK:
        before = store.station_value(key) or "{}"
        sessions = json.loads(before)
        if failures:
            sessions[session] = failures
        else:
            sessions.pop(session, None)
        after = json.dumps(sessions, sort_keys=True)
        if before != after:
            store.set_station_value(key, after)


def _with_sweep_failures(
    payload: Mapping[str, Any], failures: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    rows = list(payload.get("obligations") or ())
    for failure in failures:
        run_id = str(failure["run_id"])
        command = f"reckon crew observe --run {shlex.quote(run_id)}"
        rows.append(
            {
                "kind": "sweep-failed",
                "run_id": run_id,
                "age_seconds": 0,
                "detail": f"station sweep failed: {failure['error']}",
                "next_command": command,
                "action": {"verb": "inspect", "command": command},
            }
        )
    summary = dict(payload.get("summary") or {})
    summary["count"] = len(rows)
    return {**payload, "obligations": rows, "summary": summary}


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def follower_rows(project: str) -> list[dict[str, Any]]:
    """Every delivery registration the project holds, live or released.

    A released registration keeps its file after its follower goes, so the
    list carries sessions nothing is delivering for. Its ``reason`` is the
    registration's own account of what ended it, which is what a rewritten
    snapshot quotes rather than a second reading of the same files.
    """
    from reckon.crew import runs

    return list(runs.list_followers(project))


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
    sweep_record = store.station_value(_sweep_failure_key(project)) or "{}"
    sweep_failures = json.loads(sweep_record)
    due = (
        (memory is not None and memory.identity != sweep_record)
        or changed
        or memory is None
        or (instant - memory.at).total_seconds() >= REFRESH_FLOOR_SECONDS
    )
    if not due:
        return []

    registrations = follower_rows(project)
    sessions = sorted(
        set(sweep_failures)
        | {
            str(row.get("session") or "")
            for row in registrations
            if row.get("live") and row.get("session")
        }
    )
    ended = {
        str(row.get("session") or ""): str(row.get("reason") or "")
        for row in registrations
        if not row.get("live") and row.get("session")
    }
    state = obligation_snapshot.fleet_state(project, now=instant)
    producer = _station_producer(store)
    written: list[str] = []
    for session in sessions:
        payload = _with_sweep_failures(
            obligation_snapshot.payload_for(state, session),
            sweep_failures.get(session, ()),
        )
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
    dropped = _drop_absent(store, project, sessions)
    for session in dropped:
        try:
            _rewrite_without_follower(
                project,
                session,
                state=state,
                now=instant,
                producer=producer,
                reason=ended.get(session, ""),
            )
        except Exception:  # noqa: BLE001 - one session's rewrite costs one session
            _report_failure(project, "absent-snapshot")
    _REFRESHES[key] = _Memory(at=instant, identity=sweep_record)
    return written


def _drop_absent(store: Any, project: str, sessions: Iterable[str]) -> list[str]:
    """Remove this project's rows for sessions no follower is delivering for.

    Returns the sessions whose row went, so the caller can leave each of them a
    snapshot that says so. The removed row is what makes the rewrite a one-off:
    a session already dropped has no row left, so the next pass finds nothing
    to drop and leaves its file alone.
    """
    live = set(sessions)
    dropped: list[str] = []
    for row in store.obligation_rows():
        session = str(row.get("session") or "")
        if session in live:
            continue
        document = stored_document(row) or {}
        if str(document.get("project") or "") == project:
            store.delete_obligation(session)
            dropped.append(session)
    return dropped


def _rewrite_without_follower(
    project: str,
    session: str,
    *,
    state: Any,
    now: datetime,
    producer: Mapping[str, Any],
    reason: str,
) -> Path:
    """Leave one departed session a snapshot that does not defer to a reflex.

    The station stops deriving a session the moment nothing delivers for it,
    and the producer's last snapshot would otherwise stand as that session's
    answer however long it waits. So the departure itself is published: the
    duties are derived once more for the session -- with no live follower, so
    every review dispatch names the command rather than claiming the reflex
    will make it -- and the document says plainly that no follower is
    delivering. What it must not leave behind is the earlier text telling a
    coordinator to wait for an actor that has gone.
    """
    payload = obligation_snapshot.payload_for(state, session)
    document = obligation_snapshot.document_for(
        payload,
        computed_at=now,
        stream_offset=0,
        producer=producer,
        findings=state.findings,
    )
    document["follower"] = {
        "live": False,
        "session": session,
        "reason": reason or "no registration remains",
        "statement": (
            "no follower is delivering for this session, so nothing will "
            "dispatch on its behalf; every duty below names the command to run"
        ),
    }
    return obligation_snapshot.write_snapshot(project, session, document)


def _station_producer(store: Any) -> dict[str, Any]:
    """The station's own identity, recorded where a document carries it.

    A stored document is not judged fresh by ``computed_at`` -- it stands until
    the fleet moves -- so the block is identity, not a freshness signal.
    ``started_at`` is the station's own start, read from its store rather than
    stamped at each pass: a per-pass stamp in that field reads as a station
    restart on every document it writes.
    """
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": store.station_value(station_store.STARTED_AT_KEY),
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

    The file is rewritten when the sessions' documents have moved since the
    last export, and never more often than
    :data:`EXPORT_MIN_INTERVAL_SECONDS`. An unchanged fleet still republishes
    once the heartbeat the file carries has moved and is
    :data:`EXPORT_HEARTBEAT_REFRESH_SECONDS` old, so the beat an off-node reader
    judges staleness by stays live while the station is alive; a station that
    has stopped moves no heartbeat and leaves the file alone, which is what
    lets its last word age into a stale reading. It carries ``as_of`` -- when
    this reading was taken -- and the station's heartbeat.
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
    heartbeat = store.station_value("heartbeat")
    carried = "" if heartbeat is None else str(heartbeat)
    if memory is not None and not force:
        unchanged = memory.identity == identity
        if unchanged and memory.heartbeat == carried:
            # Nothing moved, including the beat: this is a station that has
            # stopped, and letting its file age is the correct reading.
            return None
        if (
            unchanged
            and (instant - memory.at).total_seconds() < EXPORT_HEARTBEAT_REFRESH_SECONDS
        ):
            # The beat moved, but the file is younger than the refresh
            # interval, which keeps an unchanged fleet from writing inside the
            # heartbeat refresh interval. The record of the last write is
            # left untouched so the interval keeps running from it.
            return None

    from reckon._store import write_atomically

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
    _EXPORTS[key] = _Memory(at=instant, identity=identity, heartbeat=carried)
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
