"""One node-local writer for fleet observations and their transition stream."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import checkout_trust, flight
from reckon.crew import (
    follower_reload,
    obligation_snapshot,
    recovery,
    recovery_liveness,
    recovery_watch,
    resumption,
    station_obligations,
    station_store,
    station_subscriber,
    ticker,
)
from reckon.crew.host_lease import LEASE_RENEW_SECONDS, HostLease, local_host
from reckon.crew.run_paths import crew_home
from reckon.crew_follow_commands import (
    _FOLLOWER_IMAGE_COMMIT_ENV as IMAGE_IDENTITY_ENV,
)
from reckon.crew_follow_commands import _FOLLOWER_IMAGE_PIN_ENV as IMAGE_PIN_ENV
from reckon.crew_follow_commands import _FOLLOWER_SOURCE_ROOT_ENV as SOURCE_ROOT_ENV

# How often one session's sweep runs with no fleet change to trigger it. A
# provider hold and a declared external wait both end on the clock, so a
# station that swept only on change would leave a lapsed run until something
# else in the fleet moved.
SWEEP_BEAT_SECONDS = resumption.DEFAULT_SWEEP_SECONDS

# The sweep's own record of who ran it. The station is not a follower and has
# no session of its own, so it takes the shared slot a hand-run pass uses
# rather than a follower's per-session entry.
SWEEP_WRITER = {"kind": "other", "key": "station"}


class StationHeldError(RuntimeError):
    """A station already holds this node's writer lease."""


# How often the station's own liveness beat is written. It is a handful of
# seconds because the beat's whole job is to stay inside the stale window while
# a tick is busy: the tick can spend minutes in one per-project refresh, and a
# beat written only by the tick hands every reader a station that reads stopped
# while it is working.
STATION_BEAT_SECONDS = 5.0


def _start_thread(run: Callable[[], None]) -> None:
    threading.Thread(target=run, name="station-sweep", daemon=True).start()


class StationBeat:
    """Write the station's heartbeat from a thread of its own.

    The beat is the one fact an off-node reader has about this station being
    alive, and the tick is the worst place to write it: a tick walks mounted
    projects across ledger reads and path resolution, and under a loaded
    filesystem one takes minutes. So liveness is written here, on its own
    thread and its own store handle, for as long as the process lives; what the
    tick records instead is progress -- the stage it has entered and the
    instants that bound it -- and the reader compares the two to tell a slow
    station from a stopped one.
    """

    def __init__(
        self,
        root: Path,
        *,
        beat_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
        store: station_store.StationStore | None = None,
        guard: station_store.StoreWriteGuard | None = None,
    ) -> None:
        self.root = root
        self.beat_seconds = (
            STATION_BEAT_SECONDS if beat_seconds is None else beat_seconds
        )
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._store = store
        self.guard = guard
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _handle(self) -> station_store.StationStore:
        """This thread's own store handle, opened on first use.

        A handle of its own rather than the tick's: the two write the same
        table from different threads, and a connection shared across threads is
        the kind of coupling that turns one slow tick into a corrupted beat.
        """
        if self._store is None:
            self._store = station_store.StationStore(self.root, guard=self.guard)
        return self._store

    def beat_once(self) -> None:
        """Move the heartbeat to now."""
        self._handle().heartbeat(now=self._clock())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.beat_once()
            except Exception:  # noqa: BLE001 - a missed beat is retried on the next
                print(
                    "station-beat-failure: the heartbeat could not be written",
                    file=sys.stderr,
                    flush=True,
                )
                traceback.print_exc(file=sys.stderr)
            self._stop.wait(self.beat_seconds)

    def start(self) -> None:
        """Beat once, then keep beating on its own thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="station-beat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop beating and close this thread's handle."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.beat_seconds * 2))
            self._thread = None
        if self._store is not None:
            self._store.close()
            self._store = None


class LeaseBeat:
    """Renew the station's writer lease from a thread of its own.

    A tick walks every mounted project and can outlast the lease's whole stale
    window, so a renewal written between ticks is no renewal at all: for most
    of a long tick the lease reads stale, and anything that asks -- the
    supervisor's station check or a hand-run ``crew station`` -- is free to
    claim it and start a second writer beside this one.

    So the renewal runs here, beside the beat thread and on the same handful of
    seconds, and touches only the lease file: no SQLite at all, so a heartbeat
    write waiting out its busy timeout cannot hold it. When a renewal fails the
    lease names another holder, and every row this station would commit from
    then on belongs beside that successor. The loss is marked on the store's
    shared guard, which refuses those writes, and the station's loop stops.
    """

    def __init__(
        self,
        lease: HostLease,
        *,
        guard: station_store.StoreWriteGuard,
        renew_seconds: float | None = None,
    ) -> None:
        self.lease = lease
        self.guard = guard
        self.renew_seconds = (
            LEASE_RENEW_SECONDS if renew_seconds is None else renew_seconds
        )
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def renew_once(self) -> bool:
        """Renew once, marking the loss and every write it refuses when it fails."""
        if self.lease.renew():
            return True
        self.guard.mark_lost(
            f"station lost its lease at {self.lease.path}; "
            "refusing to write beside its holder"
        )
        self._lost.set()
        return False

    def lost(self) -> bool:
        """Whether this station has lost its lease."""
        return self._lost.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.renew_once():
                    return
            except Exception:  # noqa: BLE001 - a failed read is retried, not a loss
                print(
                    "station-lease-failure: the lease could not be renewed",
                    file=sys.stderr,
                    flush=True,
                )
                traceback.print_exc(file=sys.stderr)
            self._stop.wait(self.renew_seconds)

    def start(self) -> None:
        """Renew once, then keep renewing on its own thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="station-lease", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop renewing."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.renew_seconds * 2))
            self._thread = None


