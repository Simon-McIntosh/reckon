"""A follower reads its session's rows from the node's station, after a cursor.

The station is the one process on a node that reads the fleet and writes
each transition once. A follower is a subscriber to those rows: it holds a place
in the station's event sequence, asks for the rows addressed to its session and
to every pane after that place, paints them with the ticker it always used, and
writes its place back in the same transaction that delivered them.

The place is a cursor, and it is a pair — the station-wide event sequence number
and the UTC day that number belongs to — because events live in one file per
day. A subscriber attaches the day files from its cursor's day onward, so a
cursor that spans a rollover reads both the file it stopped in and the new one
exactly once.

The row in the store's ``cursors`` table is the whole of what a re-arm needs:
the place, the pane's checkpoint (the states it was shown and the lift groups it
drew), its rendered history, and its delivery registration. Nothing on the
pane's poll path opens a file under the crew home.

The station writes a generation counter in the main database after every event
commit, because ``PRAGMA data_version`` moves only for commits to the main
database and the event rows live in the attached day files. A subscriber's poll
reads the pragma and the counter: it queries only when the counter moved, so an
idle poll is two cheap reads and no event query.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import station_store, ticker
from reckon.crew.follow_checkpoint import cap_history

# How old the station's heartbeat may be before a subscriber says so. The
# station beats once a tick, so this is many missed ticks rather than one slow
# pass; past it the pane is reading a store nothing is writing.
STALE_HEARTBEAT_SECONDS = 90.0

# How many rows of the pane's memory the store keeps, and for how long. The
# follower's own log bounded both; these are the same bounds in the store.
HISTORY_ROWS = 100
HISTORY_SECONDS = 4 * 60 * 60

# Two rows a subscriber emits that carry a sentence to print rather than a run
# transition to draw: a place older than retention, resumed at the oldest event
# still held, and a station whose heartbeat has gone quiet.
STATION_GAP_EVENT = "station-gap"
STATION_STALE_EVENT = "station-stale"

SKIPPED_DAYS_TEXT = (
    "── {days} day(s) of events are older than the station keeps; "
    "resuming at its oldest retained event ──"
)

STALE_HEARTBEAT_TEXT = (
    "the station's heartbeat is {age} old, so no fleet row is being written; "
    "restart it with: reckon crew station"
)

_RESTART_COMMAND = "reckon crew station"

# The station value one project's has-live-work readings live under, suffixed
# with the project name. The station writes it on every tick and a pane reads
# it instead of listing the live pointer directory, so a settle costs the pane
# no file under the crew home.
LIVE_WORK_KEY = "live-work"


def record_live_work(
    store: station_store.StationStore,
    project: str,
    *,
    at: datetime,
    project_live: bool,
    unowned: bool,
    sessions: Iterable[str],
) -> None:
    """Record one project's reading, as the population each pane is judged by.

    A reading names the sessions holding work that has not ended, the
    project-wide answer an unscoped pane is judged by, and whether a live
    pointer carries no owner at all. An ownerless pointer keeps every session's
    reading live, because a pane cannot show that the work is not its own.
    """
    payload = {
        "at": at.astimezone(UTC).isoformat(),
        "project_live": bool(project_live),
        "unowned": bool(unowned),
        "sessions": sorted({str(name) for name in sessions if name}),
    }
    store.set_station_value(f"{LIVE_WORK_KEY}:{project}", json.dumps(payload))


def live_work_reading(
    store: station_store.StationStore,
    project: str,
    *,
    session: str | None,
    now: datetime | None = None,
    max_age_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> bool:
    """Whether the station's latest reading shows work for this population.

    ``session=None`` asks the project-wide reading an unscoped pane is judged
    by; a named session asks its own. A reading older than ``max_age_seconds``
    answers nothing rather than a stale verdict: a station that has stopped is
    already named to the pane by :func:`stale_heartbeat_line`, and a settle
    decided on a reading nothing is refreshing would end a pane over a fleet
    nobody is looking at. No reading at all reads as no work, which is what the
    pane's own saw-live rule is for: a pane that has never been shown live work
    does not settle on an empty reading, whenever that reading was taken.
    """
    raw = store.station_value(f"{LIVE_WORK_KEY}:{project}")
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    age = _age_seconds(str(payload.get("at") or ""), now=now or datetime.now(tz=UTC))
    if age is None or age > max_age_seconds:
        return False
    if session is None:
        return bool(payload.get("project_live"))
    if bool(payload.get("unowned")):
        return True
    return str(session) in {str(name) for name in payload.get("sessions") or () if name}


def _utc_day(stamp: Any) -> date | None:
    """The UTC day an event's stamp names, or None when it names none."""
    if isinstance(stamp, datetime):
        moment = stamp
    elif isinstance(stamp, str):
        moment = parse_utc(stamp)
    else:
        moment = None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def _age_seconds(stamp: str | None, *, now: datetime) -> float | None:
    """How long ago ``stamp`` was, in seconds, or None when it cannot be read."""
    moment = parse_utc(stamp) if stamp else None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment.astimezone(UTC)).total_seconds()


