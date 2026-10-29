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
it reads as one of four fixed words: ``no-producer`` when no snapshot was
published or its producer is not running, ``no-follower`` when this session has
no snapshot at all while the project's seat names a live producer on another
host, ``producer-stale-code`` when the producer runs source other than what a
restarted one would run, and ``stale-snapshot`` when the snapshot is older than
the window.

A not-fresh snapshot whose producer is plausibly mid-reload is a fourth
reading, outside :func:`freshness`'s fixed words: :func:`reload_in_progress`
answers it from the shared reload window, so a reader can show the last
snapshot instead of the remedy a genuinely absent producer earns.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback
from collections.abc import Iterable, Mapping
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
NO_FOLLOWER = "no-follower"
STALE_CODE = "producer-stale-code"
STALE_SNAPSHOT = "stale-snapshot"

STAT_IDENTITY_INTERVAL_SECONDS = 10.0

# A producer re-executes in place when the source stamp it runs moves. Two
# bounds make a healthy reload slow: the throwaway import proof the reloader
# runs before the exec, and the producer's idle poll interval cap, the longest
# it may sleep between noticing the stamp and acting on it. A window exceeding
# both plus a scheduling margin is what a stale code stamp and a silent
# producer are read against: inside it the seat reads as reloading, outside it
# the seat is a producer to cycle or none at all. The prompt hook and the
# follower in ``reckon.cli`` obtain the window from here -- the hook by loading
# this file directly, the follower importing it -- so the two agree on when a
# producer is stale. The cap is mirrored from
# ``reckon.crew.recovery.IDLE_POLL_INTERVAL_CAP_SECONDS`` because that module
# is imported lazily; a test asserts the two figures agree so this cannot drift.
FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS = 30.0
PRODUCER_POLL_INTERVAL_CAP_SECONDS = 30.0
PRODUCER_RELOAD_WINDOW_MARGIN_SECONDS = 5.0
PRODUCER_RELOAD_WINDOW_SECONDS = (
    FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS
    + PRODUCER_POLL_INTERVAL_CAP_SECONDS
    + PRODUCER_RELOAD_WINDOW_MARGIN_SECONDS
)

# A state directory's ``runs`` subtree changes on every run event, and those
# changes reach the producer as pointer transitions, so the sweep does not walk
# it.
_RUNS_DIRNAME = "runs"

# The keys this module adds to the derived payload, and the ones it removes
# from a stored snapshot on the way back to the payload's shape. The findings
# are one such key: they record what the sweep could not read, which is a fact
# about the reading rather than a duty the derivation returns.
_AGE_SECONDS = "age_seconds"
_AGE_SINCE = "age_since"
_OLDEST_AGE_SECONDS = "oldest_age_seconds"
_OLDEST_AGE_SINCE = "oldest_age_since"
_FINDINGS = "findings"
_SNAPSHOT_KEYS = ("computed_at", "stream_offset", "producer", _FINDINGS)

# A finding is rendered into the duty list under its own kind, so the checklist
# a reader formats names every path the sweep could not read beside the duties
# it did derive. The kind is a reading rather than a duty of the derivation:
# the sweep records it, and the reader reconstructs the row from the stored
# findings rather than trusting an echo.
_UNREADABLE_RECORD_KIND = "unreadable-review-record"

# A review record the reader cannot use is one of two faults, and the duty
# names which so the reader acts rather than waiting. A record met mid-write
# does not parse, and its remedy is to read it again or drop the unfinished
# file. A record that parses but holds findings stating no claim is complete
# and will never change however long a writer is waited on, so its remedy
# rewrites the record or resumes the reviewing run. The reader reports the
# second fault as this message, and the count it carries is how many entries it
# found claimless.
_CLAIMLESS_RECORD_ERROR = re.compile(
    r"(?P<count>\d+) finding\(s\) in this record state no claim"
)

# The fields a stored finding may state its claim under, in the order the
# review reader reads them. Mirrored from
# ``reckon.crew.review.FINDING_CLAIM_FIELDS`` because that module is imported
# lazily and a snapshot is rendered on a prompt hook's path; a test asserts the
# two agree so this cannot drift.
FINDING_CLAIM_FIELDS: tuple[str, ...] = ("text", "summary", "detail", "title")


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


def crew_home() -> Path:
    """Directory holding the fleet's transient state — runs, watch, snapshots."""
    return _config_home() / "crew"


def obligations_home() -> Path:
    """Directory holding every project's published session snapshots."""
    return crew_home() / "obligations"


def _token(name: str, fallback: str) -> str:
    """The filesystem-safe stem ``runs`` gives a project or session name."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-") or fallback


def _lock_stem(name: str, fallback: str) -> str:
    """The ``<readable>-<digest>`` stem a name's advisory-lock path carries."""
    return f"{_token(name, fallback)}-{hashlib.sha256(name.encode()).hexdigest()[:12]}"


def follower_dir(project: str) -> Path:
    """Directory holding one delivery registration per session of a project.

    Repeated here rather than read through ``runs`` for the same reason the
    config home is: the prompt hook must resolve the session without importing
    the crew package, whose facade loads every derivation module.
    """
    return crew_home() / "watch" / f"{_lock_stem(project, 'project')}.followers"


def follower_lock_path(project: str, session: str) -> Path:
    """Advisory-lock path naming one session's delivery registration."""
    return follower_dir(project) / f"{_lock_stem(session, 'session')}.lock"