class SweepBeat:
    """Run each session's sweep for the station, off its tick path.

    A sweep walks registered worktrees and can run for hours, so the station
    hands it to its own thread rather than paying for it between two ticks: a
    heartbeat stalled behind a sweep is a station an off-node reader calls
    stopped. A session already walking is not started again, and a session
    with nothing due is not started at all.
    """

    def __init__(
        self,
        *,
        sweep: Callable[..., Any] | None = None,
        start: Callable[[Callable[[], None]], None] | None = None,
        beat_seconds: float = SWEEP_BEAT_SECONDS,
    ) -> None:
        self.sweep = resumption.sweep if sweep is None else sweep
        self.beat_seconds = beat_seconds
        self._start = _start_thread if start is None else start
        self._last: dict[tuple[str, str], float] = {}
        self._in_flight: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def request(
        self,
        project: str,
        session: str,
        *,
        moment: datetime,
        changed: bool,
    ) -> bool:
        """Start this session's sweep when a change or the beat makes it due."""
        key = (project, session)
        stamp = moment.timestamp()
        with self._lock:
            last = self._last.get(key)
            due = changed or last is None or (stamp - last) >= self.beat_seconds
            if not due or key in self._in_flight:
                return False
            self._last[key] = stamp
            self._in_flight.add(key)
        self._start(lambda: self._run(project, session, key, moment))
        return True

    def _run(
        self, project: str, session: str, key: tuple[str, str], moment: datetime
    ) -> None:
        try:
            self.sweep(project, session=session, now=moment, writer=dict(SWEEP_WRITER))
        except Exception:  # noqa: BLE001 - one session's sweep costs one pass
            print(
                f"station-sweep-failure: {project}/{session}: "
                "the sweep could not complete",
                file=sys.stderr,
                flush=True,
            )
            traceback.print_exc(file=sys.stderr)
        finally:
            with self._lock:
                self._in_flight.discard(key)


