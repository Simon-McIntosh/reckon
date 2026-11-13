"""Store requests and yield a lane slot to older waiting work."""

from __future__ import annotations

from . import _unbound_dispatch_call
from ._imports import (
    Any,
    CrewError,
    Iterable,
    LaneHeld,
    Mapping,
    Path,
    TaskNode,
    _ask_session_host_for_follower,
    _ensure_watch_producer,
    _session_host_runs_follower,
    _session_host_waiting,
    _utc_now,
    datetime,
    flight,
    list_live,
    new_run_id,
    queue_dispatch,
    resolve_dispatch_authority,
    resolve_role_override,
    resolved_time_ceiling,
    validate_node,
    watch_arming_suppressed,
    watch_state,
)

_recorded_backlog = _unbound_dispatch_call

_FACADE_BINDINGS = ("_recorded_backlog",)


def _queued_request_record(
    *,
    run_id: str,
    project: str,
    node: TaskNode,
    repo_root: Path,
    session: str,
    backend_name: str,
    launch_kind: str,
    lane_allowance: Mapping[str, Any],
    dispatch_options: Mapping[str, Any] | None,
    queued_at: str,
    admitted_from: str = "",
    local: bool = True,
    backlog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The request record a held local dispatch stores as one queued pointer.

    Kept in one place so the held path and a re-dispatch of an already-queued
    node store the same shape, down to the request options the run launches from.
    A request an admission pass re-queues under a new id names the request it
    replaces, so the fold reads one request still waiting rather than a
    withdrawal and a new arrival.
    """
    record = {
        "run_id": run_id,
        "project": project,
        "plan": node.plan,
        "section": node.section,
        "repo": str(repo_root),
        "session": session,
        "node": node.as_dict(),
        "due": (dispatch_options or {}).get("due"),
        "urgency": (dispatch_options or {}).get("urgency"),
        "backend": backend_name,
        "launch": launch_kind,
        "local": local,
        "launcher_host": None,
        "phase": "queued",
        "queued_at": queued_at,
        "created_at": queued_at,
        "hold": dict(lane_allowance),
        "reason": lane_allowance.get("reason"),
        "dispatch_options": dict(dispatch_options or {}),
    }
    for key in ("picker_selection", "named_route", "route_fallback"):
        value = (dispatch_options or {}).get(key)
        if value is not None:
            record[key] = value
    if admitted_from:
        record["admitted_from"] = admitted_from
    if backlog is not None:
        # A request held at dispatch records the backlog it was made over just
        # as a launched run does, so the pointer says the same thing whichever
        # way the request went.
        record["unreconciled_override"] = dict(backlog)
    return record


def _queue_validated_waits(
    record: dict[str, Any],
    *,
    refs: list[str],
    wait_projects: Mapping[str, str],
    max_requests: int | None = None,
    cap: int | None = None,
    live: list[Mapping[str, Any]] | None = None,
    cache: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int | None, str]:
    """Store one prevalidated request and apply its current wait verdict."""
    from reckon.crew import queue_admission

    record["waits"] = list(refs)
    record["wait_projects"] = dict(wait_projects)
    queued, position = queue_dispatch(record, max_requests=max_requests, cap=cap)
    phase = str(queued.get("phase") or queue_admission.QUEUED_PHASE)
    if refs and phase == queue_admission.QUEUED_PHASE:
        phase = queue_admission._resolve_and_move(
            record,
            live=[*(live if live is not None else list_live()), record],
            docs_dir=Path(str(record["repo"])) / "docs",
            cache=cache if cache is not None else {},
            run_id=str(queued["run_id"]),
        )
    return queued, position, phase


def queue_requests(
    nodes: Iterable[TaskNode],
    *,
    project: str,
    session: str,
    repo_root: str | Path,
    config: Mapping[str, Any],
    backend_name: str = "",
    launch_kind: str = "cli",
    run_ids: Mapping[str, str] | None = None,
    cap: int | None = None,
    allow_unreviewed_plan: bool = False,
) -> dict[str, Any]:
    """Validate a request batch, then use dispatch's queue writer for each.

    ``allow_unreviewed_plan`` carries the plan-review waiver the same way a
    single dispatch does: it rides the request's ``dispatch_options``, which
    admission reads before it holds the request on the review gate. A caller
    that leaves it unset queues a request the gate judges on its own.
    """
    from reckon.crew import queue_admission
    from reckon.crew.dispatch_admission import (
        _require_write_paths_in_authority,
        admission_domain_findings,
        admission_refusals_and_warnings,
    )

    nodes = list(nodes)
    lane = str(backend_name or "").strip() or str(
        config.get("local_backend") or config.get("default_backend") or ""
    )
    if not lane:
        raise CrewError("queueing a request names no backend to queue it for")
    assigned_ids = {
        node.id: str((run_ids or {}).get(node.id) or new_run_id(node.id))
        for node in nodes
    }
    ceiling = resolved_time_ceiling(config)
    for node in nodes:
        # The whole node contract is judged here, beside the admission domain,
        # so a batch entry that a single dispatch would refuse is refused while
        # it is still a request rather than surfaced later at its launch, where
        # the refusal costs the queue position and reaches nobody. The role's
        # overlay is resolved against the lane the request records, so the
        # execution verdict does not depend on whatever lane admission later
        # binds.
        _lane_name, effective_backend = resolve_role_override(
            config, node.role, node.spec_level, lane
        )
        # The write-scope fence is judged here on the authority a launch
        # resolves, so a batch entry whose path lies outside the repository and
        # the delivery directories is refused while it is still a request. Left
        # to the launch, that refusal would cost the queue position and reach
        # nobody, and the entry could never run.
        _require_write_paths_in_authority(
            node, resolve_dispatch_authority(project, repo_root)
        )
        findings = admission_domain_findings(
            node, project=project, repository=repo_root
        )
        refusals, _warnings = admission_refusals_and_warnings(findings)
        verdict = validate_node(
            node,
            budget_ceiling=ceiling,
            execution_capable=effective_backend.get("execution_capable"),
            repository=repo_root,
        )
        refusals.extend(verdict.findings)
        if refusals:
            raise CrewError(
                f"node {node.id!r} is refused at queue time: "
                + "; ".join(str(finding.get("detail") or "") for finding in refusals)
            )
    live = list_live()
    refs_by_run, projects_by_run = queue_admission._validated_batch_waits(
        nodes,
        assigned_ids,
        project=project,
        repo_root=repo_root,
        live=live,
    )
    stored: dict[str, Any] = {}
    cache: dict[str, Any] = {}
    # Queuing a request succeeds over its session's backlog exactly as a direct
    # dispatch does, and records the backlog on the request it stores, so both
    # routes say which unreconciled runs the work was chosen over.
    backlog = _recorded_backlog(
        project=project,
        session=session,
        config=config,
        requested=False,
        source="coordinator",
    )
    for index, node in enumerate(nodes):
        run_id = assigned_ids[node.id]
        record = _queued_request_record(
            run_id=run_id,
            project=project,
            node=node,
            repo_root=Path(str(repo_root)),
            session=session,
            backend_name=lane,
            launch_kind=launch_kind,
            lane_allowance={},
            dispatch_options={
                "project": project,
                "session": session,
                "no_watch": True,
                "allow_unreviewed_plan": allow_unreviewed_plan,
            },
            queued_at=_utc_now(),
            backlog=backlog,
        )
        queued, position, phase = _queue_validated_waits(
            record,
            refs=refs_by_run[run_id],
            wait_projects=projects_by_run[run_id],
            max_requests=flight.queue_max_requests(config),
            cap=cap,
            live=live,
            cache=cache,
        )
        stored[str(queued["run_id"])] = {
            "node": node.id,
            "section": node.section,
            "phase": phase,
            "waits": list(queued.get("waits") or ()),
            "position": position,
            "index": index,
        }
    return stored


def _arrange_dispatch_follower(
    project: str,
    session: str,
    launch_kind: str,
    *,
    watch_required: bool,
    watch_override: bool,
) -> tuple[dict[str, Any], str, bool]:
    """Ensure the producer and ask the calling session's host to attach."""
    # A dispatch held or queued earlier in this process leaves its hand-off
    # unread, because it composes no launch record. Clearing it here keeps the
    # hand-off a fact about the dispatch that reaches the record.
    from reckon.crew.dispatch_watch import (
        hand_off_mismatched_session,
        take_session_hand_off,
    )

    take_session_hand_off()
    dispatch_watch = watch_state(project, session=session)
    session_delivery = "monitor"
    if watch_required and not watch_override and watch_arming_suppressed():
        watch_override = True
    if not watch_required or watch_override:
        return dispatch_watch, session_delivery, watch_override

    dispatch_watch = _ensure_watch_producer(project, session=session)
    if launch_kind == "cli" and session:
        attached = bool(dispatch_watch.get("session_attached"))
        if (not attached or _session_host_waiting()) and _ask_session_host_for_follower(
            project, session
        ):
            session_delivery = "host"
            dispatch_watch = watch_state(project, session=session)
        elif _session_host_runs_follower(
            project, session, (dispatch_watch.get("follower") or {}).get("pid")
        ):
            session_delivery = "host"
        else:
            # A --session name that is not this process's Claude session cannot
            # be asked of the run's own host: this process reaches its own
            # host FIFO alone. Hand the run to that host instead of skipping
            # delivery, so the caller's pane shows a run it dispatched under
            # another name.
            handoff = hand_off_mismatched_session(project, session)
            if handoff is not None:
                dispatch_watch["host_handoff"] = handoff
                if handoff["handed_to_host"]:
                    session_delivery = "host"
    return dispatch_watch, session_delivery, watch_override


def _answer_existing_node_run(
    existing: Mapping[str, Any],
    *,
    record: dict[str, Any],
    backend: Mapping[str, Any],
    lane_allowance: Mapping[str, Any],
    max_requests: int | None = None,
) -> LaneHeld:
    """The hold that names the run a node already has, writing no second pointer.

    A queued copy is updated in place — the re-dispatch is a change to that one
    request — while a run being admitted or already working is named back with
    its phase and no position, because it stands nowhere in the admission order.
    A run that is not queued is answered from the record it already has rather
    than by looking a position up for the new request.
    """
    phase = str(existing.get("phase") or "")
    if phase == "queued":
        for key in ("picker_selection", "named_route", "route_fallback"):
            if key in existing:
                record[key] = existing[key]
                record.setdefault("dispatch_options", {})[key] = existing[key]
        queued, position = queue_dispatch(record, max_requests=max_requests)
        held = LaneHeld(dict(lane_allowance))
        held.queued = {
            "run_id": str(queued["run_id"]),
            "position": position,
            "phase": str(queued.get("phase") or "queued"),
            "reason": queued.get("reason"),
        }
        return held
    reason = f"run {existing.get('run_id')} is already {phase or 'live'} for this node"
    # LaneHeld renders its own message from the gate's ``detail`` (falling back to
    # its state and path), so the reason is passed there: an existing-run answer
    # has no lane gate, and a gate-shaped message would read "the lane gate is
    # None at None".
    held = LaneHeld({"detail": reason, "reason": reason})
    held.queued = {
        "run_id": str(existing.get("run_id") or ""),
        "position": None,
        "phase": phase,
        "reason": f"already {phase or 'live'}",
    }
    return held


def _session_older_admissible_request(
    *,
    session: str,
    backend_name: str,
    moment: datetime,
) -> Mapping[str, Any] | None:
    """The oldest of a session's queued requests on a lane the pass could admit.

    A dispatch that finds room on its lane yields the slot to its own session's
    oldest waiting request for that lane, so a session's work launches in its
    order of arrival rather than whichever of its dispatches happened to see a
    free slot first. A request the admission pass would hold does not stand in
    the way: one inside its retry back-off after a refused launch and one the
    pass holds behind an earlier overlapping request are both skipped, and the
    next older admissible request is taken. A request that can never be admitted
    therefore never stops the session launching. The ranking among sessions is
    the admission pass's — only this session's own requests are ever chosen.
    """
    from reckon.crew import queue_admission, queue_order
    from reckon.crew import runs as runs_module

    live = runs_module.list_live()
    queued = [
        pointer for pointer in live if str(pointer.get("phase") or "") == "queued"
    ]
    if not queued:
        return None
    order = queue_order.admission_order(queued, live, now=moment)
    claims: dict[str, list[tuple[Path | None, Path]]] = {}
    for index, pointer in enumerate(order):
        if str(pointer.get("session") or "") != session:
            continue
        if str(pointer.get("backend") or "") != backend_name:
            continue
        if (
            queue_admission._retry_backoff_hold(pointer, moment=moment, config_for=None)
            is not None
        ):
            continue
        if (
            queue_admission._earlier_overlapping_request(
                pointer, order[:index], moment=moment, claims=claims
            )
            is not None
        ):
            continue
        return pointer
    return None


def _launch_yielded_queued_request(
    pointer: Mapping[str, Any],
    *,
    launcher,
    moment: datetime,
) -> Mapping[str, Any] | None:
    """Launch one queued request through the admission pass's own path.

    The request keeps its run id through launch and any re-queue. A failed
    launch annotates that same pointer, so yielding a lane slot to an older
    request cannot lose or duplicate it.
    """
    from reckon.crew import queue_admission
    from reckon.crew import runs as runs_module

    snapshot = dict(pointer)
    runs_module.write_handover(pointer)
    record: Mapping[str, Any] | None = None
    caught: Exception | None = None
    try:
        candidate = queue_admission._launch_queued(
            pointer, launcher=launcher, config_for=None
        )
        record = queue_admission._require_launched_record(candidate)
    except Exception as exc:  # noqa: BLE001 - every launch failure keeps its request
        caught = exc
    finally:
        if record is None:
            queue_admission._record_queued_failure(
                snapshot,
                f"{type(caught).__name__}: {caught}"
                if caught is not None
                else "dispatch did not return a launched record",
                moment=moment,
            )
        runs_module.clear_handover(pointer)
    return record
