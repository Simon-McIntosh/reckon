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
import logging
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import node, station_store, ticker
from reckon.crew.follow_checkpoint import cap_history

_LOGGER = logging.getLogger(__name__)

# How old a reading may be before it is treated as no answer. The station beats
# once a tick, so this is many missed ticks rather than one slow pass; past it a
# pane is reading a store nothing is writing, and it must reach its own
# conclusion rather than read silence as "no work".
STALE_HEARTBEAT_SECONDS = 90.0

# How many rows of the pane's memory the store keeps, and for how long. The
# follower's own log bounded both; these are the same bounds in the store.
HISTORY_ROWS = 100
HISTORY_SECONDS = 4 * 60 * 60

# A row a subscriber emits that carries a sentence to print rather than a run
# transition to draw: a place older than retention, resumed at the oldest event
# still held.
STATION_GAP_EVENT = "station-gap"

SKIPPED_DAYS_TEXT = (
    "── {days} day(s) of events are older than the station keeps; "
    "resuming at its oldest retained event ──"
)

# The station value one project's has-live-work readings live under, suffixed
# with the project name. The station writes it on every tick and a pane reads
# it instead of listing the live pointer directory, so a settle costs the pane
# no file under the crew home.
LIVE_WORK_KEY = "live-work"

# The runs whose review duty could not be read in this process. The station
# asks every live pointer on every tick, so an unguarded warning over an
# unreadable record prints once a second for as long as the record stays that
# way, and the repetition buries the first report rather than adding to it. One
# warning per run is what makes the defect visible; the rest are noise.
_WARNED_DUTY_READ_FAILURES: set[str] = set()

# The one duty kind whose evidence is a review keyed on the run's own head. A
# duty that cannot resolve that head names a review no dispatch could satisfy:
# it is still reported to the coordinator, but it does not hold a pane open
# against work nothing can complete. No other kind is exempt on that ground --
# a promotion is actionable whatever the tree reads, and the duty for a run
# whose review tier is ``none`` carries no head at all.
_HEAD_KEYED_DUTY_KIND = "review-missing"


def _warn_duty_read_failure(run_id: str, error: BaseException) -> None:
    """Report an unreadable duty once per run per process, then stay quiet."""
    key = run_id or "<unnamed>"
    if key in _WARNED_DUTY_READ_FAILURES:
        return
    _WARNED_DUTY_READ_FAILURES.add(key)
    _LOGGER.warning(
        "settling over run %s: its review duty could not be read (%s: %s)",
        key,
        type(error).__name__,
        error,
    )