def _sessions_to_sweep(pointers: list[Mapping[str, Any]]) -> list[str]:
    """The sessions this host's station sweeps.

    A session's runs can be launched on more than one host, and a sweep works a
    whole session rather than a run, so the host that sweeps has to be chosen
    for the session rather than tested per run: leaving out a session with any
    run elsewhere leaves it out on every host, and no station ever resumes or
    reviews it. Exactly one host owns each session instead, decided from the
    same pointers on every host — the host holding its first-sorted live run —
    so a split session is swept once and not twice. Ownership can move while a
    session's runs change; the sweep is idempotent per run, so a session whose
    owner moves is swept twice rather than dropped.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for pointer in pointers:
        session = str(pointer.get("session") or "")
        if session and str(pointer.get("run_id") or ""):
            grouped.setdefault(session, []).append(pointer)
    owned: list[str] = []
    for session, records in sorted(grouped.items()):
        owner = min(records, key=lambda record: str(record.get("run_id") or ""))
        if not recovery_liveness._record_is_known_foreign(owner):
            owned.append(session)
    return owned


def station_lease(root: Path) -> HostLease:
    """Name the one writer on this node through the shared lease mechanism."""
    return HostLease(
        root,
        "station",
        local_host(),
        os.getpid(),
        os.environ.get("SLURM_JOB_ID", ""),
    )


class StationReloader:
    """Replace the station with a proved image from the node's runtime root."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.import_root = Path(__file__).resolve().parents[2]
        self.source_root = Path(
            os.environ.get(SOURCE_ROOT_ENV, str(self.import_root))
        ).resolve()
        self.identity = os.environ.pop(IMAGE_IDENTITY_ENV, "") or (
            checkout_trust.source_code_identity(
                self.source_root / "reckon", source_root=self.source_root
            )
        )
        self.image_pin: int | None = None
        carried = os.environ.pop(IMAGE_PIN_ENV, "")
        if carried:
            try:
                candidate = int(carried)
                if checkout_trust.pin_matches_materialized_tree(
                    candidate, self.import_root
                ):
                    os.set_inheritable(candidate, False)
                    self.image_pin = candidate
            except (OSError, ValueError):
                pass
        self.deferred_identity: str | None = None

    def poll(self) -> None:
        """Check committed code after a tick and re-execute when it moves."""
        if self.image_pin is not None:
            checkout_trust.refresh_materialized_tree_pin(
                self.image_pin, self.import_root
            )
        current = checkout_trust.source_code_identity(
            self.source_root / "reckon", source_root=self.source_root
        )
        if current in (checkout_trust.IDENTITY_UNKNOWN, self.identity):
            return
        if current == self.deferred_identity or not re.fullmatch(
            r"[0-9a-f]{40}", current
        ):
            return
        image, _removed, _kept, pin = checkout_trust.materialize_committed_tree(
            self.source_root, current, active_root=self.import_root
        )
        if not image.is_relative_to(self.runtime_root):
            os.close(pin)
            raise RuntimeError(f"station reload image escaped runtime root: {image}")
        imports, _reason = checkout_trust.reckon_imports(image)
        if not imports:
            self.deferred_identity = current
            os.close(pin)
            return
        environment = follower_reload.image_reexec_environment(
            self.source_root, current, pin
        )
        try:
            os.execve(  # noqa: S606 - replacement keeps this station's pid
                sys.executable,
                follower_reload.image_reexec_argv(image),
                environment,
            )
        except OSError:
            os.close(pin)
            raise