def read_followers(project: str) -> list[dict[str, Any]]:
    """Read every registration in one project's watch directory, as stored.

    Each row carries the session name (the record's own, falling back to the
    file's stem) and the stored record itself, so a reader resolves a session
    exactly as ``runs.list_followers`` would without loading the crew package.
    An unreadable or malformed registration resolves to no row rather than
    raising into a hook.
    """
    directory = follower_dir(project)
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.lock")):
        try:
            record = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict):
            record = {}
        rows.append(
            {"session": str(record.get("session") or path.stem), "follower": record}
        )
    return rows


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
    """Read one stored instant through the repository's shared UTC parser.

    The shared parser keeps this module's own policy: a value carrying no zone
    is read as UTC, which is what every stamp written here states and the only
    reading that does not depend on the machine that happens to run the code.
    It is imported where it is used rather than at module level because the
    prompt hook loads this file by path, so the package it lives in is not
    guaranteed to be importable while this module's body runs.
    """
    from reckon._timestamps import parse_utc

    return parse_utc(value)


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


# ── Station reader ──────────────────────────────────────────────────────────


def station_runtime_candidate(
    *,
    environ: Mapping[str, str] | None = None,
    run_user_root: str | Path | None = None,
) -> tuple[Path | None, str]:
    """The node's station root and which candidate produced it.

    The order is the store's own -- this node's runtime directory, then the
    per-user runtime directory, then ``TMPDIR`` -- asked of the store rather
    than restated, so a test pins the two against each other. The store is
    imported at the call: the prompt hook loads this module by file path on a
    turn's opening, and the crew package a module-scope import would drag in is
    paid only by a read that has reached the point of opening the database.
    """
    from reckon.crew import station_store

    return station_store.runtime_root_source(
        environ=environ, run_user_root=run_user_root
    )


STATION_SNAPSHOT_NAME = "station-snapshot.json"


def station_database_name() -> str:
    """The store's own file name, asked of the store rather than restated."""
    from reckon.crew import station_store

    return station_store.DATABASE_NAME


# A heartbeat older than this is reported stale rather than trusted: the
# station beats from a thread of its own for as long as its process lives, so
# this is a station that stopped.
STATION_STALE_SECONDS = FRESHNESS_WINDOW_SECONDS

# Beside its heartbeat the station keeps the stage a tick has entered and the
# instants that bound that tick. The beat answers whether the process is alive
# and these answer whether it is making progress, and the two are different
# questions: a tick that spends minutes in one per-project refresh leaves a
# fresh beat beside a tick that has run past the window. The writer names both
# the keys and the stages through here, so the two readings cannot drift.
STATION_TICK_STAGE_KEY = "tick_stage"
STATION_TICK_STARTED_AT_KEY = "tick_started_at"
STATION_TICK_FINISHED_AT_KEY = "tick_finished_at"
STATION_TICK_STAGES = (
    "store-tick",
    "classify",
    "heartbeat",
    "per-project-refresh",
    "publish",
)

# The three readings one station answer carries. ``stopped`` is a beat past the
# window, ``slow`` a fresh beat with one tick running past it, and ``live``
# everything else, including the beat between two ticks.
STATION_LIVE = "live"
STATION_SLOW = "slow"
STATION_STOPPED = "stopped"


def station_snapshot_path(project: str, *, home: str | Path | None = None) -> Path:
    """Where a station republishes one project's snapshot for off-node readers."""
    base = Path(home) if home is not None else crew_home()
    return base / "obligations" / _project_token(project) / STATION_SNAPSHOT_NAME


def station_heartbeat_age(value: Any, *, now: datetime | None = None) -> int | None:
    """How many seconds ago the station beat, or None when the stamp is unreadable."""
    stamped = _parse_instant(value)
    if stamped is None:
        return None
    instant = _utc_now() if now is None else now
    return max(0, int((instant - stamped).total_seconds()))


def station_tick_reading(
    markers: Mapping[str, Any], *, now: datetime | None = None
) -> tuple[str, int | None]:
    """The stage the station's running tick is in, and how long it has run.

    A tick is running when the start instant it recorded is later than the last
    finish the same station recorded, which is what tells the tick between two
    beats from one that never returned. The stage is returned either way, so a
    reader can name the work a station was last in, and the age only for a tick
    still running: a finished tick's age measures nothing a reader acts on.
    """
    stage = str(markers.get(STATION_TICK_STAGE_KEY) or "")
    started = _parse_instant(markers.get(STATION_TICK_STARTED_AT_KEY))
    if started is None:
        return stage, None
    finished = _parse_instant(markers.get(STATION_TICK_FINISHED_AT_KEY))
    if finished is not None and finished >= started:
        return stage, None
    instant = _utc_now() if now is None else now
    return stage, max(0, int((instant - started).total_seconds()))


