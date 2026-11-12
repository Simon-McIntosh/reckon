"""Resolve dispatch previews and enforce preflight refusals."""

from __future__ import annotations

from ._imports import (
    Any,
    CompetenceLimit,
    CrewError,
    DispatchPlan,
    Mapping,
    Path,
    Sequence,
    TaskNode,
    _agent_configuration,
    _context_fit_verdict,
    _context_refusal,
    _raw_plan_dispatch,
    calibration_configuration_key,
    pointer_path,
    read_pointer,
    record_context_refusal,
    shared_verdict_inputs,
)

_FACADE_BINDINGS = ()


def _refuse_a_live_worker_before_the_worktree(run_id: str) -> None:
    """Refuse a dispatch of a run id whose worker this host still sees running.

    A dispatch on a non-local lane, and an admitted launch, are not answered by
    the existing-run branch: it answers only a queued request or a run on the
    lane that queues. Both therefore reach worktree creation, and this
    dispatch's own claim overwrites the live pointer before it gets there. The
    worker's tree then reads as a disposable leftover — its pointer no longer
    names it — so it is released and re-cut from a newer base, and the running
    worker is left writing into a tree its fence no longer covers.

    The holder is read through the composition a launch uses, ``live_worker_pid``
    and its companion reading narrowed by the same refusal predicate, so a
    dispatch and a launch cannot disagree about whether a run is held. The read
    is taken before this dispatch writes anything of its own: no claim, no run
    directory and no tree, so a refused dispatch leaves the run as it found it.
    A run id with no live pointer has never launched here and is not refused.
    A pointer that exists and cannot be read is refused rather than skipped: an
    unreadable record says nothing about whether its worker is still running,
    and proceeding on that silence would rebuild exactly the tree this check
    exists to keep.
    """
    from reckon.crew.dispatch_launch import _launch_refused_by, _live_worker_refusal
    from reckon.crew.recovery_liveness import live_worker_reading

    path = pointer_path(run_id)
    if not path.exists():
        return
    try:
        record = read_pointer(run_id)
    except (CrewError, OSError) as exc:
        if not path.exists():
            # It went away between the check and the read: no live run.
            return
        raise CrewError(
            f"run {run_id!r} cannot be dispatched: its live pointer at {path} "
            f"cannot be read ({exc}), so whether a worker still holds this run "
            "is unknown, and a relaunch could cut a tree beneath a live worker; "
            "inspect or remove that pointer before dispatching again"
        ) from exc
    holder = live_worker_reading(record)
    if not _launch_refused_by(record, holder):
        return
    attempt = record.get("attempt")
    attempt_text = f"attempt {attempt} " if isinstance(attempt, int) else ""
    raise CrewError(
        _live_worker_refusal(
            run_id,
            holder,
            f"{attempt_text}still holds this run, and cutting its worktree again "
            "would leave it writing into a tree no fence covers; observe or stop "
            "it before dispatching again",
            record,
        )
    )


def _gating_input_window(resolution: DispatchPlan, repo: Path) -> int | None:
    """The token figure that gates this lane's context fit, or ``None``.

    Read through the router's own verdict rather than recomputed here, so a lane
    whose declared window is malformed is refused by the reader that weighs it
    on the router's path instead of quietly reading as unbounded. A lane
    declaring neither a window nor a recorded boundary is unbounded, so nothing
    gates it and no request can be too large for it.
    """
    verdict = _context_fit_verdict(resolution=resolution, repo=repo)
    if verdict is None:
        return None
    return int(verdict["window_tokens"])


def _router_free_resolution(
    *,
    resolved_route: str,
    picker_selection: Mapping[str, Any] | None,
    local: bool,
    backend_override: str | None,
    default_backend_override: str | None,
    member: str,
) -> bool:
    """Whether this dispatch resolves a backend without a router answer.

    A lane the caller named — ``--local``, ``--backend``, the briefing's own
    default or a roster member bound to a harness — is a routing instruction in
    its own right, so an oversized request on it refuses as it always has rather
    than being moved. What is left is a pick that fell back, and the
    deterministic route, where the configured default stands in for an answer
    nothing gave.
    """
    if local or backend_override or default_backend_override or member:
        return False
    if resolved_route == "picker":
        return str((picker_selection or {}).get("action") or "") == "fallback"
    return True