def terminal_run_duty_items(
    pointer: Mapping[str, Any],
    *,
    now: Any,
    live_pointers: Sequence[Mapping[str, Any]] | None = None,
    placements: Mapping[str, Mapping[str, int]] | None = None,
    grace: int,
    reviews_in_flight: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The duties a terminal run raises on its own account, as the pane reads them.

    The obligations view raises its duties over every classified row, and the
    ones that are about a single run carry that run's own ``run_id``. Reading
    those is what tells the has-live-work rule whether the coordinator still
    has something to do about a run that has ended, which is a different
    question from whether the run owes a review: a promotion owed by a
    delivered review, a promotable run, a refused landing, an owed repair and a
    run that ended ``needs-help``, ``unreadable`` or ``exited-unfinished`` all
    keep the session's pane owed, while a project-wide notice whose ``run_id``
    is empty belongs to no run and keeps nothing.

    Every kind table this reads is the obligations module's own, and the items
    are built by that module's own helpers, so the two readers cannot disagree
    about which run owes what.

    The two exemptions the view applies to a scoring row are applied here, for
    the reason the view applies them: a review the lane accepted and has not
    launched yet will still file its record, so asking for another would spend
    a member on work already queued, while a delivered review the lane refused
    is the exception -- nothing is coming, so the duty stands.

    ``reviews_in_flight`` is the caller's snapshot for one project and session,
    so a pass over many rows asks one question of one read rather than listing
    the live directory once per row.
    """
    from reckon.crew import obligations, recovery

    row = recovery.classify_pointer(pointer)
    classification = str(row.get("classification") or "")
    run_id = str(row.get("run_id") or "")
    if classification == obligations.PRODUCER_GONE_STATUS:
        return [obligations._producer_gone_item(row, now=now)]
    if classification in obligations.REVIEW_CLASSIFICATION_KINDS:
        if classification == "scoring":
            in_flight = reviews_in_flight
            if in_flight is None:
                in_flight = obligations._live_review_runs(
                    str(pointer.get("project") or ""),
                    str(pointer.get("session") or ""),
                )
            if (
                run_id in in_flight
                and row.get("review_run_phase") != "working"
                and obligations._refused_delivered_review(pointer) is None
            ):
                return []
        item = obligations._review_duty_item(
            str(pointer.get("project") or ""),
            row,
            pointer,
            now=now,
            grace=grace,
            live_pointers=live_pointers,
            placements=placements,
        )
        return [item] if item is not None else []
    recovery_reading = str(row.get("recovery_classification") or "")
    kind = obligations.RECOVERY_CLASSIFICATION_DUTY_KINDS.get(
        recovery_reading
    ) or obligations.CLASSIFICATION_DUTY_KINDS.get(classification, "")
    if not kind:
        return []
    return [obligations._live_item(row, kind=kind, now=now)]


def run_owes_its_coordinator(
    pointer: Mapping[str, Any],
    *,
    now: Any = None,
    live_pointers: Sequence[Mapping[str, Any]] | None = None,
    placements: Mapping[str, Mapping[str, int]] | None = None,
    grace: int | None = None,
    reviews_in_flight: set[str] | None = None,
) -> bool:
    """Whether this run raises a duty of its own, so its pane is still owed.

    A run whose duty cannot be read is not held for: an unreadable record is
    not a reason to keep a pane alive forever, and the warning naming it is
    printed once per run per process.

    A review owed against a head that will not resolve is not held for either.
    The review is keyed on the head, so no dispatch could ever satisfy such a
    duty and the pane would be held forever; the duty still names the run to
    the coordinator, which is where a run with no reviewable head belongs.
    """
    from reckon.crew import obligations

    run_id = str(pointer.get("run_id") or "")
    try:
        items = terminal_run_duty_items(
            pointer,
            now=now if now is not None else obligations._utc_now(),
            live_pointers=live_pointers,
            placements=placements,
            grace=(
                grace
                if grace is not None
                else _review_grace(str(pointer.get("project") or ""))
            ),
            reviews_in_flight=reviews_in_flight,
        )
    except Exception as error:  # noqa: BLE001 - a record we cannot read is not a hold
        _warn_duty_read_failure(run_id, error)
        return False
    for item in items:
        if str(item.get("run_id") or "") != run_id:
            continue
        # Only a missing review is keyed on the head: with no head resolvable
        # there is nothing a dispatch could attach to, so holding the pane
        # would hold it forever. A ready review is a promotion the coordinator
        # can take whatever the tree reads, and the tier-none duty carries no
        # head at all, so neither is exempt on this ground.
        if str(item.get("kind") or "") == _HEAD_KEYED_DUTY_KIND and not str(
            item.get("head") or ""
        ):
            continue
        return True
    return False


def _review_grace(project: str) -> int:
    """The unreconciled-run grace the obligations derive their duties with.

    Read through the obligations module rather than restated, so the pane
    settles by the same grace the duties it reads were built with.
    """
    from reckon.crew import obligations

    try:
        config = obligations.flight.resolve(project).config
        return obligations.parse_duration(
            str((config.get("fences") or {}).get("unreconciled_run_grace") or "15m")
        )
    except Exception:  # noqa: BLE001 - the obligations' own default stands
        return obligations.parse_duration("15m")


def live_work_populations(
    pointers: Iterable[Mapping[str, Any]],
) -> tuple[bool, bool, list[str]]:
    """One fleet's reading: whether anything is live, unowned, and for whom.

    This is the one statement of the has-live-work rule. The station writes its
    answer into the reading a pane settles by, and a pane with no station asks
    the same rule of the pointers directly, so the two cannot disagree about
    which run makes a population live.

    A run that has not reached a terminal phase is work still owed a pane. A
    terminal run is too, while the obligations view raises a duty on that run
    itself: the follower inside the pane is the only reader that acts on those
    duties, so ending on the delivery would strand a promotion, a refused
    landing or an owed repair with nobody watching. An ownerless pointer keeps
    every session's reading live, because without an owner nothing shows the
    work is not the reader's own.
    """
    from reckon.crew.dispatch_launch import LAUNCH_FAILED_PHASE

    project_live = False
    unowned = False
    sessions: set[str] = set()
    # A launch record says a worker was never spawned, so it is a queue
    # placement and not work a pane could show. Every reader of this rule --
    # a follower settling a pair, the station's reading, a host on start -- sees
    # the same population, so the exclusion is made here rather than by each
    # caller, and none can disagree with another about whether such a run owes
    # a pane.
    snapshot = [
        pointer
        for pointer in pointers
        if str(pointer.get("phase") or "") != LAUNCH_FAILED_PHASE
    ]
    # One read serves every pointer: the duties are asked of one snapshot of the
    # live directory and one ordering, so two runs in one pass cannot be judged
    # against two different views of the fleet.
    duty_inputs: dict[str, Any] = {}
    if any(
        str(pointer.get("phase") or "") in node._TERMINAL_RUN_PHASES
        for pointer in snapshot
    ):
        from reckon.crew import obligations, queue_admission

        duty_inputs["now"] = obligations._utc_now()
        duty_inputs["live_pointers"] = snapshot
        try:
            duty_inputs["placements"] = queue_admission.queued_placements(
                snapshot, now=duty_inputs["now"]
            )
        except Exception:  # noqa: BLE001 - the duty reads the ordering itself
            duty_inputs["placements"] = None
    graces: dict[str, int] = {}
    in_flight: dict[tuple[str, str], set[str]] = {}
    # The rules this reads for its exemptions resolve runs through the
    # obligations module's own pointer read. Publishing this pass's snapshot to
    # it keeps the whole judgement over one view of the fleet, and costs no
    # directory listing per row.
    from reckon.crew import obligations

    grouped: dict[str, list[dict[str, Any]]] = {}
    for pointer in snapshot:
        grouped.setdefault(str(pointer.get("project") or ""), []).append(dict(pointer))
    token = obligations._VIEW_LIVE_POINTERS.set(
        {project: tuple(rows) for project, rows in grouped.items()}
    )
    try:
        for pointer in snapshot:
            if str(pointer.get("phase") or "") not in node._TERMINAL_RUN_PHASES:
                project_live = True
                owner = str(pointer.get("session") or "")
                if owner:
                    sessions.add(owner)
                else:
                    unowned = True
                continue
            project = str(pointer.get("project") or "")
            if project not in graces:
                graces[project] = _review_grace(project)
            pair = (project, str(pointer.get("session") or ""))
            if pair not in in_flight:
                in_flight[pair] = obligations._live_review_runs(*pair)
            if not run_owes_its_coordinator(
                pointer,
                grace=graces[project],
                reviews_in_flight=in_flight[pair],
                **duty_inputs,
            ):
                continue
            project_live = True
            owner = str(pointer.get("session") or "")
            if owner:
                sessions.add(owner)
            else:
                unowned = True
    finally:
        obligations._VIEW_LIVE_POINTERS.reset(token)
    return project_live, unowned, sorted(sessions)


def population_has_live_work(
    pointers: Iterable[Mapping[str, Any]], session: str | None
) -> bool:
    """Whether one population — a session's runs, or the project's — is live."""
    project_live, unowned, sessions = live_work_populations(pointers)
    if session is None:
        return project_live
    return unowned or str(session) in sessions


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


def live_work_verdict(
    store: station_store.StationStore,
    project: str,
    *,
    session: str | None,
    now: datetime | None = None,
    max_age_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> bool | None:
    """Whether the station's latest reading shows work, or ``None`` for no answer.

    ``session=None`` asks the project-wide reading an unscoped pane is judged
    by; a named session asks its own. A reading older than ``max_age_seconds``,
    or no readable reading at all, is not a reading of no work: it is no
    answer, and a caller that would end a pane on "no work" has to reach its
    own conclusion instead: deciding a settle on a reading nothing is
    refreshing would end a pane over a fleet nobody is looking at. A station
    that has stopped is the obligations hook's to report, which is where a
    coordinator acts on it.
    """
    raw = store.station_value(f"{LIVE_WORK_KEY}:{project}")
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    age = _age_seconds(str(payload.get("at") or ""), now=now or datetime.now(tz=UTC))
    if age is None or age > max_age_seconds:
        return None
    if session is None:
        return bool(payload.get("project_live"))
    if bool(payload.get("unowned")):
        return True
    return str(session) in {str(name) for name in payload.get("sessions") or () if name}


def live_work_reading(
    store: station_store.StationStore,
    project: str,
    *,
    session: str | None,
    now: datetime | None = None,
    max_age_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> bool:
    """The station's answer for this population, with no answer read as no work."""
    verdict = live_work_verdict(
        store,
        project,
        session=session,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return bool(verdict)


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
    ) -> None:
        self.store = store
        self.project = project
        self.session = session
        self.observed = tuple(name for name in observed if name)
        self.run_ids = tuple(run_ids)
        self.clock = clock
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
        # Where this subscriber has read to. It starts at the stored place and
        # always advances in memory; the stored place is written only while the
        # pane holds the session's registration. A pane that does not hold it
        # still has to move past what it already drew, or every poll would read
        # from the stored place again and redraw the same rows.
        self._read_id: int | None = None
        self._read_day: str | None = None
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
        if self._read_id is None:
            self._read_id, self._read_day = self.cursor, self.cursor_day
        cursor_day = _utc_day(self._read_day)
        skipped_days = 0
        after_id = self._read_id
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
        self._read_id, self._read_day = last_id, last_day
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

    def live_work(self, session: str | None = None) -> bool | None:
        """The station's answer for that population, or ``None`` for no answer.

        The pane's own settle rule judges each population it carries
        separately; this answers one of them from the reading the station
        wrote on its tick, so the judgement costs no directory listing. A
        reading the station has stopped refreshing answers ``None`` rather
        than "no work", so a pane whose station has stalled settles nothing on
        its silence.
        """
        return live_work_verdict(self.store, self.project, session=session)

    def wake(
        self,
        *,
        stop=None,
        interval: float = 0.1,
        sleeper=time.sleep,
        each_pass=None,
    ) -> Iterator[Mapping[str, Any]]:
        """Yield this pane's rows until ``stop`` is set.

        This is the one poll loop a pane runs: the follow command's subscriber
        branch drives it rather than keeping a loop of its own, so the rows a
        pane paints and the rows a test reads arrive by the same path.
        ``each_pass`` is that caller's own per-pass work — the checks that end
        an arming — and returning false from it ends the loop, exactly as
        ``stop`` does.

        A quiet station yields nothing: a stale heartbeat is a verdict the
        obligations hook reports, not a sentence the pane repeats.
        """
        while stop is None or not stop.is_set():
            for event in self.poll():
                yield event
            for event in self.flush():
                yield event
            if each_pass is not None and not each_pass():
                return
            sleeper(interval)


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