def _read_station_document(
    project: str, session: str, *, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, str]] | None:
    """The session's document from the station store, with the station's markers.

    The markers are the station table's own key/value rows -- the heartbeat and
    the tick's stage and bounding instants -- read in the same cursor as the
    document so a reader sees one station's beat beside one station's tick.

    An absent store, a missing row, a row written for another project and an
    unreadable database all answer None: a reader falls through to the exported
    snapshot and then to the producer reading it had before the station existed.
    """
    import sqlite3

    root, _source = station_runtime_candidate(
        environ=os.environ if environ is None else environ
    )
    if root is None:
        return None
    database = root / station_database_name()
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro", uri=True, timeout=1.0
        )
        try:
            row = connection.execute(
                "SELECT payload FROM obligations WHERE session = ?", (session,)
            ).fetchone()
            markers = {
                str(key): str(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM station"
                ).fetchall()
            }
        finally:
            connection.close()
    except sqlite3.Error:
        # A read-only handle cannot open a WAL database until its shared-memory
        # file exists, so a station that has just started is read through an
        # ordinary handle; the statements below are reads either way.
        try:
            connection = sqlite3.connect(database, timeout=1.0)
            try:
                row = connection.execute(
                    "SELECT payload FROM obligations WHERE session = ?", (session,)
                ).fetchone()
                markers = {
                    str(key): str(value)
                    for key, value in connection.execute(
                        "SELECT key, value FROM station"
                    ).fetchall()
                }
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    if row is None:
        return None
    try:
        wrapper = json.loads(str(row[0]))
    except ValueError:
        return None
    document = wrapper.get("document") if isinstance(wrapper, Mapping) else None
    if (
        not isinstance(document, Mapping)
        or str(document.get("project") or "") != project
    ):
        return None
    return dict(document), markers


def _read_station_export(
    project: str, session: str, *, home: str | Path | None = None
) -> dict[str, Any] | None:
    """The session's document from the snapshot a station exported, or None."""
    try:
        text = station_snapshot_path(project, home=home).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        snapshot = json.loads(text)
    except ValueError:
        return None
    if not isinstance(snapshot, Mapping):
        return None
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, Mapping):
        return None
    document = sessions.get(session)
    if not isinstance(document, Mapping):
        return None
    exported = dict(document)
    exported.setdefault("project", snapshot.get("project"))
    station = snapshot.get("station")
    beat = station.get("heartbeat") if isinstance(station, Mapping) else None
    exported["_station_heartbeat"] = beat
    return exported


