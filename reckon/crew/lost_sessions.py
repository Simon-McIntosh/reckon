"""Whether a coordinator session is lost, and since when.

A session id outlives any one process: a resumed session keeps it for weeks. So
"is this session still there?" cannot be asked of the process carrying the id
today -- an age read from that process answers about the process, not the
session. The question is instead about the last instant the session was *heard*:
the last follower lease it renewed, a live session-host record naming it, or a
coordinator process in flight carrying its id.

A session is lost once nothing has heard it for longer than ``after_hours``.
Three days is a decision about work rather than about a heartbeat: a wait can be
correct for days, and only an owner gone for days has work nobody can release.

The readings are the ones the forgotten-run derivation already uses, so a
session reads lost here exactly where the watcher's sweep would call it
unattended, only on a longer clock. Nothing here writes. A process's start time
is never read: it measures the process, not the session.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import flight

from . import session_host as _session_host
from .follower_registration import _follower_liveness, follower_lock_path
from .runs import _unattended_seconds, parse_utc

#: How long a session may go unheard before it is read as lost.
DEFAULT_AFTER_HOURS = 72.0

#: The flight block and slot that carry the threshold.
AFTER_HOURS_KEY = "lost_session"
AFTER_HOURS_SLOT = "after_hours"


def lost_session_after_hours(config: Mapping[str, Any] | None = None) -> float:
    """The configured threshold in hours, or the shipped default.

    Zero is honoured: the schema admits it, and an operator who sets it means
    that any session nothing has heard right now is lost. Only a figure the
    schema cannot hold -- a boolean, a non-number, or a negative -- falls back
    to the shipped default, because such a value is a broken layer rather than a
    threshold, and a broken layer must not silently change what the derivation
    does.
    """

    resolved = config if config is not None else flight.resolve().config
    block = resolved.get(AFTER_HOURS_KEY)
    value = block.get(AFTER_HOURS_SLOT) if isinstance(block, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return DEFAULT_AFTER_HOURS
    return float(value)


def live_process_sessions(
    sessions: Iterable[str],
    *,
    process_root: Path = Path("/proc"),
    uid: int | None = None,
) -> set[str]:
    """Candidate session ids a live process of this user carries as one argument.

    A coordinator launches its harness with the session id as a whole argument,
    and a resumed worker is launched the same way, so the process table is where
    a session with no follower registration still answers. The match is on whole
    arguments: an id that merely appears inside another argument belongs to no
    launch of this session. The caller and its ancestors are excluded, because
    the process asking the question is never the session it asks about and a
    wrapper may carry the id in its own invocation.

    The walk reaches only processes of this user on this host, the same reach
    the run liveness probe beside it has. One table read answers every candidate
    in ``sessions``, so a pass asking about many sessions pays for one walk.
    """

    candidates = {str(session) for session in sessions if session}
    if not candidates:
        return set()
    from reckon.crew.dispatch_accounting import _ancestor_processes

    ancestry, _names = _ancestor_processes()
    excluded = ancestry | {os.getpid()}
    owner = os.getuid() if uid is None else uid
    found: set[str] = set()
    try:
        entries = list(process_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in excluded:
            continue
        try:
            if entry.stat().st_uid != owner:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        for token in raw.split(b"\0"):
            if not token:
                continue
            argument = os.fsdecode(token)
            if argument in candidates:
                found.add(argument)
    return found


def process_started_at(pid: int, *, process_root: Path = Path("/proc")) -> float | None:
    """The instant a process started, or ``None`` when it cannot be read.

    This is the evidence a derivation keyed on process age would read, and it is
    why that derivation is wrong for a session: a session id outlives the
    process carrying it, so a start time dates the process and says nothing
    about when the session was last heard. Nothing in the session derivation
    calls this; it is here so the negative control can read the same evidence a
    process-age-keyed derivation would, rather than asserting about arithmetic
    of its own.

    ``/proc/<pid>/stat`` carries the start time in field 22 as clock ticks since
    boot, so the boot instant from ``/proc/stat`` converts it to wall clock. The
    command name sits in parentheses and may contain spaces, so the fields are
    split after the closing parenthesis.
    """

    try:
        fields = (process_root / str(pid) / "stat").read_text().split()
        boot = None
        for line in (process_root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
                break
        ticks = float(fields[21])
    except (OSError, IndexError, ValueError):
        return None
    if boot is None:
        return None
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def live_census_pairs() -> set[tuple[str, str]]:
    """Project-and-session pairs a live session host names.

    A census record survives only while the process that owns it lives, so a
    record naming a pair is a session host running now and a coordinator
    process behind it. Its children list every pair, a settled child included,
    so the test is the pair's presence rather than a running follower.
    """

    pairs: set[tuple[str, str]] = set()
    for host in _session_host.live_census():
        for child in host.get("children") or ():
            if not isinstance(child, Mapping):
                continue
            project = str(child.get("project") or "")
            session = str(child.get("session") or "")
            if project and session:
                pairs.add((project, session))
    return pairs


def _oldest_record_instant(pointers: Iterable[Mapping[str, Any]]) -> float | None:
    """The oldest live record's own creation instant, or ``None`` if undated.

    A session that never registered a follower has no lease to age, so the store
    dates it from here. The oldest record is the earliest moment the store can
    show the session existed; dating it from the newest would keep a session
    that dispatched once alive for as long as any of its work remains. A record
    carrying no readable creation stamp is skipped rather than read as "now".
    """

    instants: list[float] = []
    for pointer in pointers:
        created = parse_utc(str(pointer.get("created_at") or ""))
        if created is not None:
            instants.append(created.timestamp())
    return min(instants) if instants else None


def _iso(instant: float | None) -> str | None:
    """One instant as the view carries it, or ``None`` when it is undated."""

    if instant is None:
        return None
    return datetime.fromtimestamp(instant, tz=UTC).isoformat()


def _answer(
    session: str,
    *,
    last_heard: float | None,
    now_seconds: float,
    after_hours: float,
) -> dict[str, Any]:
    """One session's answer, from the instant it was last heard."""

    threshold = after_hours * 60.0 * 60.0
    if last_heard is None:
        # Nothing dates the session at all. Absence of evidence is not evidence
        # that it is lost: there is no record of its own here to retire, and a
        # caller that met the session through its records always has one.
        return {
            "session": session or None,
            "last_heard_at": None,
            "lost": False,
            "lost_since": None,
        }
    age = max(0.0, now_seconds - last_heard)
    lost = age > threshold
    return {
        "session": session or None,
        "last_heard_at": _iso(last_heard),
        "lost": lost,
        "lost_since": _iso(last_heard + threshold) if lost else None,
    }