def _oversized_context_refusal(
    resolution: DispatchPlan, repo: Path, request_estimate: int
) -> dict[str, Any]:
    """Build the context refusal both a dispatch and its preview answer with."""
    context = _context_fit_verdict(resolution=resolution, repo=repo)
    if context is None:
        raise CrewError(f"backend {resolution.backend!r} has no context window")
    window = int(context["window_tokens"])
    if request_estimate > window and context["estimated_tokens"] <= window:
        context = {
            **context,
            "allowed": False,
            "reason": "context-window-exceeded",
            "estimated_tokens": request_estimate,
            "shortfall_tokens": request_estimate - window,
        }
    agent = _agent_configuration(
        resolution.backend, resolution.launch, resolution.backend_settings
    )
    refusal = _context_refusal(
        context,
        agent_key=calibration_configuration_key({"agent": agent}),
        nested_context=True,
    )
    refusal["cache_status"] = shared_verdict_inputs(resolution.project, repo)[
        "cache_status"
    ]
    return refusal


def _reroute_oversized_request(
    *,
    resolution: DispatchPlan,
    node: TaskNode,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    authority: Mapping[str, Any] | None,
    session: str,
    comment: str,
    orchestrator_assessment: Mapping[str, Any] | None,
    records: list[dict[str, Any]] | None,
    verdict_inputs: Mapping[str, Any] | None,
    budget_snapshot: Mapping[str, Any] | None,
    snapshot_candidates: Sequence[Mapping[str, Any]],
    reroute_kwargs: Mapping[str, Any],
    caller_time_budget: str | None,
    caller_write_paths: list[str],
) -> tuple[DispatchPlan, dict[str, Any] | None]:
    """Resolve a router-free placement onto a lane whose window holds the node.

    The check is the one the router applies to every candidate it offers: a lane
    whose window the request's context estimate exceeds cannot run it, so
    placing the request there queues work that can only fail at launch. When the
    resolved lane fails that check, the fitting lanes are read from the picker's
    own candidate scan — the snapshot a timed-out pick had already built when it
    exists, and a fresh one otherwise — and the widest is taken. A request no
    permitted lane holds refuses with its shortfall instead of queueing.
    """
    window = _gating_input_window(resolution, repo)
    if window is None:
        return resolution, None
    from reckon.crew.picker import PickRequest
    from reckon.crew.picker import snapshot as picker_snapshot

    estimate = picker_snapshot.estimated_context_tokens(node, repo, authority=authority)
    if estimate <= window:
        return resolution, None
    candidates: Sequence[Mapping[str, Any]] = snapshot_candidates
    if not candidates:
        request = PickRequest(
            project,
            node,
            estimated_context=estimate,
            comment=comment,
            session=session,
            orchestrator_assessment=dict(orchestrator_assessment or {}),
        )
        candidates = [
            candidate.as_dict()
            for candidate in picker_snapshot.candidates(
                request,
                dict(config),
                repo,
                records=records,
                verdict_inputs=verdict_inputs,
                budget_snapshot=budget_snapshot,
                cached_only=True,
                authority=authority,
            )
        ]
    chosen = picker_snapshot.widest_fitting_candidate(candidates, resolution.backend)
    if chosen is None:
        refusal = _oversized_context_refusal(resolution, repo, estimate)
        record_context_refusal(node=node, project=project, verdict=refusal)
        raise CompetenceLimit(refusal)
    chosen_backend = str(chosen.get("backend") or "")
    # Re-resolve onto the chosen lane rather than patching the plan, so the new
    # backend gets its own execution fit, sandbox and write-path checks instead
    # of inheriting the oversized lane's. The caller's own time budget and write
    # paths are restored first, because the resolved plan already filled the
    # defaults a re-resolution would state again.
    node.time_budget = caller_time_budget
    node.write_paths = list(caller_write_paths)
    rerouted = plan_dispatch(
        node=node,
        config=config,
        project=project,
        repo=repo,
        authority=authority,
        session=session,
        local=chosen_backend == str(config.get("local_backend") or ""),
        backend_override=chosen_backend,
        route="deterministic",
        picker_selection=None,
        **dict(reroute_kwargs),
    )
    if not rerouted.validation.ok:
        raise CrewError(
            f"node is not dispatchable on context-fit lane {chosen_backend!r} — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in rerouted.validation.findings
            )
        )
    return rerouted, {
        "from_backend": resolution.backend,
        "to_backend": rerouted.backend,
        "reason": "context-fit",
    }


