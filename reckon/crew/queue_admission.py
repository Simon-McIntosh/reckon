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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import queue_order, runs
from reckon.crew.dispatch_admission import (
    _LANE_GATE_WAITING_STATES,
    LaneHeld,
    LanePaused,
    _dispatch_fleet_gate,
    _dispatch_lane_gate,
)
from reckon.crew.dispatch_plan import _dispatch_lane_allowance
from reckon.crew.node import CrewError, TaskNode

ADMIT_LOCK_NAME = "admit.lock"
DEFAULT_ADMIT_INTERVAL_SECONDS = 5.0
LAUNCH_FAILED_PHASE = "launch-failed"
# A queued request whose last attempt failed for a reason other than a lane
# hold waits this long before it is attempted again, doubling per consecutive
# failure up to the cap. The back-off is bounded so a request that can never
# launch is still retried eventually rather than parked forever.
ADMISSION_RETRY_BACKOFF_SECONDS = 30.0
ADMISSION_RETRY_BACKOFF_CAP_SECONDS = 600.0


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


def _lane_gate_hold(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """The reason the lane's gate would refuse this request, or ``None``.

    The shared dispatch path reads the lane's gate document before it creates a
    worktree, and a paused or unreadable gate holds the launch there. Asking the
    same question here, before the queued pointer is released, lets a request the
    gate holds stay exactly as it is: no release, no failed launch,
    no re-queue for a request whose state never moved. The companion question —
    the session's lane allowance — is asked by :func:`_session_allowance`, whose
    ``0`` holds the session; together they are the two the dispatch path asks.
    """
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    backend = (config.get("backends") or {}).get(str(pointer.get("backend") or ""))
    if not isinstance(backend, Mapping) or not backend:
        return None
    gate = _dispatch_lane_gate(backend)
    if gate.get("state") in _LANE_GATE_WAITING_STATES:
        return str(
            gate.get("detail") or gate.get("reason") or gate.get("state") or "held"
        )
    return None


def _retry_delay(attempts: int) -> timedelta:
    """The bounded wait before a request that just failed may be retried."""
    seconds = ADMISSION_RETRY_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, ADMISSION_RETRY_BACKOFF_CAP_SECONDS))


def _failure_text(failure: Any) -> str:
    """The reason text of a recorded failure, whether it is a string or a row."""
    if isinstance(failure, Mapping):
        return str(failure.get("kind") or failure.get("detail") or failure)
    return str(failure or "")


def _is_lane_hold(failure: Any) -> bool:
    """Whether a recorded failure is the lane holding the request, not a fault.

    A lane hold is retried as soon as the lane opens, so it is never backed off;
    the two words are the exceptions ``_launch_queued`` raises for exactly this:
    the lane's allowance is zero, or its gate is paused.
    """
    return _failure_text(failure).startswith(("LaneHeld", "LanePaused"))


def _file_stamp(path: str) -> str:
    """A change detector for one dependency: its stat, or that it is absent."""
    if not path:
        return "none"
    try:
        stat = Path(path).stat()
    except OSError:
        return "absent"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _dependency_guard(
    pointer: Mapping[str, Any],
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, str]:
    """The things a queued request depends on, fingerprinted at failure time.

    The request is retried without waiting out its back-off the moment any of
    these changes: its brief file, its plan document, or the flight
    configuration that resolved its backend. Only their stamps are kept, so the
    guard is a comparison rather than a copy of the request.

    The lane document the router publishes is deliberately not watched: it is
    republished every few seconds, so its stamp moves constantly and would
    retry a request that can never launch at the publication cadence. The
    flight config layers change only when an operator changes the routing, so
    they are the honest dependency.
    """
    node = dict(pointer.get("node") or {})
    repo = str(pointer.get("repo") or "")
    project = str(pointer.get("project") or "")
    plan = str(node.get("plan") or "")
    brief = str(node.get("brief_path") or "")
    plan_path = (
        str(Path(repo) / "docs" / "plans" / f"{plan}.html") if repo and plan else ""
    )
    host_config = ""
    project_config = ""
    try:
        from reckon import flight

        host_config = str(flight.host_config_path())
        if repo:
            project_config = str(flight.project_config_path(project, repo))
    except Exception:  # noqa: BLE001 - an unresolvable flight path is simply not a dependency the guard can watch
        host_config = host_config or ""
        project_config = project_config or ""
    return {
        "brief": _file_stamp(brief),
        "plan": _file_stamp(plan_path),
        "host-config": _file_stamp(host_config),
        "project-config": _file_stamp(project_config),
    }


