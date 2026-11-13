"""Validate dispatch and prepare the values consumed by a launch."""

from __future__ import annotations

from . import _unbound_dispatch_call
from ._imports import (
    _LANE_GATE_WAITING_STATES,
    ATTEMPT_RECORD_NAME,
    DEFAULT_MEMBER_IDLE_WINDOW,
    FENCE_WORKERS,
    UTC,
    Any,
    BudgetHold,
    CompetenceLimit,
    CrewError,
    Iterable,
    LaneHeld,
    LanePaused,
    Mapping,
    Path,
    TaskNode,
    _actionable_budget_hold,
    _adjacent_live_peers,
    _agent_configuration,
    _backends,
    _brief_record,
    _budget_verdict,
    _candidate_scope_entries,
    _claim_released_on_refusal,
    _competence_verdict,
    _directory_claim_acceptance_kwargs,
    _dispatch_lane_gate,
    _disposable_member_id,
    _merge_peer_scopes,
    _peer_scopes_without_shared_landing_paths,
    _picker_refusal_reasons,
    _publish_launch_claim,
    _raise_repository_scope_conflict,
    _refuse_against_the_bookend_reserve,
    _refuse_over_concurrency_ceiling,
    _repository_scope_claims,
    _resolve_commit,
    _resolved_wave_id,
    _stamp_agent_display,
    _store_brief,
    _supervisor_write,
    _task_session_resolution,
    _utc_now,
    _watcher_delivery_admission,
    _workspace_roots,
    build_picker_inputs,
    continue_hold_on_its_offered_local_lane,
    datetime,
    dispatch_picker_selection,
    domain_rules_block,
    existing_node_run,
    flight,
    format_refusal,
    ledger,
    list_live,
    queue_dispatch,
    read_pointer,
    reap_idle_session_members,
    record_context_refusal,
    refuse_member_in_flight,
    resolve_budget_fallback,
    resolve_dispatch_authority,
    resolve_dispatch_ledger_root,
    resolve_dispatch_route,
    resolve_project_repository,
    run_dir,
)

# The facade binds cyclic calls after all concerns have been imported.
_recorded_backlog = _unbound_dispatch_call
_answer_existing_node_run = _unbound_dispatch_call
_arrange_dispatch_follower = _unbound_dispatch_call
_launch_dispatch = _unbound_dispatch_call
_launch_yielded_queued_request = _unbound_dispatch_call
_queue_validated_waits = _unbound_dispatch_call
_queued_request_record = _unbound_dispatch_call
_refuse_a_live_worker_before_the_worktree = _unbound_dispatch_call
_request_claim_restored_on_refusal = _unbound_dispatch_call
_reroute_oversized_request = _unbound_dispatch_call
_router_free_resolution = _unbound_dispatch_call
_session_older_admissible_request = _unbound_dispatch_call
plan_dispatch = _unbound_dispatch_call

_FACADE_BINDINGS = (
    "_recorded_backlog",
    "_answer_existing_node_run",
    "_arrange_dispatch_follower",
    "_launch_dispatch",
    "_launch_yielded_queued_request",
    "_queue_validated_waits",
    "_queued_request_record",
    "_refuse_a_live_worker_before_the_worktree",
    "_request_claim_restored_on_refusal",
    "_reroute_oversized_request",
    "_router_free_resolution",
    "_session_older_admissible_request",
    "plan_dispatch",
)