def read_station_answer(
    project: str,
    session: str,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> dict[str, Any] | None:
    """One session's obligations from the station, or None when it cannot answer.

    The station on this node is read first, then the snapshot a station exported
    for readers off its node. The answer carries the payload in the shape the
    derivation returns and three readings of the station itself: the heartbeat's
    age, the tick's current stage and age, and the ``state`` those two make.

    ``stopped`` is the only state a restart answers, and only the beat decides
    it: a station can be quiet because the fleet is quiet, which is not a
    station that stopped. A fresh beat with a tick that has outlived the window
    is ``slow`` instead -- alive, working, and not something to cycle. An
    exported snapshot names no tick stage, so an off-node reading is either
    ``stopped`` or ``live`` and never claims a progress it cannot see.
    """
    instant = _utc_now() if now is None else now
    found = _read_station_document(project, session, environ=environ)
    source = "station"
    if found is None:
        exported = _read_station_export(project, session, home=home)
        if exported is None:
            return None
        beat = exported.pop("_station_heartbeat", None)
        found = (exported, {"heartbeat": "" if beat is None else str(beat)})
        source = "snapshot"
    document, markers = found
    age = station_heartbeat_age(markers.get("heartbeat"), now=instant)
    stale = age is None or age > STATION_STALE_SECONDS
    stage, tick_age = station_tick_reading(markers, now=instant)
    if stale:
        state = STATION_STOPPED
    elif tick_age is not None and tick_age > STATION_STALE_SECONDS:
        state = STATION_SLOW
    else:
        state = STATION_LIVE
    return {
        "source": source,
        "state": state,
        "payload": live_payload(document),
        "heartbeat_age_seconds": age,
        "tick_stage": stage,
        "tick_age_seconds": tick_age,
        "stale": stale,
    }


# ── Model catalogue notices ─────────────────────────────────────────────────


def model_catalogue_snapshot_path() -> Path:
    """The path the model catalogue publishes its snapshot to.

    Resolved here so the prompt hook reads the notices without importing
    ``reckon.crew.model_catalogue``, whose package the hook must not reach on a
    turn's opening, and so the hook and the catalogue agree on where the
    notices live.
    """
    return crew_home() / "model-catalogue" / "snapshot.json"


def read_model_catalogue_snapshot() -> dict[str, Any]:
    """Read the published model catalogue snapshot, or an empty mapping.

    An absent or unreadable snapshot reads as empty rather than raising: the
    catalogue is refreshed by the watch producer, so a session that has never
    had one simply has no notices.
    """
    try:
        document = json.loads(
            model_catalogue_snapshot_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def unacknowledged_model_notices(
    document: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The snapshot's notices that no acknowledgement has taken up.

    This is the one definition of "unacknowledged": a notice is unacknowledged
    until it carries an ``acknowledgement``. The hook that counts them and the
    view that lists them both read the notices through here, so the count a
    coordinator sees and the list it opens cannot disagree.
    """
    notices = document.get("notices")
    if not isinstance(notices, list):
        return []
    return [
        dict(item)
        for item in notices
        if isinstance(item, Mapping) and not item.get("acknowledgement")
    ]


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
    pid = producer.get("pid")
    recorded = str(producer.get("pid_start_time") or "")
    if not recorded or not _process_is_running(pid):
        return False
    return process_start_time(pid) == recorded


def live_foreign_seat_host(project: str | None) -> str:
    """The host a live foreign seat names, or an empty string.

    The watch module owns the judgement of whether a seat names a foreign host
    whose stream is still fresh, so it is asked rather than re-derived here;
    the import is deferred to this call so the reader's module-level surface,
    which the prompt hook loads on a turn's opening, stays free of it. The host
    is returned rather than a flag so a reader can name the node a live
    producer is on, and every caller that needs only the yes/no tests this.
    """
    if not project:
        return ""
    from reckon.crew import watch_unit

    return watch_unit.live_foreign_seat_host(project)


def live_foreign_seat(project: str | None) -> tuple[str, str]:
    """The live foreign seat's host and stamp, taken from the same record."""
    if not project:
        return "", ""
    from reckon.crew import watch_unit

    return watch_unit.live_foreign_seat(project)


# ── Code stamp ──────────────────────────────────────────────────────────────

_PACKAGE_DIR = Path(__file__).resolve().parent.parent

# The CLI's own modules, named relative to the package directory. The
# CLI-owner test reads this declaration to prove no top-level module carrying a
# command is omitted. The stamp itself covers every module under the package,
# so a command module outside this tuple still advances it.
CLI_MODULE_FILES = (
    "cli.py",
    "cli_entry.py",
    "project_setup_commands.py",
    "crew_dispatch_commands.py",
    "crew_follow_commands.py",
    "crew_run_commands.py",
    "project_maintenance_commands.py",
)

# One source file's content digest, kept beside the stat that vouches for it.
# A stamp is re-read at a bounded cadence and most ticks find nothing new, so
# the mtime and size let an untouched file reuse the digest it had last time
# rather than reading every module on every tick. The memo is shared by every
# caller, so one read of a file serves the producer's stamp and a follower's.
_source_digests: dict[str, tuple[int, int, str]] = {}


def _source_content_digest(source: Path, metadata: os.stat_result) -> str:
    """Return a digest of a source file's bytes, reused while its stat holds.

    The stat is a pre-check, not the identity: a tool that rewrites a file with
    identical bytes — a formatter, a checkout restoring the same revision —
    moves the mtime alone, and a stamp keyed on it reloaded every follower for
    nothing. An unchanged mtime and size reuse the last digest; a moved one
    reads the bytes and finds them the same, so the stamp does not move.
    """
    key = str(source)
    cached = _source_digests.get(key)
    if (
        cached is not None
        and cached[0] == metadata.st_mtime_ns
        and cached[1] == metadata.st_size
    ):
        return cached[2]
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    _source_digests[key] = (metadata.st_mtime_ns, metadata.st_size, digest)
    return digest


def code_stamp(package_dir: Path | None = None) -> str:
    """A stamp that advances when any source a launch can run changes.

    Every ``*.py`` module under the package directory is hashed, not only the
    CLI modules and ``reckon/crew``: the modules a launch is built from —
    ``reckon/_worker_fence.py``, ``reckon/_backends.py``,
    ``reckon/nested_launch.py`` — sit outside those, and a producer that
    reloaded only on a command-module change would keep running stale launch
    code. Keyed on file content rather than mtime and size alone, so a
    rewritten file with identical bytes is not new code.
    """
    root = Path(package_dir) if package_dir is not None else _PACKAGE_DIR
    stamp = hashlib.sha256()
    for source in sorted(root.rglob("*.py")):
        try:
            metadata = source.stat()
        except OSError:
            continue
        stamp.update(str(source.relative_to(root)).encode())
        stamp.update(f":{_source_content_digest(source, metadata)}\n".encode())
    return stamp.hexdigest()


def source_code_stamp(package_dir: Path | None = None) -> str:
    """Read the source identity used by producer records and followers."""
    from reckon import checkout_trust

    package = Path(package_dir) if package_dir is not None else _PACKAGE_DIR
    return checkout_trust.source_code_identity(package)


def freshness_reading(
    document: Mapping[str, Any] | None,
    *,
    project: str | None = None,
    current_stamp: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Classify a snapshot and carry the foreign host used for that verdict.

    ``project`` names the seat to consult when no snapshot is held, and when the
    recorded pid is not running on this host. A snapshot published by a producer
    on another node records that node's pid, which this host's process table
    answers for only by accident. A locally absent pid is not by itself an
    absence: the project's seat and host lease cross the shared home and can
    name a live producer elsewhere. The seat's code stamp identifies the
    producer now holding it, even when its predecessor wrote the snapshot.

    The four not-fresh reasons are ``no-producer`` for an absent producer with
    no live seat behind it, ``no-follower`` for no snapshot at all while a live
    producer stands on another host -- the reading a session earns when it has
    no follower of its own with a fresh lease -- ``producer-stale-code`` for a
    producer running source other than the checkout, and ``stale-snapshot`` for
    an aged publication.
    """
    if not isinstance(document, Mapping) or not document:
        host = live_foreign_seat_host(project)
        return (NO_FOLLOWER if host else NO_PRODUCER), host
    producer = document.get("producer")
    producer = producer if isinstance(producer, Mapping) else {}
    # The seat is consulted only when the recorded producer is not live on this
    # host: it crosses the shared home, and reaching for the watch module to
    # read it pulls in the crew facade, which the prompt path must not touch
    # while a live snapshot already answers the reading.
    host = ""
    seat_stamp = ""
    if not _records_the_live_producer(producer):
        host, seat_stamp = live_foreign_seat(project)
        if not host:
            return NO_PRODUCER, host
    stamp = source_code_stamp() if current_stamp is None else current_stamp
    # Older seat records may carry no stamp; retain their snapshot's reading.
    producer_stamp = seat_stamp or str(producer.get("code_stamp") or "")
    if producer_stamp != stamp:
        return STALE_CODE, host
    computed = _parse_instant(document.get("computed_at"))
    if computed is None:
        return STALE_SNAPSHOT, host
    instant = _utc_now() if now is None else now
    if (instant - computed).total_seconds() > FRESHNESS_WINDOW_SECONDS:
        return STALE_SNAPSHOT, host
    return FRESH, host


def freshness(
    document: Mapping[str, Any] | None,
    *,
    project: str | None = None,
    current_stamp: str | None = None,
    now: datetime | None = None,
) -> str:
    """Return the snapshot's freshness state for callers needing no host."""
    return freshness_reading(
        document, project=project, current_stamp=current_stamp, now=now
    )[0]


def snapshot_age_seconds(
    document: Mapping[str, Any] | None, *, now: datetime | None = None
) -> int | None:
    """A stored snapshot's age in whole seconds, or None when it has no stamp."""
    if not isinstance(document, Mapping):
        return None
    computed = _parse_instant(document.get("computed_at"))
    if computed is None:
        return None
    instant = _utc_now() if now is None else now
    return max(0, int((instant - computed).total_seconds()))


# ── Reload window ───────────────────────────────────────────────────────────


def producer_reload_window_seconds() -> float:
    """The window a stale or silent producer reads as a reload in progress."""
    return PRODUCER_RELOAD_WINDOW_SECONDS


def watch_reload_state(project: str) -> tuple[str | None, dict[str, Any] | None]:
    """The project's recorded reload intent and deferral from one seat read.

    The seat writes the key as it begins an in-place replacement, and the
    replacement clears it by rewriting the record, so its presence marks a
    reload the seat itself declared. The record is read here as plain JSON
    because the prompt hook resolves it without the crew facade, whose
    ``watch_producer_identity`` reads the same file; an unreadable or
    malformed record resolves to None, exactly as that reader treats it.
    """
    path = crew_home() / "watch" / f"{_lock_stem(project, 'project')}.lock"
    try:
        record = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None, None
    if not isinstance(record, dict):
        return None, None
    value = record.get("reload_started_at")
    deferred = record.get("reload_deferral")
    return str(value) if value else None, deferred if isinstance(
        deferred, dict
    ) else None


def reload_in_progress(
    document: Mapping[str, Any] | None,
    *,
    state: str,
    reload_started_at: Any = None,
    now: datetime | None = None,
) -> bool:
    """Whether a producer reload reads as in progress rather than an absence.

    Any of three signals means a reload: the seat recorded a reload intent
    within the window; the snapshot's producer is alive and its code stamp is
    older than the source, so it has not caught up yet; or its last
    publication is within the window although no producer is live, which is
    the gap between the old image exiting and the replacement publishing.
    Outside every signal, a caller answers with its not-fresh line, and the
    remedy that line carries is right because nothing is coming.
    """
    instant = _utc_now() if now is None else now
    window = producer_reload_window_seconds()
    intent = _parse_instant(reload_started_at)
    intent_age = None if intent is None else (instant - intent).total_seconds()
    if intent_age is not None and intent_age <= window:
        return True
    if state == STALE_CODE:
        return True
    if state != NO_PRODUCER:
        return False
    age = snapshot_age_seconds(document, now=instant)
    return age is not None and age <= window


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


def _unreadable_record_remedy(finding: Mapping[str, Any]) -> str:
    """The remedy one unreadable review record's duty carries.

    The reader distinguishes the two faults in what it reports: a record it
    could not parse was met mid-write, so the remedy reads it again once its
    writer finishes or drops the unfinished file; a record that parses but
    holds entries stating no claim is complete, so the remedy rewrites it or
    resumes the reviewing run, naming how many entries are claimless and the
    fields a claim may use. Waiting on a writer that already finished would
    never change the second fault, and its file parsed, so nothing is waited
    on.
    """
    match = _CLAIMLESS_RECORD_ERROR.search(str(finding.get("error") or ""))
    if match is None:
        return "read it again once its writer finishes, or remove the unfinished file"
    return (
        f"{match.group('count')} claimless finding(s) in this record: rewrite "
        "the record or resume the reviewing run, stating each finding's claim "
        f"under one of {', '.join(FINDING_CLAIM_FIELDS)}"
    )


def _unreadable_record_items(findings: Any) -> list[dict[str, Any]]:
    """One duty-shaped row per finding, naming the path it could not read.

    The row is built in the shape every duty carries, because the checklist
    renders one line per duty: the path is the row's identity, the file's own
    name is its node, and the remedy names the fault the reader reported, told
    apart by :func:`_unreadable_record_remedy`. A finding naming no path is not
    renderable and is left out rather than shown as an empty row.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(findings, list):
        return rows
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        path = str(finding.get("path") or "")
        if not path:
            continue
        rows.append(
            {
                "kind": _UNREADABLE_RECORD_KIND,
                "run_id": path,
                "node": Path(path).name,
                "age_seconds": 0,
                "next_command": _unreadable_record_remedy(finding),
            }
        )
    return rows


def _with_unreadable_records(rows: Any, findings: Any) -> list[dict[str, Any]]:
    """The duty rows with one row per stored finding, and none echoed twice.

    The stored findings are the record of what could not be read, so the rows
    are rebuilt from them and any row of the same kind already in the list is
    dropped: a snapshot written before this rendering, whose list carries no
    such row, and one written after it both read back the same.
    """
    kept = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if not (isinstance(row, Mapping) and row.get("kind") == _UNREADABLE_RECORD_KIND)
    ]
    kept.extend(_unreadable_record_items(findings))
    return kept


def live_payload(document: Mapping[str, Any], *, now: datetime | None = None) -> dict:
    """The stored snapshot as the derived payload, ages recomputed at read time.

    This is the shape :func:`reckon.crew.obligations.obligations` returns, so a
    reader hands a fresh snapshot to the formatting it already has, and it
    carries one row per input the sweep could not read so that formatting names
    each skipped path. Ages are recomputed against the read instant, which is
    what keeps a snapshot from showing a frozen age however long it has been on
    disk.
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
    payload["obligations"] = _with_unreadable_records(
        payload.get("obligations"), document.get(_FINDINGS)
    )
    return payload


# ── Writer ──────────────────────────────────────────────────────────────────


def document_for(
    payload: Mapping[str, Any],
    *,
    computed_at: datetime,
    stream_offset: int,
    producer: Mapping[str, Any],
    findings: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The payload as a snapshot carries it, plus its producer and provenance.

    The derived fields are carried as the derivation returns them; only the
    ages are stored as the instant they are measured from, so a reader never
    reads a frozen count. The findings name the inputs the sweep could not
    read, so a reader sees them beside the duties rather than losing them with
    the files.
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
    document[_FINDINGS] = [dict(row) for row in findings]
    return document


def write_snapshot(
    project: str,
    session: str,
    document: Mapping[str, Any],
) -> Path:
    """Publish one complete snapshot, removing the temporary on any failure."""
    from reckon._store import write_atomically

    path = snapshot_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    write_atomically(path, lambda handle: handle.write(payload), binary=True)
    return path


# ── Producer sweep ──────────────────────────────────────────────────────────


@dataclass
class _SweepMemory:
    """What one project's producer remembers between its sweeps."""

    identity: dict[str, tuple[int, int]]
    checked_at: datetime
    published_at: datetime


_SWEEPS: dict[str, _SweepMemory] = {}


def _stat_identity(directories: Iterable[Any]) -> dict[str, tuple[int, int]]:
    """The stat identity of every file under the given directories.

    Judged per file rather than per directory: an in-place edit that leaves a
    directory's own stat unchanged still moves the file's size or mtime, and
    that is the change the producer republishes on.

    A ``runs`` directory directly under one of the roots is not walked. Its
    files change whenever a run records anything, and those changes reach the
    producer as pointer transitions already, so walking them would spend the
    sweep's stat budget on a trigger that carries no new event.
    """
    identity: dict[str, tuple[int, int]] = {}
    for directory in directories:
        root = Path(directory)
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.name == _RUNS_DIRNAME:
                continue
            try:
                if entry.is_file():
                    metadata = entry.stat()
                    identity[str(entry)] = (metadata.st_size, metadata.st_mtime_ns)
                    continue
                for path in sorted(entry.rglob("*")):
                    if not path.is_file():
                        continue
                    metadata = path.stat()
                    identity[str(path)] = (metadata.st_size, metadata.st_mtime_ns)
            except OSError:
                continue
    return identity


def _derivation_module():
    """The obligations derivation, imported where it is used and not before."""
    from reckon.crew import obligations as module

    return module


def _ledger_module():
    """The ledger, imported where it is used and not before."""
    from reckon import ledger as module

    return module


def _review_head_module():
    """The reviewed-head resolution's own module, imported where it is used.

    The module carries the region that resolves each live run's reviewed head
    once; it is reached here rather than through the recovery package so that
    one region spans a whole derivation and its slices.
    """
    from reckon.crew import recovery_review_dispatch as module

    return module


@dataclass
class FleetState:
    """The fleet inputs one sweep's per-session payloads share.

    A project's obligations are mostly a property of the fleet — every live
    pointer classified once, the ledger read once, each run's review looked up
    once — and only the slicing is per session. Deriving these once per sweep
    rather than once per session is what keeps a sweep proportional to the
    fleet instead of to the fleet multiplied by the sessions reading it.
    """

    project: str
    now: datetime
    config: Mapping[str, Any]
    grace: float
    floors: Mapping[str, Any]
    rows: list[dict[str, Any]]
    pointers: dict[str, Mapping[str, Any]]
    acknowledged: dict[str, dict[str, Any]]
    reviews_in_flight: dict[str, set[str]]
    sub_floor: dict[str, list[dict[str, Any]]]
    held_worktrees: dict[str, list[dict[str, Any]]]
    unreconciled: dict[str, int]
    findings: list[dict[str, str]]


def fleet_state(project: str, *, now: datetime | None = None) -> FleetState:
    """Derive everything a sweep's per-session payloads are sliced from.

    The derivation opens the two regions its readers share: one run index built
    once for every ledger read inside it, and one resolution of each live run's
    reviewed head and tree that the classifier, the derivation and its slices
    all read. A derivation nested in a sweep joins the regions already open, so
    the slices the sweep builds afterwards read what the derivation resolved.
    """
    ledger_module = _ledger_module()
    head_module = _review_head_module()
    with ledger_module.index_reuse(), head_module.review_heads_resolve_once():
        return _derive_fleet_state(project, now=now)


def _derive_fleet_state(project: str, *, now: datetime | None = None) -> FleetState:
    """Build the fleet state, inside the regions :func:`fleet_state` opened."""
    module = _derivation_module()
    instant = _utc_now() if now is None else now
    config = module.flight.resolve(project).config
    grace = module.parse_duration(
        str((config.get("fences") or {}).get("unreconciled_run_grace") or "15m")
    )
    reviews_in_flight: dict[str, set[str]] = {}
    pointers: dict[str, Mapping[str, Any]] = {}
    for pointer in module.runs.list_live(project=project):
        session = str(pointer.get("session") or "")
        run_id = str(pointer.get("run_id") or "")
        if run_id:
            pointers[run_id] = pointer
        if session and run_id and module._current_review_in_flight(pointer):
            reviews_in_flight.setdefault(session, set()).add(run_id)
    floors = module.review_dispositions.declared_dimension_floors(config)
    # Every review read this sweep makes happens inside the region -- the
    # classified rows, the sub-floor lookup and the held-worktree scan all
    # reach the review store -- so what could not be read is collected here,
    # once, and travels with the state every session's slice is built from. A
    # read outside the region has nobody to name its skips to, so another
    # reader in this process never reaches this sweep's findings.
    with module.review_module.collect_read_failures() as skipped_records:
        state = FleetState(
            project=project,
            now=instant,
            config=config,
            grace=grace,
            floors=floors,
            rows=module._classified_rows(project),
            pointers=pointers,
            acknowledged=module._acknowledgements_in_force(project, now=instant),
            reviews_in_flight=reviews_in_flight,
            sub_floor=module._sub_floor_items_by_session(project, floors, now=instant),
            held_worktrees=module._held_worktrees_by_session(project, now=instant),
            unreconciled=module.runs.drain_unreconciled_by_session(project),
            findings=[],
        )
    state.findings = list(skipped_records)
    return state


def _duty_kind(module, row: Mapping[str, Any]) -> str:
    """The duty one classified row owes its session, or the empty string.

    Rows whose evidence is a stored review of the run's current head are built
    through the derivation's own review-duty function instead, so this resolves
    the remaining kinds.
    """
    recovery_classification = str(row.get("recovery_classification") or "")
    if recovery_classification in module.RECOVERY_CLASSIFICATION_DUTY_KINDS:
        return module.RECOVERY_CLASSIFICATION_DUTY_KINDS[recovery_classification]
    return module.CLASSIFICATION_DUTY_KINDS.get(
        str(row.get("classification") or ""), ""
    )


def _upstream_hold_item(
    module,
    row: Mapping[str, Any],
    *,
    pointer: Mapping[str, Any],
    now: datetime,
    phase: str,
) -> dict[str, Any]:
    """The duty a request held on its section's refs owes its session.

    It is a waiting duty like a queued request's, built through the same helper
    so the two read alike, and it carries what a queued one cannot: the refs it
    waits on, and which way it is held. A request that is ``awaiting-upstream``
    starts by itself when its last ref lands, so it owes nothing but patience
    and the payload says so. One that is ``blocked-upstream`` will never start
    by itself — a ref it waits on was closed unfinished — so its session is
    told which ref it is and what has to change, rather than left reading a
    waiting row that will not move.

    The refs are read from the pointer the admission pass wrote rather than
    from the plan: the request is held on the refs it was stored with, and a
    plan edited since then must not change what a reader is told it waits on.
    """
    item = module._queued_item(row, placements={}, now=now)
    waits = list(module.queue_admission.section_waits_for(pointer))
    if waits:
        item["waits"] = waits
    if phase == module.queue_admission.BLOCKED_UPSTREAM_PHASE:
        item["next_command"] = (
            "blocked upstream: a section this request waits on was closed "
            f"unfinished ({', '.join(waits) or 'no ref recorded'}) — "
            "redispatch it once that section lands, or abandon this request"
        )
    elif waits:
        item["next_command"] = "waiting upstream: " + ", ".join(waits)
    return item


def payload_for(state: FleetState, session: str) -> dict[str, Any]:
    """One session's obligations payload, sliced from a derived fleet state.

    The shape and the ordering are :func:`reckon.crew.obligations.obligations`'s
    own, composed from the same helpers the derivation uses, so a slice and a
    derivation over the same files are equal field for field. The state's
    findings are the one addition: each names a record the sweep could not
    read, and travels in the duty list so the reader that formats it names the
    path rather than leaving the reader to open the snapshot store.
    """
    module = _derivation_module()
    in_flight = state.reviews_in_flight.get(session, set())
    items: list[dict[str, Any]] = []
    # Read once, for the first queued row this session owns, from the ordering
    # the admission pass admits by; a session with nothing queued never orders
    # anything.
    queued_placements: dict[str, dict[str, int]] | None = None
    for row in state.rows:
        if str(row.get("session") or "") != session:
            continue
        run_id = str(row.get("run_id") or "")
        classification = str(row.get("classification") or "")
        pointer = state.pointers.get(run_id) or {}
        phase = str(pointer.get("phase") or "")
        if phase in module.queue_admission.WAITING_UPSTREAM_PHASES:
            # A request held on its section's refs owes its session a duty, and
            # the phase is where that is recorded rather than in the
            # classification: the classification word for a request that is
            # neither launching nor finished is built from a phase vocabulary
            # this view does not own, so a phase the vocabulary has not learned
            # yet would leave the request listed nowhere. Reading the phase
            # itself is what keeps a held request on the view, and it is read
            # from the same constants the admission pass moves it by.
            items.append(
                _upstream_hold_item(
                    module, row, pointer=pointer, now=state.now, phase=phase
                )
            )
            continue
        if classification in module.REVIEW_CLASSIFICATION_KINDS:
            if classification == "scoring" and run_id in in_flight:
                continue
            duty = module._review_duty_item(
                state.project,
                row,
                state.pointers.get(run_id),
                now=state.now,
                grace=state.grace,
            )
            # A run carrying the head its own resume superseded owes nothing
            # for that head, and the derivation returns no duty rather than a
            # stale one; the slice has to drop it too, since the payload is
            # what the reader is handed.
            if duty is not None:
                items.append(duty)
            continue
        if classification == "queued":
            # A queued request is listed as waiting by the derivation, so the
            # slice has to list it too: the row is built through the
            # derivation's own helper, from the same ordering, which is what
            # keeps this payload equal to the derivation field for field.
            if row.get("session_refusal"):
                items.append(
                    module._live_item(
                        row,
                        kind=module.QUEUED_AWAITING_SESSION_DUTY_KIND,
                        now=state.now,
                    )
                )
                continue
            if queued_placements is None:
                queued_placements = module.queue_admission.queued_placements(
                    list(state.pointers.values()), now=state.now
                )
            items.append(
                module._queued_item(row, placements=queued_placements, now=state.now)
            )
            continue
        kind = _duty_kind(module, row)
        if kind:
            items.append(module._live_item(row, kind=kind, now=state.now))
    items.extend(state.sub_floor.get(session, []))
    items.extend(state.held_worktrees.get(session, []))
    items.extend(_unreadable_record_items(state.findings))
    items, acknowledged = module._partition_acknowledged(items, state.acknowledged)
    items.sort(
        key=lambda item: (
            -int(item["age_seconds"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    acknowledged.sort(
        key=lambda item: (str(item["until"]), str(item["run_id"]), str(item["kind"]))
    )
    # The derivation owns the duty row's shape, so the slice runs its rows
    # through the same two owners the derivation does rather than restating
    # either: the action form each printed command renders as, and the
    # projection that supplies the section a run was dispatched for and leaves
    # an empty field off the row. A slice that skipped them published duties
    # without a section or an action form while the derivation carried both.
    items = [module._with_action_form(item) for item in items]
    acknowledged = [module._with_action_form(item) for item in acknowledged]
    sections = module.run_section_index([state.pointers.values()])
    summary = {
        "count": len(items),
        "oldest_age_seconds": max(
            (int(item["age_seconds"]) for item in items), default=0
        ),
        "unreconciled_runs": int(
            state.unreconciled.get(session, state.unreconciled.get("", 0))
        ),
    }
    return {
        "project": state.project,
        "session": session,
        "obligations": module.project_run_rows(
            items,
            view="obligations",
            vocabulary=module.DUTY_ROW_FIELDS,
            sections=sections,
        ),
        "acknowledged": module.project_run_rows(
            acknowledged,
            view="obligations",
            vocabulary=module.DUTY_ROW_FIELDS,
            sections=sections,
        ),
        "summary": summary,
    }


def _report_sweep_failure(project: str, *, session: str) -> None:
    """Log one failed sweep or slice, and let the sweep carry on.

    A review record, a pointer or a stream can be met mid-write, and one
    unreadable input must not cost every other session its snapshot: the
    producer's next trigger derives the state again and publishes. The
    traceback goes to the producer's log, so the defect stays visible to a
    reader rather than surfacing only as snapshots that stopped moving.
    """
    detail = f"session {session}" if session else "the fleet state"
    print(
        f"obligation-sweep-failure: {project}: {detail} could not be published",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exc(file=sys.stderr)


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

    One failure costs only what it touched. A sweep that cannot derive the
    fleet state, or a slice that cannot be built or written, is logged with its
    traceback and the remaining sessions are published; the producer's next
    trigger runs a fresh sweep, so the seat outlives any single exception a
    mid-write file raises.
    """
    instant = _utc_now() if now is None else now
    key = str(snapshot_dir(project))
    memory = _SWEEPS.get(key)
    checked = (
        memory is not None
        and (instant - memory.checked_at).total_seconds()
        < STAT_IDENTITY_INTERVAL_SECONDS
    )
    identity = memory.identity if checked else _stat_identity(state_dirs)
    due = (
        transition_fired
        or memory is None
        or memory.identity != identity
        or (instant - memory.published_at).total_seconds() >= FLOOR_TICK_SECONDS
    )
    if not due:
        _SWEEPS[key] = _SweepMemory(
            identity=identity,
            checked_at=instant if not checked else memory.checked_at,
            published_at=memory.published_at,
        )
        return []

    written: list[Path] = []
    ledger_module = _ledger_module()
    head_module = _review_head_module()
    # One region spans the derivation and every session's slice, so the index is
    # built once and each live run's reviewed head is resolved once for the
    # whole sweep rather than once per session that names it.
    with ledger_module.index_reuse(), head_module.review_heads_resolve_once():
        try:
            state = fleet_state(project, now=instant)
        except Exception:  # noqa: BLE001 - one unreadable input costs one sweep
            _report_sweep_failure(project, session="")
        else:
            for session in dict.fromkeys(str(session or "") for session in sessions):
                if not session:
                    continue
                try:
                    payload = payload_for(state, session)
                    document = document_for(
                        payload,
                        computed_at=instant,
                        stream_offset=stream_offset,
                        producer=producer,
                        findings=state.findings,
                    )
                    written.append(write_snapshot(project, session, document))
                except Exception:  # noqa: BLE001 - the sweep answers for every other session
                    _report_sweep_failure(project, session=session)
                    continue
    _SWEEPS[key] = _SweepMemory(
        identity=identity,
        checked_at=instant if not checked else memory.checked_at,
        published_at=instant,
    )
    return written