def _overridden_hold_route_mode() -> str:
    """The route mode a hold continued onto its offered local lane records.

    Imported lazily: the picker package's ``__init__`` pulls the snapshot and
    lane-context modules, which import back into this one, so resolving the
    constant at module load would read a partially initialized dispatcher.
    """
    from reckon.crew.picker.outcomes import OVERRIDDEN_HOLD_ROUTE_MODE

    return OVERRIDDEN_HOLD_ROUTE_MODE


def plan_dispatch(**kwargs: Any) -> DispatchPlan:
    """Resolve a preview through the same context placement as a real dispatch.

    The CLI requests live-conflict reporting for its preview. That report asks
    whether the request can actually be placed, including a picker fallback
    whose resolved lane cannot hold the request. Ordinary resolution callers
    keep the side-effect-free plan verdict.

    A preview reads plan and resource identities for the node's project, its
    sections and its refs, so it enters the same observation boundary an MCP
    read does: one covered-tree observation and one resource scan answer every
    lookup the preview makes, rather than one per resolution it asks.
    """
    from reckon.crew.queue_admission import plan_read_scopes

    with plan_read_scopes():
        return _plan_dispatch_reads(**kwargs)


def _plan_dispatch_reads(**kwargs: Any) -> DispatchPlan:
    node = kwargs["node"]
    caller_time_budget = node.time_budget
    caller_write_paths = list(node.write_paths)
    resolution = _raw_plan_dispatch(**kwargs)
    if not kwargs.get("report_live_conflicts") or not resolution.validation.ok:
        return resolution
    repo = kwargs.get("repo")
    if repo is None or not _router_free_resolution(
        resolved_route=resolution.route,
        picker_selection=kwargs.get("picker_selection"),
        local=bool(kwargs.get("local")),
        backend_override=kwargs.get("backend_override"),
        default_backend_override=kwargs.get("default_backend_override"),
        member=str(kwargs.get("member") or ""),
    ):
        return resolution
    project = str(kwargs.get("project") or "")
    rerouted, _fallback = _reroute_oversized_request(
        resolution=resolution,
        node=node,
        config=kwargs["config"],
        project=project,
        repo=Path(repo).resolve(),
        authority=resolution.authority,
        session=str(kwargs.get("session") or ""),
        comment="",
        orchestrator_assessment=None,
        records=None,
        verdict_inputs=None,
        budget_snapshot=None,
        snapshot_candidates=(),
        reroute_kwargs={
            "locked_decisions": kwargs.get("locked_decisions", ()),
            "peer_scopes": kwargs.get("peer_scopes"),
            "base": kwargs.get("base", "HEAD"),
            "execution_override": kwargs.get("execution_override", False),
            "orchestrator_lane_reason": kwargs.get("orchestrator_lane_reason"),
            "run_id": resolution.run_id,
            "allow_unreviewed_plan": kwargs.get("allow_unreviewed_plan", False),
            "repairs": kwargs.get("repairs", ""),
            "report_live_conflicts": True,
            "watch_required": kwargs.get("watch_required", False),
            "watch_override": kwargs.get("watch_override", False),
            "accept_directory_claim": kwargs.get("accept_directory_claim", False),
        },
        caller_time_budget=caller_time_budget,
        caller_write_paths=caller_write_paths,
    )
    return rerouted