def _report_queued_failure(pointer: Mapping[str, Any]) -> None:
    """Surface a request's first failure once through the session's pane stream.

    The session reads the project's watch stream, so the report is one appended
    transition for the queued run. It is written once: the pointer records
    ``failure_reported_at`` on the first stamp, and every later pass sees it and
    reports nothing. A stream that cannot be written is not fatal: the record on
    the pointer is the fact, and a session attaching later still reads it there.
    """
    project = str(pointer.get("project") or "")
    run_id = str(pointer.get("run_id") or "")
    if not project or not run_id:
        return
    try:
        from reckon.crew.recovery import _fleet_counts
        from reckon.crew.recovery_watch import DEFAULT_WATCH_STALL_WINDOW

        live = runs._list_live_records(project=project)
        if all(str(record.get("run_id") or "") != run_id for record in live):
            live = [*live, pointer]
        snapshots = runs._watch_stream_snapshots(
            live, stall_window=DEFAULT_WATCH_STALL_WINDOW
        )
        snapshot = snapshots.get(run_id)
        if snapshot is None:
            return
        row = runs._stream_transition(
            project,
            snapshot=snapshot,
            previous=None,
            current=str(snapshot["state"]),
            counts=_fleet_counts(snapshots),
        )
        runs._append_watch_lines(runs.watch_stream_path(project), [row])
    except Exception:  # noqa: BLE001 - a report that cannot be written never stops admission
        return