class Station:
    """Ingest, classify and publish one ordered transition pass per tick."""

    def __init__(
        self,
        store: station_store.StationStore,
        *,
        home: Path | None = None,
        projects: Callable[[], Mapping[str, Path]] = flight.mounted_project_docs,
        stall_seconds: int = 900,
        sweeps: SweepBeat | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.home = home or crew_home()
        self.projects = projects
        self.stall_seconds = stall_seconds
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        # The review reflex and settle detection run here, once for every
        # session on the node, rather than in each follower.
        self.sweeps = SweepBeat() if sweeps is None else sweeps
        # The home this station's rows describe. A follower reads this store
        # only when its own home is the same one, because a store built from
        # another home holds another fleet's rows.
        self.store.set_station_value(station_store.CREW_HOME_KEY, str(self.home))

    def _enter(self, stage: str, *, started: datetime | None = None) -> None:
        """Record the stage this tick has entered, and its start when it opens.

        The stage is what a reader names when the tick outlives the stale
        window, and the start is what makes the tick's age measurable at all.
        Both are written before the work they name rather than after it, so a
        stage a reader sees is the stage the tick has not yet finished.
        """
        if started is not None:
            self.store.set_station_value(
                obligation_snapshot.STATION_TICK_STARTED_AT_KEY,
                started.astimezone(UTC).isoformat(),
            )
        self.store.set_station_value(obligation_snapshot.STATION_TICK_STAGE_KEY, stage)

    def tick(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Publish every new transition, then move the heartbeat."""
        moment = now or self.clock()
        stages = obligation_snapshot.STATION_TICK_STAGES
        self._enter(stages[0], started=moment)
        self.store.tick(home=self.home)
        self._enter(stages[1])
        verdicts = self.store.classify_tick(
            now_seconds=moment.timestamp(), stall_seconds=self.stall_seconds
        )
        # The heartbeat moves before anything is published, because every
        # snapshot this pass exports carries the heartbeat it reads: a station
        # that beat only at the end would hand its first reader the previous
        # station's beat, or none at all, and read as stopped while it started.
        self._enter(stages[2])
        self.store.heartbeat(now=moment)
        pointers = {
            str(row["run_id"]): json.loads(str(row["fields"]))
            for row in self.store.pointer_rows()
        }
        project_names = (
            set(self.projects())
            | self.store.recorded_projects()
            | {str(value.get("project") or "") for value in verdicts.values()}
            | {str(value.get("project") or "") for value in pointers.values()}
        )
        published: list[dict[str, Any]] = []
        # Every project's refresh and export happens in this one stage, because
        # it is the stage the night's slow ticks spent their time in: a reader
        # that names it says the station is walking a project, not that it died.
        self._enter(stages[3])
        for project in sorted(project_names - {""}):
            current = {
                run_id: verdict
                for run_id, verdict in verdicts.items()
                if str(pointers.get(run_id, {}).get("project") or "") == project
            }
            known = self.store.recorded_fleet(project)
            # A run first seen by this station is inventory. The producer's
            # fold represents an unknown run as a dispatched arrival, then
            # its actual verdict as a second row; the station records that
            # verdict as its baseline before asking the same fold for changes.
            for run_id, verdict in current.items():
                if run_id not in known:
                    known[run_id] = dict(verdict)
            folded, next_known = recovery.fleet_transitions(
                known,
                current,
                ledger_run_ids=recovery_watch._ledger_run_id_reader(project),
                handovers=recovery_watch.handover_run_ids(project),
            )
            project_pointers = [
                pointer
                for pointer in pointers.values()
                if str(pointer.get("project") or "") == project
            ]
            events: list[dict[str, Any]] = []
            for snapshot, previous, state, counts in folded:
                event = recovery._watch_transition(
                    project,
                    kind="transition",
                    snapshot=snapshot,
                    previous=previous,
                    current=state,
                    counts=counts,
                    spend_runs=project_pointers,
                    rate_statuses={},
                )
                event["audience"] = str(event.get("session") or "*")
                event["duty"] = ticker.transition_class(previous, state)
                events.append(event)
            self.store.record_tick(project, events, next_known, at=moment)
            published.extend(events)
            # The reading a pane's settle is judged by, written on every tick
            # rather than on change: a pane settles on emptiness, and a quiet
            # fleet is exactly the case that publishes no transition.
            project_live, unowned, live_sessions = (
                station_subscriber.live_work_populations(project_pointers)
            )
            station_subscriber.record_live_work(
                self.store,
                project,
                at=moment,
                project_live=project_live,
                unowned=unowned,
                sessions=live_sessions,
            )
            # Each session's duties are derived from the same fleet reading
            # this tick just committed, so a change the station published is a
            # change the coordinator's obligations reflect on the same pass.
            station_obligations.publish(
                self.store,
                project=project,
                home=self.home,
                changed=bool(events),
                now=moment,
            )
            # The sweep's two passes run for every session this host owns, on
            # a change and on the beat, so a hold or a declared
            # wait that ends while the fleet is quiet is still resumed, and a
            # settled run is still reviewed.
            for session in _sessions_to_sweep(project_pointers):
                self.sweeps.request(
                    project,
                    session,
                    moment=moment,
                    changed=bool(events),
                )
        self._enter(stages[4])
        self.store.sweep_retention(now=moment)
        # The finish stamp is what closes the tick for a reader: until it is
        # later than the start, a stage on record is a tick still running.
        self.store.set_station_value(
            obligation_snapshot.STATION_TICK_FINISHED_AT_KEY,
            self.clock().astimezone(UTC).isoformat(),
        )
        return published


def run_station(
    *,
    interval: float = 1.0,
    once: bool = False,
    store: station_store.StationStore | None = None,
    station_instance: Station | None = None,
    lease: HostLease | None = None,
    beat_seconds: float | None = None,
    renew_seconds: float | None = None,
) -> None:
    """Run the node's single station until stopped or one requested tick ends."""
    from reckon.crew.dispatch_launch import _adopt_launched_workers_from_reexec

    _adopt_launched_workers_from_reexec()
    if store is None:
        store = station_store.StationStore.at_runtime_root()
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    if lease is None:
        lease = station_lease(store.root)
    previous_holder = lease.holder()
    if previous_holder is not None and previous_holder.host == local_host():
        try:
            os.kill(previous_holder.pid, 0)
        except ProcessLookupError:
            if not lease.release_holder(previous_holder):
                raise StationHeldError(
                    f"station at {lease.path} changed holder before takeover"
                ) from None
    if not lease.claim():
        holder = lease.holder()
        identity = (
            f"{holder.host} pid {holder.pid}"
            if holder is not None
            else "unknown holder"
        )
        raise StationHeldError(f"station at {lease.path} is held by {identity}")
    with store:
        # A station hosted off the main thread cannot be signalled -- the stop
        # handler installs only in the interpreter's main thread -- so a caller
        # that runs it elsewhere keeps the handlers it already has.
        previous = (
            {
                signum: signal.signal(signum, stop)
                for signum in (signal.SIGINT, signal.SIGTERM)
            }
            if threading.current_thread() is threading.main_thread()
            else {}
        )
        beat: StationBeat | None = None
        renewal: LeaseBeat | None = None
        try:
            station = Station(store) if station_instance is None else station_instance
            reloader = StationReloader(store.root)
            # Both threads start before the first tick, so a station whose
            # opening tick is slow is never read as one that never started, and
            # its lease never lapses while that tick runs.
            beat = StationBeat(
                store.root,
                clock=station.clock,
                guard=store.guard,
                beat_seconds=beat_seconds,
            )
            beat.start()
            renewal = LeaseBeat(lease, guard=store.guard, renew_seconds=renew_seconds)
            renewal.start()
            while not stopping:
                if renewal.lost():
                    break
                try:
                    station.tick()
                except station_store.StationLeaseLostError as lost:
                    raise StationHeldError(
                        f"station lost its lease at {lease.path}: {lost}"
                    ) from None
                if renewal.lost():
                    break
                if once:
                    break
                reloader.poll()
                time.sleep(interval)
            if renewal.lost():
                raise StationHeldError(f"station lost its lease at {lease.path}")
        finally:
            if renewal is not None:
                renewal.stop()
            if beat is not None:
                beat.stop()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            lease.release()
