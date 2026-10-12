"""Admit queued local dispatches when the lane's router opens a slot.

One loop, run by the fleet supervisor as a declared service, reads the lane
document and the queued run pointers. Whenever the router grants a session
room it launches that session's highest-ranked queued entry through the same
dispatch path a coordinator uses, so the queued pointer becomes the dispatched
run. A launch the shared path refuses or fails leaves the request queued, with
the failure and its time recorded on it: the shared path rolls a refused spawn
back through ``_release_launch_claim``, which unlinks the pointer, so the
admission pass keeps the pointer's content before it launches and writes it
back when the launch does not survive.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew import queue_order, runs
from reckon.crew.dispatch_admission import LaneHeld, LanePaused, _dispatch_fleet_gate
from reckon.crew.dispatch_plan import _dispatch_lane_allowance
from reckon.crew.node import CrewError, TaskNode

ADMIT_LOCK_NAME = "admit.lock"
DEFAULT_ADMIT_INTERVAL_SECONDS = 5.0
LAUNCH_FAILED_PHASE = "launch-failed"
# The gate states in which no dispatch may start, shared with the dispatch path
# the coordinator uses so the drainer launches nothing a coordinator could not.
_GATE_WAITING_STATES = frozenset({"paused", "unreadable"})


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_now() -> str:
    return _now().isoformat()


def _admit_lock_path() -> Path:
    return runs.crew_home() / "locks" / ADMIT_LOCK_NAME


@contextmanager
def _admit_lock():
    """Serialise two admission passes so one queued run launches once."""
    path = _admit_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _node_from_pointer(pointer: Mapping[str, Any]) -> TaskNode:
    node = dict(pointer.get("node") or {})
    return TaskNode(
        id=str(node["id"]),
        goal=str(node.get("goal") or ""),
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        brief=str(node.get("brief") or ""),
        brief_sha256=str(node.get("brief_sha256") or ""),
        brief_path=str(node.get("brief_path") or ""),
        role=str(node.get("role") or "implement"),
        spec_level=str(node.get("spec_level") or ""),
        done_when=str(node.get("done_when") or ""),
        write_paths=list(node.get("write_paths") or ()),
        time_budget=str(node.get("time_budget") or ""),
        manifest_path=str(node.get("manifest_path") or ""),
        negative_control=str(node.get("negative_control") or ""),
        estimated_hours=node.get("estimated_hours"),
        requires_decisions=list(node.get("requires_decisions") or ()),
        peer_scopes=dict(node.get("peer_scopes") or {}),
    )


def _resolve_lane_config(pointer: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve flight config for the queued request's own project lanes."""
    from reckon import flight as flight_module
    from reckon.crew_dispatch_commands import _resolved_flight

    options = dict(pointer.get("dispatch_options") or {})
    overrides = tuple(options.get("overrides") or ())
    config = _resolved_flight(
        flight_module, str(pointer["project"]), pointer.get("repo"), overrides
    )
    if pointer.get("local"):
        config = flight_module.select_local_backend(config)
    return config