def _retry_backoff_hold(
    pointer: Mapping[str, Any],
    *,
    moment: datetime,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """Why a failed request is left where it is, or ``None`` to attempt it.

    A request whose last attempt failed for a reason other than a lane hold is
    not attempted on every pass: the pass leaves its pointer and its recorded
    failure untouched until something it depends on changes or its bounded
    back-off elapses. A lane hold has no recorded back-off and is retried at
    once. This is what keeps a request that cannot launch — a missing brief, for
    one — from cycling through a release, a failed dispatch and a restore on
    every pass.
    """
    failure = pointer.get("failure")
    if not failure:
        return None
    if _is_lane_hold(failure):
        return None
    guard = pointer.get("failure_guard")
    if guard is not None and guard != _dependency_guard(pointer, config_for):
        return None
    retry_at = pointer.get("retry_at")
    if not retry_at:
        return None
    try:
        when = datetime.fromisoformat(str(retry_at))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if moment < when:
        return (
            f"last attempt failed: {_failure_text(failure)}; "
            f"not retried before {retry_at}"
        )
    return None


def _stamp_failure(
    record: dict[str, Any],
    failure: Any,
    *,
    moment: datetime,
    guard: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Record one attempt's failure and when the request may be retried."""
    attempts = int(record.get("failure_attempts") or 0) + 1
    record["failure"] = failure if isinstance(failure, str) else dict(failure)
    record["failure_at"] = moment.isoformat()
    # The first stamp of a failure is the one the session is told about, so the
    # report marker is set once and never moved by a later attempt.
    record.setdefault("failure_reported_at", moment.isoformat())
    record["failure_attempts"] = attempts
    record["retry_at"] = (moment + _retry_delay(attempts)).isoformat()
    if guard is not None:
        record["failure_guard"] = dict(guard)
    return record


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
        # Stamp the re-queue with the same back-off a failed attempt records, so
        # a request that keeps failing on launch is attempted once per back-off
        # rather than released, failed and restored on every pass.
        _stamp_failure(
            restored,
            failure or {"kind": LAUNCH_FAILED_PHASE},
            moment=_now(),
            guard=_dependency_guard(restored, None),
        )
        _write_pointer(str(pointer["run_id"]), restored)
        requeued.append(str(pointer["run_id"]))
    return requeued


def _write_pointer(run_id: str, record: dict[str, Any]) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _restore_queued(
    snapshot: Mapping[str, Any],
    failure: str,
    *,
    moment: datetime | None = None,
    guard: Mapping[str, Any] | None = None,
) -> None:
    """Write a launched request back as one queued pointer carrying the failure.

    The restored pointer carries the same bounded back-off a failure annotated
    in place would: a request whose release already withdrew it is retried once
    per back-off, not on every pass.
    """
    restored = copy.deepcopy(dict(snapshot))
    restored["phase"] = "queued"
    _stamp_failure(restored, failure, moment=moment or _now(), guard=guard)
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


def _record_queued_failure(
    snapshot: Mapping[str, Any],
    failure: str,
    requeued_run_id: str = "",
    *,
    moment: datetime | None = None,
    guard: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Return the retained request's id and any unrecoverable write error."""
    original_run_id = str(snapshot["run_id"])
    run_id = requeued_run_id or str(snapshot["run_id"])
    failed_at = moment or _now()

    def annotate(current: dict[str, Any]) -> dict[str, Any]:
        return _stamp_failure(current, failure, moment=failed_at, guard=guard)

    try:
        runs._mutate_pointer(run_id, annotate)
    except Exception as annotate_error:  # noqa: BLE001 - a failed write needs restoration
        try:
            _restore_queued(snapshot, failure, moment=failed_at, guard=guard)
        except Exception as restore_error:  # noqa: BLE001 - report storage failure to the pass
            annotation = f"annotation {type(annotate_error).__name__}: {annotate_error}"
            restoration = f"restore {type(restore_error).__name__}: {restore_error}"
            return original_run_id, f"{annotation}; {restoration}"
        return original_run_id, None
    return run_id, None


def _require_launched_record(candidate: Any) -> Mapping[str, Any]:
    if (
        not isinstance(candidate, Mapping)
        or not candidate.get("run_id")
        or not candidate.get("phase")
        or candidate.get("phase") == "queued"
    ):
        raise CrewError("dispatch did not return a launched record")
    return candidate


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
    if gate.get("state") in _LANE_GATE_WAITING_STATES:
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
                remaining[session] = _session_allowance(pointer, config_for=config_for)
            allowance = remaining[session]
            if allowance is not None and allowance <= 0:
                result["held"].append(str(pointer["run_id"]))
                continue
            gate_hold = _lane_gate_hold(pointer, config_for=config_for)
            if gate_hold is not None:
                # The gate would refuse the launch the release below feeds into,
                # so the request is left where it is: releasing it would emit a
                # withdrawal and a re-queue for a state that never moved. The
                # session-allowance hold above is the same question's other half.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = gate_hold
                continue
            backoff_hold = _retry_backoff_hold(
                pointer, moment=moment, config_for=config_for
            )
            if backoff_hold is not None:
                # The request failed for a reason other than a lane hold and
                # nothing it depends on has changed yet, so it is left exactly as
                # it is rather than released, failed and restored again.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                    backoff_hold
                )
                continue
            snapshot = copy.deepcopy(dict(pointer))
            first_report = not pointer.get("failure_reported_at")
            record = None
            failure = "dispatch did not return a launched record"
            requeued_run_id = ""
            caught: Exception | None = None
            reported_id = str(pointer["run_id"])
            restore_error = None
            try:
                candidate = _launch_queued(
                    pointer, launcher=launcher, config_for=config_for
                )
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("phase") == "queued"
                ):
                    requeued_run_id = str(candidate.get("run_id") or "")
                record = _require_launched_record(candidate)
            except Exception as exc:  # noqa: BLE001 - every dispatch failure keeps its request
                caught = exc
                failure = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, LaneHeld):
                    queued = getattr(exc, "queued", None)
                    if isinstance(queued, Mapping):
                        requeued_run_id = str(queued.get("run_id") or "")
            finally:
                if record is None:
                    reported_id, restore_error = _record_queued_failure(
                        snapshot,
                        failure,
                        requeued_run_id,
                        moment=moment,
                        guard=_dependency_guard(pointer, config_for),
                    )
            if record is None:
                if first_report:
                    _report_queued_failure(pointer)
                key = "held" if isinstance(caught, (LaneHeld, LanePaused)) else "failed"
                detail_key = "hold_detail" if key == "held" else "failure_detail"
                result[key].append(reported_id)
                result.setdefault(detail_key, {})[reported_id] = (
                    str(caught) if caught is not None else failure
                )
                if reported_id != str(pointer["run_id"]):
                    result.setdefault("superseded", {})[reported_id] = str(
                        pointer["run_id"]
                    )
                if restore_error is not None:
                    result.setdefault("restore_failure_detail", {})[reported_id] = (
                        restore_error
                    )
                continue
            _stamp_admission(str(record["run_id"]), snapshot, admitted_at=_utc_now())
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