def dispatch(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path | None,
    config: Mapping[str, Any],
    session: str,
    wave: str = "",
    base: str = "HEAD",
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    member: str = "",
    launcher=None,
    check_budget: bool = True,
    budget_state: Mapping[str, Any] | None = None,
    execution_override: bool = False,
    orchestrator_lane_reason: str | None = None,
    unreconciled_override: bool = False,
    unreviewed_plan_override: bool = False,
    watch_required: bool = False,
    watch_override: bool = False,
    lineage_override: Mapping[str, Any] | None = None,
    worktree_session: str | None = None,
    local: bool = False,
    backend_override: str | None = None,
    lane_origin: str = "",
    default_backend_override: str | None = None,
    repairs: str = "",
    accept_directory_claim: bool = False,
    no_fence_reason: str = "",
    route: str | None = None,
    comment: str = "",
    picker_selection: Mapping[str, Any] | None = None,
    dispatch_options: Mapping[str, Any] | None = None,
    admission_launch: bool = False,
    admitted_from: str = "",
    readiness_cache: dict[str, Any] | None = None,
    run_id: str | None = None,
    partner_masks: Iterable[str] | None = None,
    paired_shadow_run_id: str = "",
) -> dict[str, Any]:
    """Validate, prepare and launch one node; return its run record.

    The single branch is on launch kind. A ``cli`` backend is spawned here and
    the caller yields on the returned run id. An ``in-harness`` backend cannot
    be spawned by reckon at all, so everything a worker needs is prepared and
    returned as a directive the calling harness dispatches itself, binding its
    task back with :func:`attach`.

    Naming a roster ``member`` routes the node into that member's long-lived
    session, and that member's own in-flight run refuses a second dispatch to
    it. Omitting it makes the dispatch disposable: the run carries its own
    identity and registers no roster row, so no unrelated task is refused for
    the member another run in flight happens to hold. Two dispatches of one
    node for the same project, session and node id are serialised by an
    exclusive claim over the node's worktree path: the second is refused
    before any worktree exists, naming the in-flight dispatch, so exactly one
    of two concurrent dispatches launches.

    A node whose backend has no headroom left is *held* rather than dispatched:
    :class:`BudgetHold` is raised before any worktree exists, so the node stays
    ready and nothing has to be judged or unwound. Holding costs nothing; a wave
    launched into a spent quota costs its whole setup plus half-finished commits.

    Either way the operation is atomic: a failure after the worktree exists
    removes it and writes no pointer, so no orphan is left holding write scope.
    An execution-fit override is an explicit exception to a heuristic refusal;
    the matched measure and resolved role stay on the run record so the exception
    remains visible after the request that supplied it is gone.

    A session's overdue unreconciled runs never refuse a dispatch. They are
    duties, listed by the obligations view, and this dispatch copies the exact
    runs it was made over onto the new record so a later reader sees the work
    was started over unreconciled runs. ``--allow-unreconciled-runs`` still
    parses and is still recorded, and gates nothing.

    Dispatch arms the project watcher before creating a worktree when watcher
    policy is enabled. The producer is detached from the caller and keeps a
    supervisor as its live parent, so it remains valid after the dispatching
    process exits. A watch override records both the arming command and the
    liveness observed at the dispatch gate.

    The repository is the project's own mount, resolved before any worktree,
    pointer or ledger row exists, so a dispatch run from another checkout
    cannot cut its worktree from that checkout.

    ``admitted_from`` names the queued request an admission pass is launching.
    It is written on the first pointer this dispatch publishes, launched or
    re-queued, so the fleet fold reads the new run as that request continuing
    rather than as one run withdrawn and an unrelated one arriving.

    ``readiness_cache`` is an admission pass's readiness cache, which the pass
    hands to every launch it makes so one project's roadmap is derived once for
    the pass. A direct dispatch supplies none and builds its own.

    ``lane_origin`` is recorded as the ``set_by`` of the ``named_route`` a
    caller-named backend leaves on the pointer. A review the reflex composes
    names its lane itself, following the run under review, which admission may
    rebind when that lane is held; a coordinator's own naming records the
    default and stays authoritative.
    """
    repo_root = resolve_project_repository(project, repo)
    worktree_identity = str(worktree_session or session)
    shadow_lineage = (
        dict(lineage_override)
        if isinstance(lineage_override, Mapping)
        and lineage_override.get("kind") == "shadow"
        else None
    )
    if lineage_override is not None and shadow_lineage is None:
        raise CrewError(
            format_refusal(
                "D20", "only shadow lineage may be supplied explicitly at dispatch"
            )
        )
    # A paired dispatch hands each arm the surfaces its partner's allocation
    # names, so neither arm resolves its partner from a pointer or a ledger row
    # the partner does not have yet. The list is fixed at the call and passed
    # through unchanged; a run that supplies none composes masks the shadow way.
    paired_masks: tuple[str, ...] | None = (
        tuple(str(path) for path in partner_masks)
        if partner_masks is not None
        else None
    )
    authority = resolve_dispatch_authority(project, repo_root)
    ledger_root = resolve_dispatch_ledger_root(authority)
    # Captured before plan_dispatch fills in per-backend defaults, so a budget
    # fallback's re-resolution (below) starts from what the caller actually
    # asked for rather than carrying the held backend's defaults forward.
    caller_time_budget = node.time_budget
    caller_write_paths = list(node.write_paths)
    caller_named_route = bool(local or backend_override or default_backend_override)
    # Each picker input is built independently behind a guard: an unreadable
    # ledger, a conflicting mount or a raising budget view is recorded against
    # the input that failed and the picker is left to fall back, so a picker
    # meant only to inform the dispatch can never abort the dispatch itself.
    resolved_route = resolve_dispatch_route(config, route)
    # The coordinator's reading of the node's difficulty rides the dispatch
    # options, which survive onto the pointer the queue replays, so a run the
    # admission gate launches later carries the same assessment its request did.
    orchestrator_assessment = dict(
        (dispatch_options or {}).get("orchestrator_assessment") or {}
    )
    due = (dispatch_options or {}).get("due") or None
    urgency = (dispatch_options or {}).get("urgency") or None
    # A plan review composes under one node id for every revision of the plan,
    # so an earlier review of the same plan is a different piece of work rather
    # than a previous attempt of this one. The compose marks
    # such a dispatch fresh; the marker rides the dispatch options onto the
    # queued record the admission pass replays, so a review held by the lane and
    # launched later records a first attempt too.
    fresh_review = bool((dispatch_options or {}).get("fresh_review"))
    deferred_shadow_selection = (
        picker_selection is None
        and resolved_route != "picker"
        and (
            resolved_route == "deterministic"
            or local
            or bool(backend_override or default_backend_override)
        )
    )
    picker_records: list[dict[str, Any]] | None = None
    picker_inputs: Mapping[str, Any] | None = None
    picker_budget: Mapping[str, Any] | None = None
    # The candidate scan a pick builds before it asks, carried out of a pick that
    # gave up so the context-fit check below weighs the lanes that pick already
    # measured rather than reading every window a second time.
    snapshot_candidates: list[Mapping[str, Any]] = []
    if picker_selection is not None:
        # The caller already asked the picker and ran the availability check on
        # its answer, so asking again would both double the pick's latency and
        # let the second pick choose a backend the check never saw. Reuse the
        # caller's answer so the checked backend is the dispatched backend.
        picker_selection = dict(picker_selection)
    elif not deferred_shadow_selection:
        (
            picker_records,
            picker_inputs,
            picker_budget,
            picker_input_errors,
        ) = build_picker_inputs(
            project, config, repo_root, ledger_root=ledger_root, session=session
        )
        picker_selection = dispatch_picker_selection(
            node=node,
            config=config,
            project=project,
            repo=repo_root,
            session=session,
            comment=comment,
            authority=authority,
            records=picker_records,
            verdict_inputs=picker_inputs,
            budget_snapshot=picker_budget,
            input_errors=picker_input_errors,
            orchestrator_assessment=orchestrator_assessment,
            due=due,
            urgency=urgency,
        )
    if picker_selection is not None:
        # The scan is this check's input rather than part of the answer the pick
        # gave, so it is taken out of the selection here — whichever branch asked
        # — before anything records that selection.
        snapshot_candidates = list(
            picker_selection.pop("snapshot_candidates", ()) or ()
        )
    # A hold whose offered candidates include the local lane is a wait for that
    # lane, not a refusal: the request is validated and continued there, and the
    # lane allowance then launches it if the lane has room or queues it. The hold
    # is kept aside so the run still records Jev's answer, and the picker-hold
    # record is written with the run it became. The rule lives beside the
    # resolution step so a preview continues a hold exactly as this does.
    picker_selection, held_selection = continue_hold_on_its_offered_local_lane(
        picker_selection=picker_selection,
        config=config,
        route=route,
        due=due,
        urgency=urgency,
    )
    if held_selection is not None:
        local = True
    try:
        resolution = plan_dispatch(
            node=node,
            config=config,
            locked_decisions=locked_decisions,
            peer_scopes=peer_scopes,
            project=project,
            repo=repo_root,
            base=base,
            execution_override=execution_override,
            orchestrator_lane_reason=orchestrator_lane_reason,
            authority=authority,
            local=local,
            run_id=run_id,
            backend_override=backend_override,
            default_backend_override=default_backend_override,
            member=member,
            allow_unreviewed_plan=unreviewed_plan_override,
            repairs=repairs,
            session=session,
            route=route,
            picker_selection=picker_selection,
        )
    except BudgetHold:
        if (
            resolve_dispatch_route(config, route) == "picker"
            and picker_selection is not None
            and picker_selection.get("action") == "hold"
        ):
            from reckon.crew.picker.outcomes import record_picker_hold

            record_picker_hold(
                project=project,
                node=node.id,
                plan=node.plan,
                selection=picker_selection,
                reason=_picker_refusal_reasons(picker_selection),
            )
        raise
    route = resolution.route
    if not resolution.validation.ok:
        raise CrewError(
            "node is not dispatchable — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )

    # A backend resolved without a router answer is the configured default
    # standing in, and nothing has yet weighed it against the node's context
    # estimate. Applying the router's own fit check here is what keeps an
    # oversized request off a lane that can only refuse it at launch, where a
    # full lane would have queued it first.
    context_fit_fallback: dict[str, Any] | None = None
    if _router_free_resolution(
        resolved_route=resolved_route,
        picker_selection=picker_selection,
        local=local,
        backend_override=backend_override,
        default_backend_override=default_backend_override,
        member=member,
    ):
        resolution, context_fit_fallback = _reroute_oversized_request(
            resolution=resolution,
            node=node,
            config=config,
            project=project,
            repo=repo_root,
            authority=authority,
            session=session,
            comment=comment,
            orchestrator_assessment=orchestrator_assessment,
            records=picker_records,
            verdict_inputs=picker_inputs,
            budget_snapshot=picker_budget,
            snapshot_candidates=snapshot_candidates,
            reroute_kwargs={
                "locked_decisions": locked_decisions,
                "peer_scopes": peer_scopes,
                "base": base,
                "execution_override": execution_override,
                "orchestrator_lane_reason": orchestrator_lane_reason,
                "run_id": resolution.run_id,
                "allow_unreviewed_plan": unreviewed_plan_override,
                "repairs": repairs,
            },
            caller_time_budget=caller_time_budget,
            caller_write_paths=caller_write_paths,
        )
        route = resolution.route

    dispatch_options = dict(dispatch_options or {})
    dispatch_options.pop("picker_selection", None)
    dispatch_options.setdefault("route", route or resolution.route)
    if context_fit_fallback is not None:
        dispatch_options["route_fallback"] = context_fit_fallback
    if not admission_launch and caller_named_route:
        dispatch_options["named_route"] = {
            "backend": resolution.backend,
            "set_by": lane_origin or "dispatch",
        }

    _workspace_roots(repo_root)
    candidate_scope = (
        ()
        if shadow_lineage
        else _candidate_scope_entries(
            node, project=project, repo=repo_root, authority=authority
        )
    )
    # The node's own resolved scope is the whole question the claim read asks, so
    # it is handed over as the read's candidate paths: a live claim can refuse
    # this dispatch only through a path the two both declare, and every other
    # claim's disposition is then never read. The predicate the read filters on is
    # the same repository-and-containment test the arbitration matches on, over
    # the same entries built by the same expansion, so no claim that could refuse
    # is dropped.
    candidate_paths = tuple(
        absolute
        for _repository, _path, absolute, _declared, _derived_from in candidate_scope
    )
    candidate_repositories = {
        repository
        for repository, _path, _absolute, _declared, _derived_from in candidate_scope
        if repository is not None
    }
    live_claims = (
        []
        if shadow_lineage
        else _repository_scope_claims(
            exclude_run_ids=(admitted_from,) if admission_launch else (),
            candidate_paths=candidate_paths,
            candidate_repositories=candidate_repositories,
        )
    )
    if shadow_lineage:
        peer_claims = []
    else:
        peer_claims = [
            claim for claim in live_claims if claim.repository in candidate_repositories
        ]

    competence = resolution.competence or _competence_verdict(
        resolution=resolution, project=project, repo=repo_root
    )
    if not competence["allowed"]:
        record_context_refusal(node=node, project=project, verdict=competence)
        raise CompetenceLimit(competence)

    fences = config.get("fences") or {}
    # A session's terminal backlog is recorded, not enforced. The rows are copied
    # onto the run as a field rather than referenced, so the record states which
    # runs the dispatch was made over instead of pointing at the ledger's state
    # in a later moment. The flag still parses and still says whether the
    # coordinator asked for an exception; it changes no gate, because there is
    # no gate left to change.
    # Admission continues work already chosen by the coordinator; computing
    # that session's backlog here would classify the fleet for every launch.
    backlog = (
        None
        if admission_launch
        else _recorded_backlog(
            project=project,
            session=session,
            config=config,
            requested=unreconciled_override,
            source="coordinator",
        )
    )
    # A waived plan review is narrower than the recorded backlog: it excuses
    # only the missing review of the plan this node builds, and is recorded with
    # the plan it waived so the exception names what was let through.
    plan_review_waiver = (
        {"requested": True, "plan": node.plan} if unreviewed_plan_override else None
    )

    budget_warnings: list[str] = []
    budget_fallback: dict[str, Any] | None = None
    requested_backend = resolution.requested_backend
    if check_budget:
        # Before the worktree, not after: a hold that had already cut a worktree
        # would leave write scope claimed by a node nobody is running.
        requested_backend_name = resolution.backend
        budget_config = config
        if route == "picker":
            budget_config = {
                **config,
                "budget": {
                    **(config.get("budget") or {}),
                    "resume_reserve_pct": 0,
                    "coordinator_reserve_pct": 0,
                },
            }
        verdict = _budget_verdict(
            project=project,
            root=ledger_root,
            config=budget_config,
            backend_name=resolution.backend,
            backend=resolution.backend_settings,
            purpose="dispatch",
            budget_state=budget_state,
            session=session,
        )
        if route == "picker":
            state = verdict.get("state") or {}
            from reckon import budget as budget_module

            ceiling = float(budget_module.policy(config)["utilisation_ceiling_pct"])
            utilisation = state.get("utilisation_pct")
            if (
                not verdict.get("credit_lift")
                and state.get("headroom") == "known"
                and isinstance(utilisation, (int, float))
                and utilisation >= ceiling
            ):
                verdict = {
                    **verdict,
                    "held": True,
                    "reason": (
                        f"backend {resolution.backend!r} is at {utilisation:g}% "
                        f"utilisation, at or above the provider's {ceiling:g}% "
                        "hard ceiling"
                    ),
                }
        budget_warnings.extend(verdict.get("warnings") or ())
        if verdict["held"]:
            # A wait is stored on the picked lane. Availability matters when
            # the request becomes ready, not while its upstream is unfinished.
            from reckon.crew import queue_admission

            waiting_on_upstream = False
            if not admission_launch:
                _refs, _projects, unmet = queue_admission.dispatch_waits(
                    node,
                    repo_root=repo_root,
                    project=project,
                    run_id=resolution.run_id,
                )
                waiting_on_upstream = bool(unmet)
            if waiting_on_upstream:
                verdict = {**verdict, "held": False}
            else:
                if (
                    caller_named_route
                    and not admission_launch
                    and not verdict.get("credit_lift")
                    and resolve_budget_fallback(
                        config,
                        node.role,
                        node.spec_level,
                        resolution.backend,
                        resolution.backend_settings,
                    )
                    is None
                ):
                    queued, position = queue_dispatch(
                        _queued_request_record(
                            run_id=resolution.run_id,
                            project=project,
                            node=node,
                            repo_root=repo_root,
                            session=session,
                            backend_name=resolution.backend,
                            launch_kind=resolution.launch,
                            lane_allowance=verdict,
                            dispatch_options=dispatch_options,
                            queued_at=_utc_now(),
                            local=resolution.backend
                            == str(config.get("local_backend") or ""),
                            backlog=backlog,
                        ),
                        max_requests=flight.queue_max_requests(config),
                    )
                    held = LaneHeld(
                        {
                            "detail": str(verdict.get("reason") or "metered lane held"),
                            "reason": str(verdict.get("reason") or "metered lane held"),
                        }
                    )
                    held.queued = {
                        "run_id": queued["run_id"],
                        "position": position,
                        "phase": "queued",
                        "backend": resolution.backend,
                        "reason": verdict.get("reason"),
                    }
                    _arrange_dispatch_follower(
                        project,
                        session,
                        resolution.launch,
                        watch_required=watch_required,
                        watch_override=watch_override,
                    )
                    raise held
                if route == "picker" and verdict.get("credit_lift"):
                    raise _actionable_budget_hold(verdict, config=budget_config)
                if route == "picker" and not admission_launch:
                    fallback_name = str(config.get("local_backend") or "")
                    substitute = (
                        (
                            fallback_name,
                            (config.get("backends") or {}).get(fallback_name),
                        )
                        if fallback_name and fallback_name != resolution.backend
                        else None
                    )
                elif route == "picker":
                    raise _actionable_budget_hold(verdict, config=budget_config)
                else:
                    substitute = resolve_budget_fallback(
                        config,
                        node.role,
                        node.spec_level,
                        resolution.backend,
                        resolution.backend_settings,
                    )
                if substitute is None:
                    raise _actionable_budget_hold(verdict, config=config)
                fallback_name, _fallback_settings = substitute
                # Re-resolve fully rather than patch the existing DispatchPlan, so
                # the fallback gets its own execution-fit, sandbox and write-path
                # checks instead of inheriting the held backend's. The caller's
                # own time_budget/write_paths are restored first because the held
                # backend's plan_dispatch call already defaulted them in place.
                node.time_budget = caller_time_budget
                node.write_paths = caller_write_paths
                resolution = plan_dispatch(
                    node=node,
                    config=config,
                    locked_decisions=locked_decisions,
                    peer_scopes=peer_scopes,
                    project=project,
                    repo=repo_root,
                    base=base,
                    execution_override=execution_override,
                    orchestrator_lane_reason=orchestrator_lane_reason,
                    authority=authority,
                    local=local
                    or (
                        route == "picker"
                        and fallback_name == str(config.get("local_backend") or "")
                    ),
                    run_id=resolution.run_id,
                    backend_override=fallback_name,
                    declared_backend=(
                        str(resolution.lane_declaration.get("backend") or "")
                        if resolution.lane_declaration is not None
                        else ""
                    ),
                    allow_unreviewed_plan=unreviewed_plan_override,
                    session=session,
                    route="deterministic"
                    if route == "picker"
                    else resolution.route_override,
                    picker_selection=None if route == "picker" else picker_selection,
                )
                resolution.requested_backend = requested_backend
                if not resolution.validation.ok:
                    raise CrewError(
                        f"node is not dispatchable on budget fallback {fallback_name!r} — "
                        + "; ".join(
                            f"{finding['property']}: {finding['detail']}"
                            for finding in resolution.validation.findings
                        )
                    )
                fallback_verdict = _budget_verdict(
                    project=project,
                    root=ledger_root,
                    config=config,
                    backend_name=resolution.backend,
                    backend=resolution.backend_settings,
                    purpose="dispatch",
                    budget_state=budget_state,
                    session=session,
                )
                budget_warnings.extend(fallback_verdict.get("warnings") or ())
                if fallback_verdict["held"]:
                    # No fallback-of-fallback chain: a declared fallback is a single
                    # named substitute, not a search, so a held fallback refuses on
                    # its own verdict rather than guessing a third lane.
                    raise _actionable_budget_hold(fallback_verdict, config=config)
                budget_fallback = {
                    "requested_backend": requested_backend_name,
                    "used_backend": resolution.backend,
                    "hold": verdict,
                }
                if route == "picker":
                    dispatch_options["route_fallback"] = {
                        "from_backend": requested_backend_name,
                        "to_backend": resolution.backend,
                        "reason": str(verdict.get("reason") or "metered lane held"),
                    }

    backend_name = resolution.backend
    backend = resolution.backend_settings
    launch_kind = resolution.launch
    run_id = resolution.run_id
    if admission_launch and admitted_from and run_id != admitted_from:
        raise CrewError(
            f"admitted request {admitted_from!r} resolved a different run id {run_id!r}"
        )
    lane_allowance = resolution.lane_allowance or {}
    local_backend = str(config.get("local_backend") or "")
    is_local = backend_name == local_backend and bool(local_backend)

    # A node that already has a run — queued, being admitted, or in flight — is
    # answered rather than dispatched a second time. This is read before the
    # scope check and before any lane-specific branch, so a re-dispatch of a
    # queued node updates that one request in place instead of being refused
    # against the queued pointer's own write-path claim, and a node whose run is
    # admitted, starting or working is answered with that run and never has its
    # pointer touched. A queued copy is a re-queue on any lane; a run already
    # being admitted or working is answered only on the lane that queues, where
    # the request would otherwise become a second pointer.
    from reckon.crew import queue_admission

    existing: dict[str, Any] | None = None
    existing_phase = ""
    member_refuses = False
    queued_backend_name = backend_name
    if not shadow_lineage:
        existing = existing_node_run(
            project,
            plan=node.plan,
            section=node.section,
            node_id=node.id,
            include_handovers=not admission_launch,
        )
        if (
            admission_launch
            and existing is not None
            and str(existing.get("run_id")) == admitted_from
        ):
            existing = None
        existing_phase = (
            str(existing.get("phase") or "") if existing is not None else ""
        )
        # A caller that names a member routes the node into that member's durable
        # session, whose own in-flight run refuses a second dispatch there. That
        # refusal outranks the existing-run answer for a run that is admitted or
        # working, so the caller hears the member is busy rather than the lane.
        # A queued copy is still updated in place, where no member refusal applies.
        member_refuses = bool(member) and existing_phase != queue_admission.QUEUED_PHASE
        # A request already queued stays on the lane that queued it, which is the
        # lane whose admission launches it. This dispatch's resolved backend may
        # be a different lane — a re-dispatch naming one, or a lift that makes it
        # eligible — and recording that lane on the waiting request strands it:
        # the admission pass keeps only the requests whose backend equals the lane
        # it drains, so nothing would ever launch it. The lane already on the
        # waiting request is kept for the re-queue.
        queued_backend_name = (
            str(existing.get("backend") or "").strip()
            if existing_phase == queue_admission.QUEUED_PHASE
            else ""
        ) or backend_name

    # A node whose waits have not landed is stored and held whatever the lane
    # state, and ahead of every branch that is taken for a lane's sake: a node
    # whose upstream has not landed cannot run at any lane occupancy, so a lane
    # with room must not launch it. The waits are read only for a node that has
    # no run at all, or one whose run is still only a stored request — a run
    # that is admitted, starting or working is running, and its pointer is the
    # record of a launch that happened, so reading its waits and rewriting that
    # record would take a live run off its lane. The refs are read through the
    # same readiness the admission pass re-reads and recorded by the same writer
    # that pass reads back, so a node is held here for exactly the reason the
    # pass would hold it.
    stored_request = existing is None or existing_phase in (
        queue_admission.QUEUED_PHASE,
        *queue_admission.WAITING_UPSTREAM_PHASES,
    )
    validated_refs: list[str] = []
    wait_projects: dict[str, str] = {}
    waiting: list[str] = []
    wait_readiness_cache = readiness_cache if readiness_cache is not None else {}
    if stored_request and not member_refuses:
        validated_refs, wait_projects, waiting = queue_admission.dispatch_waits(
            node,
            repo_root=repo_root,
            project=project,
            run_id=str((existing or {}).get("run_id") or run_id),
            readiness_cache=wait_readiness_cache,
        )
    if waiting:
        blockers = queue_admission.dispatch_wait_blockers(
            node,
            repo_root=repo_root,
            project=project,
            refs=validated_refs,
            readiness_cache=wait_readiness_cache,
        )
        queued, position, phase = _queue_validated_waits(
            _queued_request_record(
                run_id=run_id,
                project=project,
                node=node,
                repo_root=repo_root,
                session=session,
                backend_name=backend_name,
                launch_kind=launch_kind,
                lane_allowance=lane_allowance,
                dispatch_options=dispatch_options,
                queued_at=_utc_now(),
                admitted_from=admitted_from,
                local=is_local,
                backlog=backlog,
            ),
            refs=validated_refs,
            wait_projects=wait_projects,
            max_requests=flight.queue_max_requests(config),
        )
        hold = queue_admission.section_hold(
            waiting,
            project=project,
            docs_dir=Path(str(repo_root)) / "docs",
            cache=wait_readiness_cache,
        )
        queue_admission.record_section_hold(
            str(queued["run_id"]),
            hold,
            verdict=queue_admission.section_hold_verdict(hold),
        )
        held = LaneHeld({"detail": hold["detail"], "reason": hold["reason"]})
        held.queued = {
            "run_id": str(queued["run_id"]),
            "position": position,
            # A request the writer could not move off ``queued`` is reported
            # as the phase it really holds, so the answer never claims a wait
            # the pointer does not carry.
            "phase": phase,
            "waits": list(validated_refs),
            "blockers": blockers,
            "hold": hold["names"],
            "reason": hold["reason"],
        }
        if held_selection is not None:
            # The hold that offered the local lane is kept in the outcome
            # history, marked queued with the run it became, so a reader of the
            # holds file reads Jev's answer and where it landed.
            from reckon.crew.picker.outcomes import record_picker_hold

            record_picker_hold(
                project=project,
                node=node.id,
                plan=node.plan,
                selection=held_selection,
                reason=_picker_refusal_reasons(held_selection),
                run_id=str(queued["run_id"]),
                queued=True,
            )
        _arrange_dispatch_follower(
            project,
            session,
            launch_kind,
            watch_required=watch_required,
            watch_override=watch_override,
        )
        raise held

    # A node that already has a run — queued, being admitted, or in flight — is
    # answered here rather than dispatched a second time, from the resolution
    # above and before any worktree is made. A queued copy is a re-queue on any
    # lane; a run already being admitted or working is answered only on the lane
    # that queues, where the request would otherwise become a second pointer.
    if (
        not shadow_lineage
        and existing is not None
        and not member_refuses
        and (existing_phase == queue_admission.QUEUED_PHASE or is_local)
    ):
        held = _answer_existing_node_run(
            existing,
            record=_queued_request_record(
                run_id=run_id,
                project=project,
                node=node,
                repo_root=repo_root,
                session=session,
                backend_name=queued_backend_name,
                launch_kind=launch_kind,
                lane_allowance=lane_allowance,
                dispatch_options=dispatch_options,
                queued_at=_utc_now(),
                local=queued_backend_name == local_backend,
                backlog=backlog,
            ),
            backend=backend,
            lane_allowance=lane_allowance,
            max_requests=flight.queue_max_requests(config),
        )
        if existing_phase == "queued":
            _arrange_dispatch_follower(
                project,
                session,
                launch_kind,
                watch_required=watch_required,
                watch_override=watch_override,
            )
        raise held

    # The lane gate is read before anything is created: a paused gate, one that
    # cannot be answered, or a declared path that differs from the lane
    # document's published one holds the dispatch here, so no pointer and no
    # worktree is left behind for a worker nobody may launch. Re-reading the
    # resolved backend here catches a pause added after plan_dispatch; both
    # ``--local`` and an explicit ``--backend`` reach that resolved backend.
    lane_gate = _dispatch_lane_gate(backend)
    resolution.lane_gate = lane_gate
    if lane_gate.get("state") in _LANE_GATE_WAITING_STATES:
        raise LanePaused(lane_gate)

    # The router's allowance is checked before creating a worktree. A local
    # hold records the request as queued; other holds remain transient. The
    # allowance rides the exception so the caller reports the router's reason.
    if lane_allowance.get("held"):
        if is_local and not shadow_lineage:
            queued, position = queue_dispatch(
                _queued_request_record(
                    run_id=run_id,
                    project=project,
                    node=node,
                    repo_root=repo_root,
                    session=session,
                    backend_name=backend_name,
                    launch_kind=launch_kind,
                    lane_allowance=lane_allowance,
                    dispatch_options=dispatch_options,
                    queued_at=_utc_now(),
                    admitted_from=admitted_from,
                    backlog=backlog,
                ),
                max_requests=flight.queue_max_requests(config),
            )
            from reckon.crew import queue_admission

            # A node whose waits are unmet is held on them above, whatever the
            # lane state, so what this branch stores is a request the lane had
            # no room for and it carries no wait.
            held = LaneHeld(lane_allowance)
            held.queued = {
                "run_id": queued["run_id"],
                "position": position,
                "phase": str(queued.get("phase") or queue_admission.QUEUED_PHASE),
                "waits": [],
                "reason": lane_allowance.get("reason"),
            }
            if held_selection is not None:
                # The hold that offered the local lane is kept in the outcome
                # history, marked queued with the run it became, so a reader of
                # the holds file reads Jev's answer and where it landed.
                from reckon.crew.picker.outcomes import record_picker_hold

                record_picker_hold(
                    project=project,
                    node=node.id,
                    plan=node.plan,
                    selection=held_selection,
                    reason=_picker_refusal_reasons(held_selection),
                    run_id=str(queued["run_id"]),
                    queued=True,
                )
            _arrange_dispatch_follower(
                project,
                session,
                launch_kind,
                watch_required=watch_required,
                watch_override=watch_override,
            )
            raise held
        raise LaneHeld(lane_allowance)

    # A dispatch that found room on its lane yields the slot to its own session's
    # oldest waiting request for that lane, launching that request through the
    # admission pass's own path and queueing itself behind it. A session's new
    # work must not keep taking the slots its oldest queued request needs. A
    # request the pass would hold is skipped by the selector, so a request that
    # can never be admitted never stops the session launching; the lane slot an
    # admission pass is itself spending is left to the pass, which already owns
    # the ordering.
    if is_local and not shadow_lineage and not admission_launch:
        moment = datetime.now(UTC)
        older_request = _session_older_admissible_request(
            session=session, backend_name=backend_name, moment=moment
        )
        if older_request is not None:
            launched = _launch_yielded_queued_request(
                older_request, launcher=launcher, moment=moment
            )
            if launched is not None:
                queued, position = queue_dispatch(
                    _queued_request_record(
                        run_id=run_id,
                        project=project,
                        node=node,
                        repo_root=repo_root,
                        session=session,
                        backend_name=backend_name,
                        launch_kind=launch_kind,
                        lane_allowance=lane_allowance,
                        dispatch_options=dispatch_options,
                        queued_at=_utc_now(),
                        backlog=backlog,
                    ),
                    max_requests=flight.queue_max_requests(config),
                )
                held = LaneHeld(
                    {
                        "detail": (
                            "yielded this lane slot to queued request "
                            f"{launched.get('run_id')!r}"
                        ),
                        "reason": "yielded to this session's older queued request",
                    }
                )
                from reckon.crew import queue_admission

                # The waits are answered above, whatever the lane state, so a
                # request queued here waits only on the slot it yielded.
                held.queued = {
                    "run_id": str(queued["run_id"]),
                    "position": position,
                    "phase": str(queued.get("phase") or queue_admission.QUEUED_PHASE),
                    "waits": [],
                    "reason": "waiting behind this session's older queued request",
                }
                _arrange_dispatch_follower(
                    project,
                    session,
                    launch_kind,
                    watch_required=watch_required,
                    watch_override=watch_override,
                )
                raise held

    # A cli worker launches inside the fence, and the fence is bubblewrap over a
    # user namespace. A host with neither cannot seal a worker's writes, so the
    # dispatch is refused before any worktree, pointer or run directory exists
    # rather than launched unfenced: a fence that disappears silently still
    # reports a protection it does not have. ``--no-fence REASON`` is the one
    # deliberate way through, and the reason is recorded on the run, its
    # composed plan and its ledger row.
    fence_waiver = str(no_fence_reason).strip()
    compose_fence = launch_kind == "cli" and FENCE_WORKERS
    if compose_fence and not fence_waiver:
        capability_problem = _backends.fence_capability_problem()
        if capability_problem is not None:
            missing, detail = capability_problem
            raise CrewError(
                format_refusal(
                    "D22",
                    f"refusing to dispatch run {run_id!r}: {detail} "
                    f"(missing capability: {missing}). The fence is what keeps "
                    "a worker out of the operator's home and every other run's "
                    "worktree, so a dispatch that cannot build one is refused "
                    "rather than launched unprotected; pass --no-fence REASON to "
                    "launch unfenced and record why",
                )
            )
    if fence_waiver:
        compose_fence = False

    # The pace this dispatch is judged against, composed once here — before
    # anything is created — and reused on the record below, so the bookend
    # reserve refuses against the very reading the record carries rather than a
    # second one taken a moment later that could differ from it.
    from reckon import budget as budget_module

    pace_record = budget_module.pace_row(
        config,
        project=project,
        lane=backend_name,
        node=node.id,
        score=resolution.open_endedness,
        root=ledger_root,
        hold=None if budget_fallback is None else budget_fallback["hold"],
    )
    if check_budget and route != "picker":
        _refuse_against_the_bookend_reserve(
            config=config, role=node.role, pace_record=pace_record
        )

    # A run id that already has a live worker is refused here, before this
    # dispatch writes a claim, a run directory or a worktree: every one of those
    # would overwrite or release something the running worker holds.
    _refuse_a_live_worker_before_the_worktree(run_id)

    directory = run_dir(run_id)
    # A brief is authority text, so its durable copy is taken with the run's id
    # rather than at launch: a later reader opening the pointer finds the same
    # bytes the worker read, and a refusal below unwinds the directory that
    # holds them. The digest was taken in plan_dispatch, where the dry run
    # reaches it too.
    # The domain-rules block this brief carries is composed once and handed to
    # both the stored copy and the record, so the text the worker reads, the
    # copy the pointer names and the digest a promotion cites are one document.
    brief_block = domain_rules_block(project=project, node=node)
    if node.brief.strip():
        node.brief_path = str(_store_brief(directory, node.brief, block=brief_block))
    brief = _brief_record(node, project=project)
    # The declared paths are this run's from the moment its id exists, so the
    # claim goes out here, before the refusals, reads and watcher arming below,
    # any of which can take seconds. A dispatch that published only once the
    # launch was composed held its paths invisibly for that whole span, so a
    # second dispatch arriving inside it read no claim and launched a duplicate
    # worker over the first. The guard opened under this block gives the claim
    # back if any refusal below is reached. See _publish_launch_claim.
    effective_member = member or _disposable_member_id(run_id)
    agent = _stamp_agent_display(
        _agent_configuration(backend_name, launch_kind, backend), backend
    )
    if resolution.local:
        agent["local"] = True
    # An admitted request already has a pointer. Save it so a refusal can
    # restore the waiting phase atomically after this starting claim replaces it.
    request_pointer = read_pointer(run_id) if admission_launch else None
    claim_published = not shadow_lineage
    # The moment this dispatch's claim is registered, held so the admission
    # checks below can order it against a peer still composing its own claim.
    claim_registered_at = _utc_now() if claim_published else ""
    if claim_published:
        _publish_launch_claim(
            run_id,
            node=node,
            project=project,
            repo=repo_root,
            session=session,
            authority=resolution.authority,
            member=effective_member,
            backend=backend_name,
            launch=launch_kind,
            agent=agent,
            session_id=None,
            brief=brief,
            registered_at=claim_registered_at,
        )
    with (
        _request_claim_restored_on_refusal(run_id, request_pointer)
        if admission_launch
        else _claim_released_on_refusal(run_id, claim_published)
    ):
        # A dispatch that would exceed a resource bound — the placement's
        # admitted partition cores or the login memory slice — refuses before
        # anything is created or spawned. A fallback backend resolved above
        # gets the same reading as a directly chosen one, so a held lane never
        # reroutes onto an exhausted resource.
        _refuse_over_concurrency_ceiling(
            backend_name, backend, project, exclude_run_ids=(run_id,)
        )
        explicitly_named_peers = set() if shadow_lineage else set(node.peer_scopes)
        peers = (
            {} if shadow_lineage else _merge_peer_scopes(peer_claims, node.peer_scopes)
        )
        peers = _peer_scopes_without_shared_landing_paths(
            peers,
            node=node,
            project=project,
            repo=repo_root,
            authority=authority,
        )
        node.peer_scopes = peers

        reap_idle_session_members(
            project,
            root=ledger_root,
            idle_window=str(
                fences.get("member_idle_window") or DEFAULT_MEMBER_IDLE_WINDOW
            ),
        )
        named_member = bool(member)
        # An unnamed dispatch is disposable: it carries a per-run identity instead
        # of the dispatching session's shared one, and it gets no roster row. So
        # two unnamed dispatches of one coordinator — every reflex review among
        # them — are never serialised against each other, and one run in flight
        # cannot refuse an unrelated task with `member-in-flight`. A named member
        # remains a deliberate route to a durable worker, so it keeps the roster
        # lookup, the registration check and the refusal. The identity is minted with the
        # claim above, so the roster below is only ever asked about a named one.
        roster_member = (
            ledger.member(project, effective_member, root=ledger_root)
            if named_member
            else None
        )
        if named_member and roster_member is None:
            raise CrewError(
                format_refusal(
                    "D14",
                    f"project {project!r} has no crew member {member!r}; register it "
                    "with `reckon crew member add` before dispatching to it",
                )
            )
        live_pointers = [
            pointer
            for pointer in list_live(project=project)
            if str(pointer.get("run_id") or "") != run_id
        ]
        if roster_member is not None:
            for pointer in live_pointers:
                if pointer.get("member") == effective_member:
                    refuse_member_in_flight(effective_member, pointer)
        disregarded_claims: list[str] = []
        accepted_directory_claims: list[dict[str, Any]] = []
        # The scope check judges one pointer at a time, so it cannot tell a
        # dispatch cut from a stopped holder's chain tip from one cut from the
        # integration branch unless it is told the revision this dispatch builds
        # on. Resolve it once here and hand it to both checks; the admissions
        # they grant land on the pointers after the second one passes.
        dispatch_base = _resolve_commit(repo_root, base)
        stacked_admissions: list[dict[str, Any]] = []
        if shadow_lineage:
            adjacent_peers = []
        else:
            # A declared path is claimed whole, not piecemeal: a figure topic
            # directory is claimed as a tree, so any live claim that contains or is
            # contained by a candidate is refused with the owner named — never a
            # shared workspace, because a figure is replaced wholesale and a merged
            # half is never correct. The walk is path-based only, so a topic with
            # no files on disk yet binds exactly like one that does.
            _raise_repository_scope_conflict(
                node,
                project=project,
                repo=repo_root,
                authority=authority,
                claims=live_claims,
                disregarded=disregarded_claims,
                # The acceptance sink is passed only when the flag is given, so
                # a call that carries no directory claim keeps the walk's own
                # signature rather than threading an unused sink through it.
                **_directory_claim_acceptance_kwargs(
                    accept_directory_claim, accepted_directory_claims
                ),
                base_revision=dispatch_base,
                stacked=stacked_admissions,
                own_run_id=run_id,
                own_registered_at=claim_registered_at,
            )
            adjacent_peers = _adjacent_live_peers(
                node,
                project=project,
                repo=repo_root,
                explicitly_named=explicitly_named_peers,
                exclude_run_ids=(run_id,),
            )
        committed_runs = ledger.runs(project, root=ledger_root)
        session_resolution = (
            _task_session_resolution(
                node,
                project,
                committed_runs=committed_runs,
                live_pointers=live_pointers,
                harness=_backends.dialect_for(backend).name
                if launch_kind == "cli"
                else "",
            )
            if backend.get("session_reuse")
            else {"session_id": None, "withheld": None}
        )
        reuse_session = session_resolution["session_id"]
        prior_node_runs = [
            item
            for item in committed_runs
            if str(item.get("node") or "") == node.id
            and not (
                isinstance(item.get("lineage"), Mapping)
                and item["lineage"].get("kind") == "shadow"
            )
        ]
        lineage = shadow_lineage
        attempt = 1
        primary = None
        if shadow_lineage and paired_masks is None:
            primary = next(
                (
                    item
                    for item in committed_runs
                    if str(item.get("run_id") or "")
                    == str(shadow_lineage.get("primary_run_id") or "")
                ),
                None,
            )
            if primary is None:
                raise CrewError(
                    format_refusal(
                        "D20", "shadow lineage names no committed primary run"
                    )
                )
            attempt = int(primary.get("attempt") or 1)
        elif prior_node_runs and not fresh_review:
            previous = prior_node_runs[-1]
            previous_lineage = previous.get("lineage") or {}
            previous_attempt = previous.get("attempt") or previous_lineage.get(
                "attempt"
            )
            try:
                attempt = int(previous_attempt) + 1
            except (TypeError, ValueError):
                attempt = len(prior_node_runs) + 1
            lineage = {
                "kind": "redispatch",
                "attempt": attempt,
                "root_run_id": previous_lineage.get("root_run_id")
                or str(prior_node_runs[0].get("run_id") or ""),
                "previous_run_id": str(previous.get("run_id") or ""),
            }

        # The attempt's identity is written down here, with the claim, rather
        # than only after the launch is composed. Everything from this point to
        # the supervisor — the worktree, the prompt, the composed argv — takes
        # seconds, and a launcher that ends inside that span runs no handler and
        # writes nothing else, so the run would keep a claim and no attempt at
        # all. The marker is what makes such a run name the attempt it stopped
        # on instead of reading as one that never started.
        attempt_marker = {
            "run_id": run_id,
            "attempt": attempt,
            "attempt_kind": (
                "shadow" if shadow_lineage else "redispatch" if lineage else "dispatch"
            ),
            "attempt_started_at": _utc_now(),
        }
        # A solo shadow launch publishes no claim, and the marker write refuses
        # to create its run directory, so nothing else would put that directory
        # there. Without it a launch that dies mid-composition leaves no record
        # of the attempt it was making.
        if shadow_lineage is not None and paired_masks is None:
            directory.mkdir(parents=True, exist_ok=True)
        _supervisor_write(directory / ATTEMPT_RECORD_NAME, attempt_marker)
        dispatch_watch, session_delivery, watch_override = _arrange_dispatch_follower(
            project,
            session,
            launch_kind,
            watch_required=watch_required,
            watch_override=watch_override,
        )
        released_follower_warning: str | None = None
        if watch_required and not watch_override:
            # The watcher requirement is answered by the process, read from the
            # watcher's own state — never by a session's follower, which is how a
            # project with no watcher process at all kept admitting dispatches.
            # Whether this session hears what the producer writes is a separate
            # fact, and the only one that decides if the finished run gets
            # noticed. Both are judged here, before a worktree exists, and one
            # refusal names every unmet condition so the caller reaches the fix
            # in one dispatch rather than one condition per round trip. A
            # released registration proceeds with a re-arm warning rather than
            # a refusal.
            released_follower_warning = _watcher_delivery_admission(
                project,
                dispatch_watch,
                session=session,
                launch_kind=launch_kind,
                delivery=session_delivery,
            )
        watcher_waiver = (
            {
                "requested": True,
                "arming_line": dispatch_watch["arming_line"],
                "attach_line": dispatch_watch["attach_line"],
                "watcher_live": bool(dispatch_watch["watcher_live"]),
                "session_attached": bool(dispatch_watch["session_attached"]),
            }
            if watch_override
            else None
        )
        gates = config.get("gates") or {}
        suite_command = str(gates.get("suite_command") or "").strip() or None
        declared_census = gates.get("census_tests") or ()
        if isinstance(declared_census, (str, bytes)):
            declared_census = [declared_census]
        census_tests = [
            str(entry).strip() for entry in declared_census if str(entry or "").strip()
        ]
        wave_id = _resolved_wave_id(project, session, wave)

    return _launch_dispatch(
        accept_directory_claim=accept_directory_claim,
        accepted_directory_claims=accepted_directory_claims,
        adjacent_peers=adjacent_peers,
        admission_launch=admission_launch,
        admitted_from=admitted_from,
        agent=agent,
        attempt=attempt,
        attempt_marker=attempt_marker,
        authority=authority,
        backend=backend,
        backend_name=backend_name,
        backend_override=backend_override,
        base=base,
        brief=brief,
        budget_fallback=budget_fallback,
        budget_warnings=budget_warnings,
        candidate_paths=candidate_paths,
        candidate_repositories=candidate_repositories,
        census_tests=census_tests,
        claim_registered_at=claim_registered_at,
        comment=comment,
        competence=competence,
        compose_fence=compose_fence,
        config=config,
        default_backend_override=default_backend_override,
        deferred_shadow_selection=deferred_shadow_selection,
        directory=directory,
        dispatch_base=dispatch_base,
        dispatch_options=dispatch_options,
        disregarded_claims=disregarded_claims,
        due=due,
        effective_member=effective_member,
        fence_waiver=fence_waiver,
        fences=fences,
        held_selection=held_selection,
        launch_kind=launch_kind,
        launcher=launcher,
        ledger_root=ledger_root,
        lineage=lineage,
        local=local,
        node=node,
        orchestrator_assessment=orchestrator_assessment,
        pace_record=pace_record,
        paired_masks=paired_masks,
        paired_shadow_run_id=paired_shadow_run_id,
        peers=peers,
        picker_selection=picker_selection,
        plan_review_waiver=plan_review_waiver,
        primary=primary,
        project=project,
        released_follower_warning=released_follower_warning,
        repairs=repairs,
        repo_root=repo_root,
        request_pointer=request_pointer,
        resolution=resolution,
        reuse_session=reuse_session,
        run_id=run_id,
        session=session,
        session_delivery=session_delivery,
        session_resolution=session_resolution,
        shadow_lineage=shadow_lineage,
        stacked_admissions=stacked_admissions,
        suite_command=suite_command,
        urgency=urgency,
        backlog=backlog,
        watcher_waiver=watcher_waiver,
        wave_id=wave_id,
        worktree_identity=worktree_identity,
    )
