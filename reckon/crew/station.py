"""One node-local writer for fleet observations and their transition stream."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import checkout_trust, flight
from reckon.crew import recovery, recovery_watch, station_store, ticker
from reckon.crew.host_lease import HostLease, local_host
from reckon.crew.run_paths import crew_home


class StationHeldError(RuntimeError):
    """A station already holds this node's writer lease."""


SOURCE_ROOT_ENV = "RECKON_STATION_SOURCE_ROOT"
IMAGE_IDENTITY_ENV = "RECKON_STATION_IMAGE_IDENTITY"
IMAGE_PIN_ENV = "RECKON_STATION_IMAGE_PIN"


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
        original_cache = checkout_trust._committed_tree_cache
        checkout_trust._committed_tree_cache = lambda: (
            self.runtime_root / "reload-trees"
        )
        try:
            image, _removed, _kept, pin = checkout_trust.materialize_committed_tree(
                self.source_root, current, active_root=self.import_root
            )
        finally:
            checkout_trust._committed_tree_cache = original_cache
        if not image.is_relative_to(self.runtime_root):
            os.close(pin)
            raise RuntimeError(f"station reload image escaped runtime root: {image}")
        imports, _reason = checkout_trust.reckon_imports(image)
        if not imports:
            self.deferred_identity = current
            os.close(pin)
            return
        os.set_inheritable(pin, True)
        environment = dict(os.environ)
        environment[SOURCE_ROOT_ENV] = str(self.source_root)
        environment[IMAGE_IDENTITY_ENV] = current
        environment[IMAGE_PIN_ENV] = str(pin)
        environment.setdefault("RECKON_SHIM_PYTHON", sys.executable)
        launcher = (
            "import sys; "
            f"sys.path.insert(0, {str(image)!r}); "
            "from reckon.cli import main; main()"
        )
        try:
            os.execve(  # noqa: S606 - replacement keeps this station's pid
                sys.executable,
                [sys.executable, "-c", launcher, *sys.argv[1:]],
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
        previous = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            station = Station(store)
            reloader = StationReloader(store.root)
            renewed_at = time.monotonic()
            while not stopping:
                station.tick()
                if time.monotonic() - renewed_at >= 5:
                    if not lease.renew():
                        raise StationHeldError(
                            f"station lost its lease at {lease.path}"
                        )
                    renewed_at = time.monotonic()
                if once:
                    break
                reloader.poll()
                time.sleep(interval)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            lease.release()