def _human_age(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def stale_heartbeat_line(
    store: station_store.StationStore,
    *,
    now: datetime | None = None,
    stale_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> str | None:
    """One line naming a heartbeat too old to trust, or None while it is fresh.

    ``None`` is the answer for a store that has never beaten: a station that has
    written no heartbeat has written no rows either, and a pane owed nothing is
    not owed a warning.
    """
    stamp = store.station_value(station_store.HEARTBEAT_KEY)
    if not stamp:
        return None
    age = _age_seconds(stamp, now=now or datetime.now(tz=UTC))
    if age is None or age < stale_seconds:
        return None
    return STALE_HEARTBEAT_TEXT.format(age=_human_age(age))


class StationSubscriber:
    """One pane's place in the station's event sequence."""

    def __init__(
        self,
        store: station_store.StationStore,
        project: str,
        *,
        session: str | None = None,
        observed: Iterable[str] = (),
        run_ids: Iterable[str] = (),
        clock=time.time,
        stale_seconds: float = STALE_HEARTBEAT_SECONDS,
    ) -> None:
        self.store = store
        self.project = project
        self.session = session
        self.observed = tuple(name for name in observed if name)
        self.run_ids = tuple(run_ids)
        self.clock = clock
        self.stale_seconds = stale_seconds
        self.audience = session or "*"
        self._row = store.read_cursor(self.project, self.audience)
        checkpoint = self._row.get("checkpoint") or {}
        reported = checkpoint.get("reported") if isinstance(checkpoint, Mapping) else {}
        lift_groups = (
            checkpoint.get("lift_groups") if isinstance(checkpoint, Mapping) else ()
        )
        self.path = ticker.PaneRowPath(
            selects=self._selects,
            own_session=session,
            observed_sessions=self.observed,
            reported=reported if isinstance(reported, Mapping) else {},
            lift_groups=lift_groups if isinstance(lift_groups, Iterable) else (),
        )
        self.history: list[dict[str, Any]] = [
            row for row in (self._row.get("history") or []) if isinstance(row, Mapping)
        ]
        # The last generation seen, so a poll answers "nothing moved" without
        # querying the day files. Seeded from the store so the first poll of a
        # fresh subscriber still reads what was written before it attached.
        self._generation = store.event_generation()
        self._data_version = store.data_version()
        self._first_poll = True
        # Whether this pane may write the session's row. The row holds one
        # session's place, and a second pane reading the same rows would move
        # the first pane's cursor and checkpoint out from under it, so only the
        # pane holding the session's registration writes. It still draws what
        # it reads; it keeps no place of its own until the claim passes to it.
        self.persist = True

    # ── the place ────────────────────────────────────────────────────────

    @property
    def cursor(self) -> int:
        return int(self._row.get("cursor") or 0)

    @property
    def cursor_day(self) -> str | None:
        return self._row.get("day")

    @property
    def registration(self) -> dict[str, Any] | None:
        return self._row.get("registration")

    def _selects(self, event: Mapping[str, Any]) -> bool:
        run_id = str(event.get("run_id") or "")
        return not self.run_ids or run_id in self.run_ids

    def _audiences(self) -> tuple[str, ...]:
        if self.session is None:
            # An unscoped reader carries the whole fleet, so it asks for every
            # audience rather than inventing one it is not registered for.
            return ()
        return (self.session, *self.observed, "*")

    def register(
        self,
        *,
        pid: int | None = None,
        host: str | None = None,
        delivery: str = "unknown",
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Write this subscriber's delivery registration into its own row."""
        import os

        record = {
            "pid": int(os.getpid() if pid is None else pid),
            "host": host,
            "delivery": delivery,
            "scope": dict(scope or {}),
            "attached_at": datetime.now(tz=UTC).isoformat(),
        }
        self.store.write_cursor(self.project, self.audience, registration=record)
        self._row = self.store.read_cursor(self.project, self.audience)

    def detach(self) -> None:
        """Release the registration, keeping the place the pane reached."""
        self.store.write_cursor(self.project, self.audience, registration=None)
        self._row = self.store.read_cursor(self.project, self.audience)

    def save_checkpoint(self, *, at: float | None = None) -> None:
        """Persist the pane's memory — the states and lift groups it was shown."""
        if not self.persist:
            return
        checkpoint = {
            "reported": {str(k): str(v) for k, v in self.path.reported.items()},
            "lift_groups": sorted(self.path.lift_groups),
            "written_at": float(self.clock() if at is None else at),
        }
        self.store.write_cursor(self.project, self.audience, checkpoint=checkpoint)
        self._row = self.store.read_cursor(self.project, self.audience)

    def append_history(
        self,
        *,
        text: str,
        at: float,
        kind: str = "row",
        run_id: str = "",
        state: str = "",
        group: str = "",
        max_rows: int = HISTORY_ROWS,
        max_seconds: float = HISTORY_SECONDS,
    ) -> None:
        """Record one rendered row in the store, bounded as the file log was."""
        if not self.persist:
            return
        row = {
            "kind": str(kind),
            "at": float(at),
            "text": str(text),
            "run_id": str(run_id),
            "state": str(state),
        }
        if group:
            row["group"] = str(group)
        if kind == "format-changed" and self.history:
            last = self.history[-1]
            if str(last.get("kind") or "") == "format-changed":
                return
        capped = cap_history(
            [*self.history, row],
            now=float(at),
            max_rows=max_rows,
            max_seconds=max_seconds,
        )
        self.history = capped
        self.store.write_cursor(self.project, self.audience, history=capped)
        self._row = self.store.read_cursor(self.project, self.audience)

    def note_delivered(self, event: Mapping[str, Any]) -> None:
        """Record the state a delivered row left its run in, for a later re-arm."""
        if not self.checkpoint_exists():
            self.save_checkpoint()
            return
        run_id = str(event.get("run_id") or "")
        state = str(event.get("to_state") or "")
        if not run_id or not state:
            return
        self.path.remember(run_id, state)
        self.save_checkpoint()

    def checkpoint_exists(self) -> bool:
        return bool(self._row.get("checkpoint"))

    def seed_from_history(self) -> None:
        """Restore the pane's memory from its stored history when it has none."""
        for row in self.history:
            run_id = str(row.get("run_id") or "")
            state = str(row.get("state") or "")
            if run_id and state:
                self.path.remember(run_id, state)
        for group, state in _lift_states(self.history):
            if state in (ticker.LIFT_GRANTED, ticker.LIFT_IN_FORCE):
                self.path.remember_lift(group)

    # ── the read ─────────────────────────────────────────────────────────

    def generation_moved(self) -> bool:
        """Whether the station committed an event since this subscriber's last read.

        ``PRAGMA data_version`` moves for any commit to the main database, and
        the station's generation counter moves only for a commit that added
        event rows. Both are read, so a subscriber's own cursor write does not
        read as an event, and a day file write is still seen because the
        station's counter commit lands in the main database after it.
        """
        data_version = self.store.data_version()
        if data_version == self._data_version:
            return False
        self._data_version = data_version
        generation = self.store.event_generation()
        if generation == self._generation:
            return False
        self._generation = generation
        return True

    def poll(self) -> list[Mapping[str, Any]]:
        """Every row this pane receives now, in the station's own order.

        A cursor older than the store's oldest day file is resumed at the first
        event still held, preceded by one line saying how many days were
        skipped. The query is skipped entirely when the generation has not
        moved, and the first poll of a fresh subscriber always queries: its
        place may be behind rows written before it attached.
        """
        if not self._first_poll and not self.generation_moved():
            return []
        self._first_poll = False

        oldest = self.store.oldest_day()
        cursor_day = _utc_day(self.cursor_day)
        skipped_days = 0
        after_id = self.cursor
        from_day = cursor_day
        if oldest is not None and (cursor_day is None or cursor_day < oldest):
            if cursor_day is not None:
                skipped_days = (oldest - cursor_day).days
            after_id = 0
            from_day = oldest

        audiences = self._audiences()
        rows = self.store.events_for(
            project=self.project,
            audiences=audiences or (),
            after_id=after_id,
            from_day=from_day,
        )
        # An unscoped reader asks for no audience filter; ``events_for`` with no
        # audiences reads project-wide, which is what its empty tuple means.
        delivered: list[Mapping[str, Any]] = []
        if skipped_days:
            delivered.append(
                {
                    "event": STATION_GAP_EVENT,
                    "project": self.project,
                    "session": self.session or "",
                    "run_id": None,
                    "skipped_days": skipped_days,
                    "line": SKIPPED_DAYS_TEXT.format(days=skipped_days),
                }
            )
        moment = self.clock()
        last_id = after_id
        last_day = from_day.isoformat() if from_day is not None else None
        for row in rows:
            event = _event_from_row(row)
            last_id = int(row["id"])
            day = _utc_day(row.get("time"))
            if day is not None:
                last_day = day.isoformat()
            delivered.extend(self.path.feed(event, now=moment))
        if self.persist and (last_id != self.cursor or last_day != self.cursor_day):
            self.store.write_cursor(
                self.project,
                self.audience,
                cursor=last_id,
                day=last_day,
                at=_moment(at=moment),
            )
            self._row = self.store.read_cursor(self.project, self.audience)
        if delivered:
            self.save_checkpoint(at=moment)
        return delivered

    def flush(self) -> list[Mapping[str, Any]]:
        """Rows whose hold window has closed while the station was quiet."""
        released = self.path.flush(now=self.clock())
        if released:
            self.save_checkpoint()
        return released

    def stale_line(self, *, now: datetime | None = None) -> str | None:
        return stale_heartbeat_line(
            self.store, now=now, stale_seconds=self.stale_seconds
        )

    def live_work(self, session: str | None = None) -> bool:
        """Whether the station's latest reading shows work for that population.

        The pane's own settle rule judges each population it carries
        separately; this answers one of them from the reading the station
        wrote on its tick, so the judgement costs no directory listing.
        """
        return live_work_reading(self.store, self.project, session=session)

    def wake(
        self,
        *,
        stop=None,
        interval: float = 0.1,
        sleeper=time.sleep,
        stale_interval: float = 5.0,
    ) -> Iterator[Mapping[str, Any]]:
        """Yield this pane's rows until ``stop`` is set.

        The station's heartbeat is checked on its own cadence rather than on
        every pass, and its line is printed once — a subscriber that repeated it
        every poll would bury the fleet under the same sentence.
        """
        stale_reported = False
        while stop is None or not stop.is_set():
            for event in self.poll():
                yield event
            for event in self.flush():
                yield event
            line = None if stale_reported else self.stale_line()
            if line is not None:
                stale_reported = True
                yield {
                    "event": STATION_STALE_EVENT,
                    "project": self.project,
                    "session": self.session or "",
                    "run_id": None,
                    "line": line,
                }
            sleeper(interval)
            if stale_reported and stale_interval and self.stale_line() is None:
                stale_reported = False


def _moment(*, at: float) -> datetime:
    return datetime.fromtimestamp(float(at), tz=UTC)


def _event_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The producer's own event object, rebuilt from its stored row.

    The row's columns carry the fields a query filters on; the payload carries
    everything the painter draws. A row written before the payload column
    existed still reaches the pane from its columns alone.
    """
    payload = row.get("payload")
    from reckon.crew import runs

    event: dict[str, Any] | None = None
    if payload:
        event = runs.parse_stream_line(str(payload))
    if not isinstance(event, Mapping):
        event = {}
    rebuilt = dict(event)
    rebuilt.setdefault("event", row.get("kind") or "transition")
    rebuilt.setdefault("project", row.get("project"))
    rebuilt.setdefault("audience", row.get("audience"))
    rebuilt.setdefault("run_id", row.get("run"))
    rebuilt.setdefault("from_state", row.get("from_state"))
    rebuilt.setdefault("to_state", row.get("to_state"))
    rebuilt.setdefault("detail", row.get("detail"))
    rebuilt.setdefault("duty", row.get("duty"))
    rebuilt.setdefault("observed_at", row.get("time"))
    rebuilt["station_event_id"] = int(row["id"])
    return rebuilt


def _lift_states(rows: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    return [
        (str(row.get("group") or ""), str(row.get("state") or ""))
        for row in rows
        if row.get("group")
    ]