class LostSessionReads:
    """One pass's answers about every session it lists.

    A pass over forty pointers owned by six sessions must not cost forty
    derivations: each one reads a registration, a lease and -- when nothing has
    heard the session -- the process table. This holds the pointers the pass
    already listed, indexes them by pair, and answers each pair once. The
    process walk and the census are read once for the whole set, lazily, so a
    pass whose sessions are all heard by lease pays for no walk at all.

    The answers are kept as the dicts :func:`session_lost` returns, so a caller
    that wants to record them can hand the same object on.
    """

    def __init__(
        self,
        *,
        pointers: Iterable[Mapping[str, Any]] = (),
        now: datetime | None = None,
        after_hours: float | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._now = datetime.now(tz=UTC) if now is None else now
        self._after_hours = float(after_hours) if after_hours is not None else None
        self._config = config
        self._by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for pointer in pointers:
            if not isinstance(pointer, Mapping):
                continue
            session = str(pointer.get("session") or "")
            if not session:
                continue
            project = str(pointer.get("project") or "")
            self._by_pair.setdefault((project, session), []).append(pointer)
        self._answers: dict[tuple[str, str], dict[str, Any]] = {}
        self._live_sessions: set[str] | None = None
        self._census_pairs: set[tuple[str, str]] | None = None

    def _reading(self, key: tuple[str, str]) -> dict[str, Any]:
        """The pair's answer, derived once. Its derivation is one named read."""

        if self._live_sessions is None:
            self._live_sessions = live_process_sessions(
                {session for _project, session in self._by_pair} | {key[1]}
            )
        if self._census_pairs is None:
            self._census_pairs = live_census_pairs()
        return session_lost(
            key[0],
            key[1],
            pointers=self._by_pair.get(key, ()),
            now=self._now,
            after_hours=self._after_hours,
            live_sessions=self._live_sessions,
            census_pairs=self._census_pairs,
            config=self._config,
        )

    def answer(self, session: str, project: str = "") -> dict[str, Any]:
        """The answer for one pair, derived once per pass.

        ``project`` decides which project's registration and lease are read; a
        row naming no project takes the pair its session owns, if it owns one,
        because a session's id is what the record names and the project only
        decides where its registration lives.
        """

        key = (project, session)
        if key in self._answers:
            return self._answers[key]
        if key not in self._by_pair and not project:
            owned = sorted(pair for pair in self._by_pair if pair[1] == session)
            if owned:
                key = owned[0]
        if key in self._answers:
            return self._answers[key]
        answer = self._reading(key)
        self._answers[key] = answer
        return answer

    def answers(self, pairs: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        """One answer per distinct pair, in the order first asked."""

        seen: list[tuple[str, str]] = []
        for project, session in pairs:
            key = (project, session)
            if key in self._answers or key in seen:
                continue
            seen.append(key)
        return [self.answer(session, project) for project, session in seen]

    def count(self) -> int:
        """How many derivations the pass has made."""

        return len(self._answers)


def session_lost(
    project: str,
    session: str,
    *,
    pointers: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
    after_hours: float | None = None,
    config: Mapping[str, Any] | None = None,
    live_sessions: set[str] | None = None,
    census_pairs: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Whether one session is lost, with the instant it was last heard.

    ``pointers`` is the session's own live records, which a caller usually
    already holds: this adds no read of the live directory. ``live_sessions``
    and ``census_pairs`` are the process and session-host readings supplied by
    a caller asking about several sessions so one walk and one census answer
    them all; left ``None``, each is read here for this session alone.

    The answer is the latest of: a follower attached now, a live session host
    naming the pair, a live process carrying the session id, the session's own
    last lease or release stamp, and -- for a session that never registered a
    follower -- its oldest live record.
    """

    records = [pointer for pointer in pointers if isinstance(pointer, Mapping)]
    moment = datetime.now(tz=UTC) if now is None else now
    now_seconds = moment.timestamp()
    threshold = (
        float(after_hours)
        if after_hours is not None
        else lost_session_after_hours(config)
    )

    if session:
        # A follower attached right now is heard, whatever its lease stamp says:
        # the registration's own reader decides it, so a held lock and a live
        # process count exactly where they count for the watcher's sweep.
        if _follower_liveness(follower_lock_path(project, session)).get("live"):
            return _answer(
                session,
                last_heard=now_seconds,
                now_seconds=now_seconds,
                after_hours=threshold,
            )

        pairs = live_census_pairs() if census_pairs is None else census_pairs
        if (project, session) in pairs:
            # A live session host names the pair, so the coordinator process
            # behind it is here now.
            return _answer(
                session,
                last_heard=now_seconds,
                now_seconds=now_seconds,
                after_hours=threshold,
            )
        carries = (
            session in live_sessions
            if live_sessions is not None
            else bool(live_process_sessions([session]))
        )
        if carries:
            # A process carries the session id now. Its start time is not read:
            # a resumed session keeps its id across processes and a process
            # started ten days ago may hold a session heard a minute ago.
            return _answer(
                session,
                last_heard=now_seconds,
                now_seconds=now_seconds,
                after_hours=threshold,
            )

    instants: list[float] = []
    unattended = _unattended_seconds(project, session, now_seconds=now_seconds)
    if unattended is not None:
        instants.append(now_seconds - unattended)
    else:
        oldest = _oldest_record_instant(records)
        if oldest is not None:
            instants.append(oldest)

    return _answer(
        session,
        last_heard=max(instants) if instants else None,
        now_seconds=now_seconds,
        after_hours=threshold,
    )