def _launch_queued(
    pointer: Mapping[str, Any],
    *,
    launcher: Callable[..., int] | None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Launch one queued run through the shared dispatch path a coordinator uses."""
    from reckon.crew.dispatch import dispatch

    options = dict(pointer.get("dispatch_options") or {})
    node = _node_from_pointer(pointer)
    # The queued pointer claims the run's own write paths, so the shared path
    # would refuse the launch as a scope conflict against the run itself. The
    # request is held in memory by the caller, which writes it back if the
    # launch does not survive, so the pointer is released here for the launch
    # to re-publish under its own identity.
    with contextlib.suppress(FileNotFoundError):
        runs.pointer_path(str(pointer["run_id"])).unlink()
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    return dispatch(
        node=node,
        project=str(pointer["project"]),
        repo=pointer.get("repo"),
        config=config,
        session=str(pointer["session"]),
        wave=str(options.get("wave") or ""),
        base=str(options.get("base") or "HEAD"),
        locked_decisions=tuple(options.get("locked_decisions") or ()),
        peer_scopes=node.peer_scopes,
        member=str(options.get("member") or ""),
        launcher=launcher,
        execution_override=bool(options.get("allow_execution_mismatch")),
        orchestrator_lane_reason=options.get("orchestrator_lane_reason") or None,
        unreconciled_override=bool(options.get("allow_unreconciled_runs")),
        unreviewed_plan_override=bool(options.get("allow_unreviewed_plan")),
        watch_required=True,
        watch_override=bool(options.get("no_watch")),
        local=bool(pointer.get("local")),
        backend_override=str(pointer.get("backend") or ""),
        repairs=str(options.get("repairs") or ""),
        accept_directory_claim=bool(options.get("accept_directory_claim")),
        no_fence_reason=str(options.get("no_fence_reason") or ""),
        route=options.get("route"),
        comment=str(options.get("comment") or ""),
        dispatch_options=options,
    )


def _session_allowance(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> float | None:
    """The extra-worker allowance the lane grants the queued run's session.

    ``None`` means no lane document is declared, so the lane publishes nothing
    to hold on and every queued run may be attempted. A published allowance of
    zero or less holds that session.
    """
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    backend = (config.get("backends") or {}).get(str(pointer.get("backend") or ""))
    if not backend:
        return None
    decision = _dispatch_lane_allowance(backend, session=str(pointer["session"]))
    if decision is None:
        return None
    if decision.get("held"):
        return 0
    allowance = decision.get("allowance")
    if isinstance(allowance, bool) or not isinstance(allowance, (int, float)):
        return None
    return float(allowance)


def _requeue_admitted_failures(live: list[dict[str, Any]]) -> list[str]:
    """Re-queue runs this drainer launched that the reaper marked launch-failed.

    The admission pass stamps every run it launches with the request it
    launched from. A run the reaper later marks ``launch-failed`` is a request
    still waiting behind a launch that never reached a model, so the next pass
    writes it back as one fresh queued pointer carrying the failure.
    """
    requeued = []
    for pointer in live:
        if str(pointer.get("phase") or "") != LAUNCH_FAILED_PHASE:
            continue
        request = pointer.get("admit_request")
        if not isinstance(request, Mapping):
            continue
        failures = list(pointer.get("launch_failures") or ())
        failure = dict(failures[-1]) if failures else {}
        restored = copy.deepcopy(dict(request))
        restored["run_id"] = str(pointer["run_id"])
        restored["phase"] = "queued"
        restored["queued_at"] = _utc_now()
        restored.setdefault("created_at", restored["queued_at"])
        restored["failure"] = failure or {"kind": LAUNCH_FAILED_PHASE}
        restored["failure_at"] = _utc_now()
        _write_pointer(str(pointer["run_id"]), restored)
        requeued.append(str(pointer["run_id"]))
    return requeued


def _write_pointer(run_id: str, record: dict[str, Any]) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _restore_queued(snapshot: Mapping[str, Any], failure: str) -> None:
    """Write a launched request back as one queued pointer carrying the failure."""
    restored = copy.deepcopy(dict(snapshot))
    restored["phase"] = "queued"
    restored["failure"] = failure
    restored["failure_at"] = _utc_now()
    for key in (
        "pid",
        "pid_start_time",
        "worktree",
        "log_path",
        "stderr_path",
        "prompt_path",
        "admitted_at",
    ):
        restored.pop(key, None)
    _write_pointer(str(snapshot["run_id"]), restored)


def admit_once(
    *,
    launcher: Callable[..., int] | None = None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    lane: str = "",
    now: datetime | None = None,
    lock: bool = True,
) -> dict[str, Any]:
    """Run one admission pass and report what it launched, held and failed.

    The pass reads the fleet gate, then the queued and live pointers. It ranks
    the queued entries by current lane occupancy and admits each in turn while
    the lane grants its session room. A held or paused gate launches nothing.
    ``lock`` serialises the pass against a concurrent drainer so one queued run
    launches once; a caller already holding the admit lock passes ``lock=False``.
    """
    moment = now or _now()
    gate = _dispatch_fleet_gate()
    result: dict[str, Any] = {
        "gate": gate.get("state"),
        "launched": [],
        "held": [],
        "failed": [],
        "requeued": [],
    }
    if gate.get("state") in _GATE_WAITING_STATES:
        result["action"] = "gated"
        return result
    with _admit_lock() if lock else nullcontext():
        result["requeued"] = _requeue_admitted_failures(
            runs.list_live(phase=LAUNCH_FAILED_PHASE)
        )
        live = runs.list_live()
        queued = [pointer for pointer in live if pointer.get("phase") == "queued"]
        if lane:
            queued = [
                pointer for pointer in queued if str(pointer.get("backend")) == lane
            ]
        order = queue_order.admission_order(queued, live, now=moment)
        remaining: dict[str, float | None] = {}
        for pointer in order:
            session = str(pointer["session"])
            if session not in remaining:
                remaining[session] = _session_allowance(
                    pointer, config_for=config_for
                )
            allowance = remaining[session]
            if allowance is not None and allowance <= 0:
                result["held"].append(str(pointer["run_id"]))
                continue
            snapshot = copy.deepcopy(dict(pointer))
            try:
                record = _launch_queued(
                    pointer, launcher=launcher, config_for=config_for
                )
            except (LaneHeld, LanePaused) as exc:
                # The shared path already refreshed the queued pointer; the
                # request stays waiting rather than being destroyed.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = str(exc)
                continue
            except (CrewError, OSError) as exc:
                _restore_queued(snapshot, f"{type(exc).__name__}: {exc}")
                result["failed"].append(str(pointer["run_id"]))
                result.setdefault("failure_detail", {})[str(pointer["run_id"])] = str(
                    exc
                )
                continue
            _stamp_admission(
                str(record["run_id"]), snapshot, admitted_at=_utc_now()
            )
            result["launched"].append(str(record["run_id"]))
            if allowance is not None:
                remaining[session] = allowance - 1
        result["action"] = "admitted" if result["launched"] else "idle"
        return result


def _stamp_admission(
    run_id: str, request: Mapping[str, Any], *, admitted_at: str
) -> None:
    """Record on a launched run the queued request it was admitted from.

    The stamp is what lets the next pass recognise a run this drainer launched
    and re-queue it when the reaper marks it ``launch-failed``.
    """

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admitted_at"] = admitted_at
        pointer["admit_request"] = copy.deepcopy(dict(request))
        return pointer

    runs._mutate_pointer(run_id, mutation)


def admit_until_stopped(
    *,
    lane: str = "",
    interval: float = DEFAULT_ADMIT_INTERVAL_SECONDS,
    once: bool = False,
) -> dict[str, Any]:
    """Repeat the admission pass on a cadence until stopped.

    ``--once`` runs a single pass and returns, which is what a test drives. The
    loop otherwise catches ``KeyboardInterrupt`` so a stopped service returns
    the count of passes it ran rather than a traceback.
    """
    if once:
        return admit_once(lane=lane)
    passes = 0
    try:
        while True:
            admit_once(lane=lane)
            passes += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        return {"passes": passes}