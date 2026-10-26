"""One node-local writer for fleet observations and their transition stream."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import flight
from reckon.crew import recovery, recovery_watch, station_store, ticker
from reckon.crew.run_paths import crew_home

LOCK_NAME = "station.lock"


class StationHeldError(RuntimeError):
    """A station already holds this node's writer lock."""


@contextmanager
def node_lock(root: Path) -> Iterator[None]:
    """Hold one stable lock inode for the station's entire process lifetime."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_NAME
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip() or "unknown holder"
            raise StationHeldError(f"station at {path} is held by {holder}") from exc
        holder = {"pid": os.getpid(), "host": socket.gethostname()}
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(holder, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Station:
    """Ingest, classify and publish one ordered transition pass per tick."""

    def __init__(
        self,
        store: station_store.StationStore,
        *,
        home: Path | None = None,
        projects: Callable[[], Mapping[str, Path]] = flight.mounted_project_docs,
        stall_seconds: int = 900,
    ) -> None:
        self.store = store
        self.home = home or crew_home()
        self.projects = projects
        self.stall_seconds = stall_seconds

    def tick(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Publish every new transition, then move the heartbeat."""
        moment = now or datetime.now(tz=UTC)
        self.store.tick(home=self.home)
        verdicts = self.store.classify_tick(
            now_seconds=moment.timestamp(), stall_seconds=self.stall_seconds
        )
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
        self.store.heartbeat(now=moment)
        self.store.sweep_retention(now=moment)
        return published


def run_station(*, interval: float = 1.0, once: bool = False) -> None:
    """Run the node's single station until stopped or one requested tick ends."""
    from reckon.crew.dispatch_launch import _adopt_launched_workers_from_reexec

    _adopt_launched_workers_from_reexec()
    store = station_store.StationStore.at_runtime_root()
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    with node_lock(store.root), store:
        previous = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            station = Station(store)
            while not stopping:
                station.tick()
                if once:
                    break
                time.sleep(interval)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
