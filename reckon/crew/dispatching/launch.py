"""Compose the worktree, worker prompt and launch receipt."""

from __future__ import annotations

from . import _unbound_dispatch_call
from ._imports import (
    ATTEMPT_RECORD_NAME,
    LAUNCH_IDENTITY_STEP,
    LAUNCH_WORKER_STEP,
    SUPERVISOR_SPEC_NAME,
    UTC,
    Any,
    CrewError,
    Mapping,
    Path,
    _attach_shadow_picker_selection,
    _backends,
    _boundary_snapshot_roots,
    _brief_text,
    _channel_root,
    _claim_node_dispatch,
    _compose_dispatch_prompt,
    _coordinator_accounting,
    _create_worktree,
    _current_host_facts,
    _directory_claim_acceptance_kwargs,
    _directory_claim_acceptance_line,
    _dispatch_session_absence,
    _fence_write_roots,
    _launched_prior_session,
    _manifest_mtime_ns,
    _mutate_pointer,
    _NodeDispatchClaim,
    _persisted_worker_environment,
    _picker_fallback,
    _plan_composed_the_fence,
    _plan_impl_at_dispatch,
    _prepare_attempt_records,
    _process_start_time,
    _raise_repository_scope_conflict,
    _refuse_a_discarded_launch,
    _release_launch_claim,
    _remove_worktree,
    _repository_scope_claims,
    _repository_tree_snapshot,
    _require_fleet_gate_open,
    _shadow_mask_paths,
    _signal_process_group,
    _start_shadow_picker_selection,
    _start_supervisor,
    _supervisor_spec,
    _supervisor_write,
    _unwire_peer_channels,
    _utc_now,
    _watch_arming_line,
    _watch_attach_line,
    _wire_peer_channels,
    _with_anchored_edit_instruction,
    _worker_host_line,
    _worker_runtime_environment,
    _write_boundary_tree_snapshot,
    _write_json,
    advance_launch_step,
    brief_reduction,
    consume_context_refusal,
    contextlib,
    datetime,
    flight,
    format_refusal,
    harness_command_index,
    placement_job_id,
    pointer_path,
    preflight_launch_command,
    rasterise_svg,
    read_dispatch_detail,
    record_stacked_dispatch,
    refuse_a_released_launch,
    resolve_backend_placement,
    resolve_launch_executable,
    resolved_grace_multiple,
    run_dir,
    socket,
    sys,
    watch_state,
    worker_scratch_dir,
)

# The facade binds cyclic calls after all concerns have been imported.
_attempt_record_for = _unbound_dispatch_call
_overridden_hold_route_mode = _unbound_dispatch_call
_prompt_file_text = _unbound_dispatch_call
_recorded_capability = _unbound_dispatch_call
_resolved_suite_gate = _unbound_dispatch_call
_suite_gate_prompt_body_for = _unbound_dispatch_call

_FACADE_BINDINGS = (
    "_attempt_record_for",
    "_overridden_hold_route_mode",
    "_prompt_file_text",
    "_recorded_capability",
    "_resolved_suite_gate",
    "_suite_gate_prompt_body_for",
)


def _launch_dispatch(
    *,
    accept_directory_claim,
    accepted_directory_claims,
    adjacent_peers,
    admission_launch,
    admitted_from,
    agent,
    attempt,
    attempt_marker,
    authority,
    backend,
    backend_name,
    backend_override,
    base,
    brief,
    budget_fallback,
    budget_warnings,
    candidate_paths,
    candidate_repositories,
    census_tests,
    claim_registered_at,
    comment,
    competence,
    compose_fence,
    config,
    default_backend_override,
    deferred_shadow_selection,
    directory,
    dispatch_base,
    dispatch_options,
    disregarded_claims,
    due,
    effective_member,
    fence_waiver,
    fences,
    held_selection,
    launch_kind,
    launcher,
    ledger_root,
    lineage,
    local,
    node,
    orchestrator_assessment,
    pace_record,
    paired_masks,
    paired_shadow_run_id,
    peers,
    picker_selection,
    plan_review_waiver,
    primary,
    project,
    released_follower_warning,
    repairs,
    repo_root,
    request_pointer,
    resolution,
    reuse_session,
    run_id,
    session,
    session_delivery,
    session_resolution,
    shadow_lineage,
    stacked_admissions,
    suite_command,
    urgency,
    waiver,
    watcher_waiver,
    wave_id,
    worktree_identity,
) -> dict[str, Any]:
    """Compose and publish a launch after dispatch preflight has passed."""
    worktree: dict[str, Any] | None = None
    reservation = None
    spawned_pid: int | None = None
    spawned_start_time: str | None = None
    wired_peer_run_ids: list[str] = []
    node_claim: _NodeDispatchClaim | None = None
    try:
        # The claim over this node's worktree path is taken here, immediately
        # before the worktree, so a dispatch that loses it refuses before
        # touching the path. It is held until the launch is decided, and
        # released only after a refusal has unwound — the unwind removes the
        # worktree, and a next dispatch taking the claim early would race that
        # removal against its own creation.
        node_claim = _claim_node_dispatch(
            project=project,
            worktree_identity=worktree_identity,
            node_id=node.id,
            run_id=run_id,
            session=session,
        )
        # A relaunch of a dispatch here has never launched takes no reservation;
        # a launch of an existing run id takes one for the attempt it starts,
        # before the worktree is touched, and launches exactly that attempt.
        # An in-harness relaunch takes none and is refused while a CLI launch
        # of the run holds one, because nothing on disk records its own end.
        from reckon.crew.dispatch_launch import (
            record_supervisor as _record_supervisor,
        )
        from reckon.crew.dispatch_launch import (
            refuse_held_launch,
            reserve_launch_attempt,
            run_holds_an_attempt_record,
        )
        from reckon.crew.dispatch_launch import (
            release_launch_reservation as _release_launch_reservation,
        )

        if run_holds_an_attempt_record(run_dir(run_id)):
            if launch_kind == "cli":
                reservation = reserve_launch_attempt(
                    run_dir(run_id), run_id, kind="dispatch"
                )
                attempt = reservation.attempt
                if lineage:
                    lineage["attempt"] = attempt
                # The reserved attempt is the attempt this launch runs, so the
                # marker is rewritten to name it. A marker left naming an older
                # attempt would duplicate that attempt's number while a receipt
                # for it already exists, and the run would read as one that
                # launched an attempt it did not.
                if attempt != attempt_marker["attempt"]:
                    attempt_marker["attempt"] = attempt
                    _supervisor_write(directory / ATTEMPT_RECORD_NAME, attempt_marker)
            else:
                refuse_held_launch(run_id, run_dir(run_id))
        rendered_images = []
        pending_pngs = []
        for index, image in enumerate(node.images):
            source = Path(image)
            if source.suffix.lower() != ".svg":
                rendered_images.append(image)
                continue
            if not source.is_absolute():
                source = repo_root / source
            try:
                png = rasterise_svg(source, 1400)
            except Exception as exc:
                raise CrewError(f"cannot render SVG image {source}: {exc}") from exc
            output = directory / f"image-{index}.png"
            pending_pngs.append((output, png))
            rendered_images.append(str(output))
        from reckon.crew import routing

        launch_token = routing.LAUNCH_RUN_ID.set(run_id)
        try:
            worktree = _create_worktree(repo_root, worktree_identity, node.id, base)
        finally:
            routing.LAUNCH_RUN_ID.reset(launch_token)
        directory.mkdir(parents=True, exist_ok=True)
        for output, png in pending_pngs:
            output.write_bytes(png)
        node.images = rendered_images
        working_directory = worktree["path"]
        if launch_kind == "cli":
            try:
                working_directory = _backends.launch_working_directory(
                    backend=backend,
                    worktree=worktree["path"],
                    manifest_path=node.manifest_path,
                )
            except _backends.BackendError as exc:
                raise CrewError(format_refusal("D22", str(exc))) from exc
        # The suite command is resolved here, before the prompt is composed and
        # before the record is written, so the command the worker is told to
        # measure is the one promotion will run against it. The worktree is the
        # tree the scoped paths are named within, which is why this waits for
        # it to exist.
        suite_command_resolved, suite_gate = _resolved_suite_gate(
            suite_command,
            node=node,
            worktree=worktree["path"],
            census_tests=census_tests,
        )
        # One read of the clock is both the attempt's recorded launch instant
        # and the instant its fence states, so the prompt and the record cannot
        # disagree about when this attempt started.
        attempt_started_at = _utc_now()
        dispatch_host = _current_host_facts()
        prompt = _compose_dispatch_prompt(
            node=node,
            project=project,
            authority=authority,
            backend=backend,
            repo_root=repo_root,
            run_directory=directory,
            worktree=worktree["path"],
            working_directory=working_directory,
            # The prompt carries the template and the per-tree rule beside the
            # resolution, so the worker can resolve it over each arm's tree
            # rather than run a file list its own change invalidates. The record
            # keeps the resolved command itself, which is the string promotion
            # substitutes when no tree answers what an arm holds. The base
            # revision is named with it because the worktree's head is that
            # revision at this moment, so this resolution is the baseline arm's
            # and a worker given only the command cannot tell which revision to
            # record it at.
            suite_command=(
                _suite_gate_prompt_body_for(
                    suite_command, suite_command_resolved, worktree
                )
                if suite_command_resolved
                else ""
            ),
            suite_gate=suite_gate,
            launch_instant=attempt_started_at,
            needs_help_after_failures=int(fences.get("needs_help_after_failures", 2)),
            # The multiple is read through its single owner, the same resolver
            # the watchdog and the resumed fence read, so the first attempt's
            # prompt and the watchdog agree on the instant the hard stop falls on
            # rather than one parsing the raw configuration and the other not.
            grace_multiple=resolved_grace_multiple(config),
            peer_scopes=peers,
            run_id=run_id,
            peer_channels={
                str(peer["node"]): {"run_id": str(peer["run_id"])}
                for peer in adjacent_peers
            },
            peer_channel_path=str(_channel_root(run_id)),
            host_line=_worker_host_line(dispatch_host, directory),
            brief=_brief_text(node, project=project),
        )
        # The anchored arm's other half is its instruction: the argv makes the
        # tool callable and this is what makes the worker reach for it. Keyed
        # on the resolved backend, so a lane that did not receive the server
        # never reads an instruction naming a tool it does not have.
        prompt = _with_anchored_edit_instruction(prompt, backend)
        if shadow_lineage:
            prompt += (
                "\n\nSHADOW RUN — produce the named evidence without committing. "
                "The durable deliverable is the worktree patch retained at completion; "
                "this run is never merged.\n"
            )
        prompt_path = directory / "prompt.txt"
        prompt_path.write_text(prompt)
        log_path = directory / "stream.jsonl"
        stderr_path = directory / "stderr.log"
        final_path = directory / "final.txt"
        coordinator = _coordinator_accounting(session)
        node_definition = node.as_dict()
        node_definition["requested_backend"] = resolution.requested_backend
        node_definition["lane_declaration"] = resolution.lane_declaration
        node_definition["lane_reading"] = resolution.lane_reading
        # The token budget is resolved here, at dispatch, so the run record is
        # authoritative and a later config edit cannot silently re-charge a run
        # that launched under another allowance. It rides the node block beside
        # time_budget, which is where recovery reads both allowances back.
        node_definition["token_budget"] = resolution.token_budget
        # Promotion deliberately rebuilds the committed row from selected live
        # fields. The authored node definition is one of those durable fields,
        # so attribution lives there as well as at the pointer's top level.
        node_definition["coordinator"] = coordinator
        # The requirement Jev weighed, recorded on the run so a later reader can
        # condition history on difficulty. ``capability`` is the section
        # routing's own resolution and stands on every route, including one
        # whose lane an explicit --backend fixed; ``orchestrator_assessment``
        # is the coordinator's own reading when one was given, stored beside it
        # rather than in place of it.
        node_definition["capability"] = _recorded_capability(
            resolution,
            config=config,
            node=node,
            project=project,
            repo_root=repo_root,
            authority=authority,
        )
        # A brief cut after a context refusal is recorded here, in the node block
        # promotion rebuilds the committed row from, so the reduction is visible
        # on the landed run and not only on the dispatch that accepted it.
        reduction = brief_reduction(node=node, project=project, verdict=competence)
        if reduction is not None:
            node_definition["brief_reduction"] = reduction
        if orchestrator_assessment:
            node_definition["orchestrator_assessment"] = dict(orchestrator_assessment)

        # The pace this dispatch was judged against, composed by the module that
        # owns every figure in it and carried on the record the run already
        # writes, which is where its evidence lives. Promotion rebuilds the
        # committed row from selected fields rather than whole, and carries this
        # one across by reading it back from the run's own pointer while that
        # pointer is still open, so a week of dispatch decisions replays from
        # those rows alone rather than from the streams they were read out of.
        # The row reports the reading's age and its source, so a row that paced
        # a dispatch on stale evidence says so itself, and a lane declaring no
        # wallet records that no group paced it rather than a wallet nothing
        # read. The row is composed once, above the refusals, so the reading
        # this record carries is the same one the bookend reserve judged.
        record: dict[str, Any] = {
            "run_id": run_id,
            "project": project,
            "repo": str(repo_root),
            "authority": resolution.authority,
            "session": session,
            "wave": wave_id,
            "coordinator": coordinator,
            "node": node_definition,
            "due": due,
            "urgency": urgency,
            "brief": brief,
            # The owner a brief implementing run named when it was dispatched.
            # Landing refuses to promote such a run without one of the two
            # discharges, so the value chosen at dispatch is carried onto the
            # pointer promotion reads rather than asked for again after the
            # work is done. A run that needed neither records the empty value
            # the dispatch was given, and landing falls back to its own flags.
            "plan_link": dispatch_options.get("plan_link"),
            "unplanned_reason": dispatch_options.get("unplanned_reason"),
            "role": node.role,
            "backend": backend_name,
            # The lane and model key the resolved backend serves, written beside
            # ``backend`` so a reader of the pointer carries the catalogue pair
            # without re-resolving a backend name that may since have retired.
            "lane": backend.get("lane"),
            "model_key": backend.get("model_key"),
            # The queued request an admission pass launched this run from, on
            # the pointer from its first write: the fleet fold reads the queued
            # pointer's disappearance as this run continuing it, never as a
            # withdrawal beside an unrelated arrival.
            "admitted_from": admitted_from or None,
            "requested_backend": resolution.requested_backend,
            "lane_declaration": resolution.lane_declaration,
            "lane_reading": resolution.lane_reading,
            # What the section's own record contributed to the lane this run
            # took: the raise it earned, or the failure that left it on role
            # routing. The pointer is the record a later reader reaches without
            # the dispatching process, so a raise that could not be resolved
            # has to be visible here and not only in the dispatch payload.
            "section_routing": (
                None
                if resolution.section_routing is None
                else dict(resolution.section_routing)
            ),
            "lane_gate": resolution.lane_gate,
            # The orchestrator-lane stop the dispatch resolved, recorded even
            # when the fence did not fire: the pointer is the record a reader
            # reaches without the dispatching process, so "the lane declared
            # nothing" has to be distinguishable from a record written before
            # the declaration existed.
            "orchestrator_lane_stop": resolution.orchestrator_lane_stop,
            "orchestrator_lane_override": resolution.orchestrator_lane_override,
            "local": resolution.local,
            "execution_fit": resolution.execution_fit.as_dict(),
            "launch": launch_kind,
            # The host this attempt's worker was issued its pid on, recorded by
            # the launch rather than inherited from the pointer it merges over.
            # A queued request is stored with no host, because no process
            # exists yet, and the admission pass that launches it may run on
            # another machine: carrying the queued absence forward would leave
            # the pointer naming no host, and no host could then prove the
            # worker dead. A redispatch of a new run is stamped by the pointer
            # writer alone, and a settled run's later writes carry this value.
            "launcher_host": socket.gethostname(),
            "sandbox": backend.get("sandbox"),
            # Whether this launch was composed inside the fence wrapper,
            # overwritten from the composed plan below. The boundary check reads
            # this recorded fact rather than the current default, so a later
            # change to the default cannot redefine what an already-dispatched
            # run is checked against. A record written before the field existed
            # carries neither value and keeps the full scan.
            "fenced": False,
            "sandbox_write_roots": (
                None
                if resolution.sandbox_write_roots is None
                else [str(path) for path in resolution.sandbox_write_roots]
            ),
            # A backend permitting a run to continue an earlier session is a
            # property of the configuration, so it is named as one: a reader
            # taking a bare ``session_reuse`` for an observation reaches a true
            # answer to a question nobody asked. Whether this run actually
            # carried a session is written from its own launch below.
            "session_reuse_capable": bool(backend.get("session_reuse")),
            # Overwritten from the launched argv for a spawned run. An
            # in-harness launch is delegated, not spawned, so reckon cannot put
            # a prior session on its command line and the value stays false.
            "session_resumed": False,
            "member": effective_member,
            # The configuration that actually ran the node, recorded now because
            # a later config layer change makes it unreconstructable — and
            # without it a measured duration cannot be attributed to anything.
            "agent": agent,
            "competence": competence,
            "worktree": worktree["path"],
            "base": worktree["base"],
            "base_sha": worktree["base_sha"],
            # The directory this run's scratch was created at, recorded at
            # dispatch so the promotion or discard that later removes it removes
            # exactly that path rather than re-deriving it under a root that may
            # have moved.
            "scratch": str(worker_scratch_dir(run_id)),
            # The authored implementation fraction at dispatch, so promotion can refuse a passing
            # implement landing whose plan did not move. An unreadable value
            # stays absent, which exempts the run rather than recording a false
            # zero that would look like a plan that never moved.
            "plan_impl_at_dispatch": _plan_impl_at_dispatch(
                project, node.plan, ledger_root
            ),
            # The project's declaration as written, beside the command this
            # node's promotion will actually run. A scoped declaration stays a
            # template on the record so a later reader can tell a scope that
            # resolved to nothing from one that was never scoped, and the
            # resolved field is absent rather than empty when it did.
            "suite_command": suite_command,
            "suite_command_resolved": suite_command_resolved or None,
            "census_tests": list(census_tests),
            "suite_gate": suite_gate or "unarmed",
            "prompt_path": str(prompt_path),
            "log_path": str(log_path),
            "stderr_path": str(stderr_path),
            "final_message_path": str(final_path),
            "manifest_path": node.manifest_path,
            "manifest_baseline_mtime_ns": _manifest_mtime_ns(node.manifest_path),
            "peer_scopes": {name: sorted(paths) for name, paths in peers.items()},
            "peer_channel": {
                "endpoint": str(_channel_root(run_id)),
                "peers": {},
                "scope_transfer": False,
            },
            "created_at": _utc_now(),
            "attempt": attempt,
            "attempt_kind": (
                "shadow" if shadow_lineage else "redispatch" if lineage else "dispatch"
            ),
            # The promoted run this dispatch repairs, declared with --repairs.
            # Promotion reads it to exempt the impl move: the movement belongs
            # to the run being repaired. Absent when the dispatch declares none.
            "repairs": str(repairs or "").strip() or None,
            "attempt_started_at": attempt_started_at,
            "phase": "starting",
            "session_id": reuse_session,
            # A run that carries no session id names why it does not, from the
            # moment it is created rather than only once something folds its
            # stream in. A dispatch-time absence is the pending kind — the run's
            # own stream or harness task may still supply one — and observation
            # replaces this with the id or with the point the capture reached.
            "session_id_absent": _dispatch_session_absence(
                backend,
                reused=reuse_session,
                withheld=session_resolution["withheld"],
            ),
            # A prior run of this task whose session ended too large to
            # continue is not resumed, and that is written down rather than
            # left silent: a peer whose worker starts a fresh conversation
            # reads the session and the reason it was passed over here.
            "session_withheld": session_resolution["withheld"],
            "task": None,
            "pid": None,
            "argv": None,
            # The harness the launch resolves to, recorded explicitly rather
            # than left to be read off argv[0]: a placed launch prefixes the
            # scheduler onto the argv, so its first word names the scheduler and
            # a later reader reconstructing the backend from it would translate
            # the wrong lane.
            "command": None,
            "dialect": None,
            "budget": _backends.unknown_budget("no events yet"),
            "budget_fallback": budget_fallback,
            "route_fallback": dispatch_options.get("route_fallback"),
            "picker_selection": held_selection or picker_selection,
            "route_mode": (
                # A hold overridden onto the local lane it offered is its own
                # route mode, so a reader can count a hold the dispatcher
                # continued rather than tuning it in with the shadow picks.
                _overridden_hold_route_mode()
                if held_selection is not None
                else "fallback"
                if dispatch_options.get("route_fallback")
                else "picker"
                if admission_launch
                and not dispatch_options.get("named_route")
                and isinstance(picker_selection, Mapping)
                and picker_selection.get("action") == "route"
                else "explicit"
                if backend_name
                == str(
                    backend_override
                    or default_backend_override
                    or (config.get("local_backend") if local else "")
                    or ""
                )
                else "picker"
                if resolution.route == "picker"
                and picker_selection is not None
                and picker_selection.get("action") == "route"
                and picker_selection.get("backend") == backend_name
                else "shadow"
            ),
            "route": resolution.route,
            "route_override": resolution.route_override,
            "pace": pace_record,
            "warnings": [
                *resolution.warnings,
                *budget_warnings,
                *disregarded_claims,
                *(
                    [
                        _directory_claim_acceptance_line(row)
                        for row in accepted_directory_claims
                    ]
                ),
                *([released_follower_warning] if released_follower_warning else []),
            ],
            "done_when_warnings": [
                dict(item) for item in resolution.done_when_warnings
            ],
            "directory_claim_acceptances": list(accepted_directory_claims),
            "lineage": lineage,
            # A paired dispatch's primary arm names its partner here, because
            # the primary is not itself a shadow: its lineage is whatever an
            # ordinary dispatch of the node would record. The shadow arm names
            # its primary through the shadow lineage above, exactly as a
            # standalone shadow does, so the scan, the control predicate and
            # the pairing hold read either arm unchanged.
            **(
                {"paired_shadow_run_id": str(paired_shadow_run_id)}
                if paired_shadow_run_id
                else {}
            ),
            "unreconciled_override": waiver,
            "unreviewed_plan_override": plan_review_waiver,
            "watch_override": watcher_waiver,
            "watch": {
                "arming_line": _watch_arming_line(project),
                "attach_line": _watch_attach_line(project, session=session),
                "delivery": session_delivery,
                "watcher_live": False,
                "session": session,
                "session_attached": False,
                "session_follower_released": False,
                "watcher": {},
            },
        }

        # The advisory the dispatch computed rides the record when it exists,
        # beside the lane declaration and reading it was derived from. An
        # absent advisory is left off entirely rather than written as a null:
        # a null key would read as a lane that was checked and found quiet,
        # which is the opposite of a lane that was never assessed.
        if resolution.lane_advisory is not None:
            record["lane_advisory"] = resolution.lane_advisory

        # A deliberate unfenced dispatch survives on the record with its reason,
        # so a later reader can tell a launch that declined the fence from one
        # that never asked for it — the field is written only when the flag was
        # given, never as a null that would read as a fence that was built.
        if launch_kind == "cli" and fence_waiver:
            record["fence_waiver"] = {"reason": fence_waiver}

        # The defaults a layer names under ``unprotected_paths`` are left out of
        # this run's fence, so the run carries the list it left out. Written
        # only when the fence actually composed and only when a default was
        # removed: a run that removes nothing records no such key rather than an
        # empty one, which would read as a fence that was built and found whole.
        fence_unprotected = _backends.fence_unprotected_paths(config=config)

        if launch_kind == "cli":
            try:
                # Refused before composition, which seeds the run's harness
                # home: an absent backend must leave no run behind.
                preflight_launch_command(
                    backend_name, backend, fence=compose_fence, facts=dispatch_host
                )
                fence_roots = _fence_write_roots(
                    backend=backend,
                    repository=repo_root,
                    run_directory=run_dir(run_id),
                    manifest_path=node.manifest_path,
                    worktree=worktree["path"],
                    declared_write_paths=node.write_paths,
                )
                plan = resolve_launch_executable(
                    _backends.launch_plan(
                        backend_name=backend_name,
                        backend=backend,
                        prompt=prompt,
                        worktree=worktree["path"],
                        manifest_path=node.manifest_path,
                        writable_directories=fence_roots,
                        final_message_path=str(final_path),
                        resume_session=reuse_session,
                        images=node.images,
                        fence=compose_fence,
                        fence_config=config,
                        fence_waiver=fence_waiver or None,
                        masked_paths=(
                            paired_masks
                            if paired_masks is not None
                            else _shadow_mask_paths(shadow_lineage, primary, project)
                        ),
                    ),
                    facts=dispatch_host,
                )
                # Read before the placement wraps the plan: the harness sits at
                # the position the plan composes it at, and after the wrap that
                # element is the scheduler's rather than the harness's.
                harness_command = (
                    str(plan.argv[harness_command_index(plan.argv)])
                    if plan.argv
                    else None
                )
                record["session_harness"] = plan.dialect if reuse_session else None
                plan = resolve_backend_placement(plan, backend, project, payload=record)
                # The worker is handed the run's prompt file, not the plan, and
                # the plan is where the prompt is composed: a lane that names
                # its figures adds each figure's path to the text it carries.
                # The file written above holds the prompt before that
                # composition, so it is rewritten here from the composed text.
                # A dialect that takes its prompt on stdin carries it in
                # stdin_text; one that puts it on the command line carries it
                # nowhere else, so the launch's own prompt is the copy that keeps
                # the run's prompt.txt holding the brief it was dispatched with.
                prompt_path.write_text(_prompt_file_text(plan), encoding="utf-8")
            except (_backends.BackendError, flight.FlightConfigError, OSError) as exc:
                raise CrewError(format_refusal("D22", str(exc))) from exc
            placement = flight.placement_for(backend)
            job_id, job_id_status = placement_job_id(placement, run_id=run_id)
            record.update(
                {
                    # Whether the plan this dispatch composed carries the fence
                    # wrapper. Read from the composed argv, so a cli launch that
                    # asked for the fence is recorded as fenced while any launch
                    # that composed none is not.
                    "fenced": _plan_composed_the_fence(plan),
                    **(
                        {
                            "fence_unprotected_paths": [
                                str(path) for path in fence_unprotected
                            ]
                        }
                        if fence_unprotected and _plan_composed_the_fence(plan)
                        else {}
                    ),
                    # The pointer's pid is the per-run supervisor's, written
                    # once it is running, further down. Until then the run has no
                    # process identity, which is why it starts empty rather than
                    # naming a worker that is not spawned yet.
                    "pid": None,
                    "pid_start_time": None,
                    "argv": list(plan.argv),
                    # The directories this plan resolved for its backend, so a
                    # resume or a lane change composes the worker's PATH from
                    # what this launch searched rather than from whatever the
                    # flight layers declare when the later attempt runs.
                    "backend_search_path": list(plan.backend_search_path),
                    "command": harness_command,
                    "dialect": plan.dialect,
                    "session_resumed": _launched_prior_session(plan) is not None,
                    # A placed launch is charged to a scheduler job rather than
                    # to the coordinator's own login slice, so the job is the
                    # process identity a liveness read needs; it is recorded
                    # beside the pid, from which it is not derivable.
                    "job_id": job_id,
                    # The queries the placement declares are carried onto the
                    # record with it: a liveness read happens long after the
                    # configuration that launched the run may have changed, and
                    # the record is the only place the placement was ever
                    # recorded. A record that keeps the wrapper without its
                    # query cannot be asked about its own job.
                    "placement": (
                        None
                        if placement is None
                        else {
                            "scheduler": str(placement["scheduler"]),
                            "options": [
                                str(item) for item in placement.get("options") or ()
                            ],
                            "job_id_status": job_id_status,
                            **flight.placement_scheduler_queries(placement),
                        }
                    ),
                }
            )
        else:
            # An in-harness launch composes no plan here and no fence argv, so
            # it is recorded unfenced and its boundary check keeps the full scan
            # of every registered worktree.
            plan = None
            record["fenced"] = False
            record["directive"] = {
                "attach_with": f"reckon crew attach --run {run_id} --task <task-id>",
                "fences": {
                    "delivery": node.manifest_path,
                    "evidence": node.done_when,
                    "scope": list(node.write_paths),
                    "time": node.time_budget,
                },
                "prompt_path": str(prompt_path),
                "sandbox": {
                    "tier": backend.get("sandbox"),
                    "write_roots": record["sandbox_write_roots"],
                },
                "worktree": worktree["path"],
            }
            if dispatch_host.in_allocation:
                record["directive"]["environment"] = _persisted_worker_environment(
                    {}, facts=dispatch_host
                )
            record["directive"]["environment"] = _worker_runtime_environment(
                record["directive"].get("environment"),
                run_id=run_id,
                manifest_path=node.manifest_path,
                attempt_started_at=attempt_started_at,
                coordinator_session=session,
                claude_headers=False,
            )

        # Read the claims once more, now that the worktree, the prompt and the
        # peer wiring exist: two dispatches can both pass the admission check
        # before either has published a claim, and whichever arrives here
        # second must be the one refused, naming the first. This run's own
        # claim is excluded by identity, so the dispatch never arbitrates
        # against the claim it made itself.
        if not shadow_lineage:
            _raise_repository_scope_conflict(
                node,
                project=project,
                repo=repo_root,
                authority=authority,
                claims=_repository_scope_claims(
                    exclude_run_ids=(run_id,),
                    candidate_paths=candidate_paths,
                    candidate_repositories=candidate_repositories,
                ),
                **_directory_claim_acceptance_kwargs(
                    accept_directory_claim, accepted_directory_claims
                ),
                base_revision=dispatch_base,
                stacked=stacked_admissions,
                own_run_id=run_id,
                own_registered_at=claim_registered_at,
            )
            # Both checks have passed, so the dispatch stands. Write the stacking
            # on both pointers now, before the pointer write below: the holder
            # gains this run in its ``stacked_runs``, and the record about to be
            # published carries the run it was stacked on.
            for admission in stacked_admissions:
                record_stacked_dispatch(
                    str(admission.get("run_id") or ""),
                    run_id,
                    descendant_record=record,
                )
        # Publish the pointer before probing the watcher. Otherwise a watcher
        # could drain an empty fleet between the probe and this write, leaving
        # a new run behind a payload that incorrectly said it was watched.
        _write_json(pointer_path(run_id), record)
        # Any dispatch that fits ends the refusal's usefulness, whether or not
        # the brief changed: a trace left behind would mark a later dispatch
        # against an older refusal's figures, and a node never dispatched again
        # would leave its trace in the crew home for good.
        consume_context_refusal(node=node, project=project)
        record["peer_channel"] = _wire_peer_channels(record, adjacent_peers)
        wired_peer_run_ids = list(record["peer_channel"]["peers"])
        record["watch"] = watch_state(project, session=session)
        # The delivery verdict rides the payload so a later reader can tell a
        # session the host attached from one that still needs the Monitor tool.
        # A host-delivered session carries no arming instruction: the host armed
        # the follower, and re-arming a Monitor watch would double-deliver.
        record["watch"]["delivery"] = session_delivery
        if session_delivery == "host":
            record["watch"]["arming_line"] = ""
        _write_json(pointer_path(run_id), record)
        # Starting the supervisor is dispatch's last repository-facing step.
        # Every write dispatch makes inside a repository — the worktree, and
        # nothing else now that no dispatch registers a member of its own — is
        # complete before this point, so the boundary baseline can follow it
        # with no handshake: there is nothing left for dispatch to write that
        # the baseline must follow. After this dispatch writes only the pointer,
        # which lives under the configuration home outside every repository.
        # Dispatch waits for neither the snapshot nor the spawn.
        if launch_kind == "cli" and plan is not None:
            # Starting the worker is the one operation a caller-supplied launcher
            # stands in for and the one that fails for reasons outside
            # dispatch's own writes: a harness executable that is absent or not
            # executable, a refused fork, an exhausted process table. The plan
            # above is wrapped for exactly that reason; the spawn is not, so an
            # OSError from it would escape as a traceback. It is a launch
            # refusal and it is rendered as one, and because it is raised
            # inside the unwind below the refusal still leaves no pointer, run
            # directory or worktree behind.
            try:
                if launcher is None:
                    _prepare_attempt_records(
                        directory,
                        run_id=run_id,
                        attempt=int(record["attempt"]),
                        attempt_kind=str(record["attempt_kind"]),
                        attempt_started_at=str(record["attempt_started_at"]),
                    )
                    spec_path = directory / SUPERVISOR_SPEC_NAME
                    _write_json(
                        spec_path,
                        _supervisor_spec(
                            run_id=run_id,
                            run_directory=directory,
                            repo_root=repo_root,
                            worktree=Path(worktree["path"]),
                            plan=plan,
                            fenced=bool(record["fenced"]),
                            prompt_path=prompt_path,
                            log_path=log_path,
                            stderr_path=stderr_path,
                            facts=dispatch_host,
                            attempt=int(record["attempt"]),
                            attempt_kind=str(record["attempt_kind"]),
                            attempt_started_at=str(record["attempt_started_at"]),
                        ),
                    )
                    _require_fleet_gate_open()
                    _refuse_a_discarded_launch(run_id)
                    refuse_a_released_launch(run_id)
                    spawned_pid = _start_supervisor(spec_path, directory, run_id)
                    if reservation is not None:
                        # The supervisor only becomes the hold once it exists:
                        # from here the reservation is freed by its exit, not by
                        # this dispatching process, which returns while the
                        # worker it started runs on. A rewrite that does not
                        # land leaves the hold naming this process, so it is
                        # reported on stderr rather than suppressed; the
                        # attempt's own exit receipt frees it in its own time.
                        _record_supervisor(reservation, spawned_pid)
                    advance_launch_step(run_id, LAUNCH_WORKER_STEP)
                else:
                    _require_fleet_gate_open()
                    _refuse_a_discarded_launch(run_id)
                    refuse_a_released_launch(run_id)
                    spawned_pid = launcher(
                        plan,
                        log_path=log_path,
                        stderr_path=stderr_path,
                        prompt_path=prompt_path,
                    )
                    if reservation is not None:
                        # A caller-supplied launcher returns from the launch it
                        # stood in for, and nothing on disk records the end of
                        # whatever it started, so the reservation is released
                        # here rather than held by this dispatch's own exit.
                        _release_launch_reservation(
                            reservation, only_when_unspawned=True
                        )
                    advance_launch_step(run_id, LAUNCH_WORKER_STEP)
            except OSError as exc:
                raise CrewError(
                    format_refusal("D22", f"the worker launch could not start: {exc}")
                ) from exc
            if launcher is not None:
                # A caller-supplied launcher is a test seam that stands in for
                # the supervisor: it spawns synchronously and the boundary
                # baseline is taken inline, exactly as the supervisor would.
                record["repository_tree_snapshot"] = _repository_tree_snapshot(
                    repo_root,
                    roots=_boundary_snapshot_roots(
                        repo_root, Path(worktree["path"]), fenced=bool(record["fenced"])
                    ),
                )
            spawned_start_time = _process_start_time(spawned_pid)
            record["pid"] = spawned_pid
            record["pid_start_time"] = spawned_start_time

            # Publish every launch field after the spawn, under the pointer lock.
            # A competing refusal may have replaced the pre-spawn record; the
            # supervisor's newer observations still belong to this attempt.
            def attach_launch_identity(pointer: dict[str, Any]) -> dict[str, Any]:
                from reckon.crew.queue_admission import _archive_queued_life

                published = {**pointer, **record}
                for key in ("phase", "session_id", "session_id_absent", "task"):
                    if (
                        pointer.get(key) is not None
                        and pointer.get("phase") != "queued"
                    ):
                        published[key] = pointer[key]
                published["launch_recorded_at"] = _utc_now()
                for request in (request_pointer or {}, pointer):
                    _archive_queued_life(
                        published, request, admitted_at=published["launch_recorded_at"]
                    )
                return published

            # The pointer now names the running worker, so a discard arriving
            # after this merge is refused by discard's own liveness test — the
            # record it reads names a process. A discard that landed inside the
            # spawn, before this merge, was refused by nothing: the pointer it
            # read carried no pid yet, so it removed the pointer and left its
            # own record for the rollback to find. Read once more here, so the
            # launch refuses in that case too and stops the worker it already
            # has a pid for.
            record = _mutate_pointer(run_id, attach_launch_identity)
            _refuse_a_discarded_launch(run_id, started=True)
            refuse_a_released_launch(run_id, started=True)
            advance_launch_step(run_id, LAUNCH_IDENTITY_STEP)
            # The release may have landed between the pre-spawn check and the
            # spawn, and this launch already owns a pid: refusing here hands it
            # to the rollback, which signals it, so a stalled launch that
            # resumes cannot leave a worker running for a released run.
        else:
            # A delegated launch spawns no process, so there is no supervisor to
            # take the boundary baseline after dispatch's writes. Dispatch takes
            # it here instead, in the same last repository-facing step, so the
            # baseline still predates every write this run's worker will make.
            _write_boundary_tree_snapshot(
                directory,
                repo_root,
                worktree=Path(str(worktree["path"])),
                fenced=bool(record["fenced"]),
            )
    except Exception as exc:
        # Undoing this run is the rollback, and a step of a rollback that fails
        # must not become the error the caller reads: the dispatch stopped for
        # the reason above, and an unwind that refused here — a worktree
        # removal answering a claim — would report the cleanup instead, so the
        # operator would re-run the dispatch by hand to find out what actually
        # happened. The unwind's own failure therefore rides the original —
        # attached as its cause when the original names none, and as a note
        # when it already does, so the cause a launch refusal carries, the
        # OSError that ended the spawn, is preserved rather than replaced.
        # It is printed here too, where a caller that shows only the original's
        # message would otherwise lose the tree the rollback could not remove.
        try:
            _unwire_peer_channels(run_id, wired_peer_run_ids)
            # A launch that refused before its supervisor was recorded releases
            # its reservation at once, so the next launch of the run is
            # admitted. One that recorded a supervisor leaves it to that
            # supervisor's exit, whatever this unwind then signals.
            from reckon.crew.dispatch_launch import (
                release_launch_reservation as _release_launch_reservation,
            )

            _release_launch_reservation(reservation, only_when_unspawned=True)
            if spawned_pid is not None:
                with contextlib.suppress(CrewError, OSError):
                    _signal_process_group(
                        spawned_pid,
                        spawned_start_time,
                        run_dir=run_dir(run_id),
                        reason="dispatch-rollback",
                    )
            # The pointer goes first. The worktree remover refuses a worktree
            # that a live pointer still claims, and until this run's own pointer
            # is gone it is that claim — so removing the worktree first raised,
            # and the unlink and the run-directory removal below never ran. A
            # refusal reached after the pointer write therefore left a live
            # pointer behind, which a reader takes for a run whose process died
            # without a manifest. Unlinking first clears the claim, and the run
            # directory follows, so a refusal is indistinguishable from a
            # dispatch that never ran.
            from reckon.crew.promotion import discard_recorded

            restore_request = (
                admission_launch
                and admitted_from == run_id
                and spawned_pid is None
                and request_pointer is not None
                and not discard_recorded(run_id)
            )
            if restore_request:
                from reckon.crew.queue_admission import (
                    _retain_launched_record,
                    _stamp_failure,
                )

                # The starting claim published the attempt record beside the
                # claim. This request is going back to the queue rather than
                # running, so the record describes an attempt that never
                # happened: kept, the restored request reads afterwards as a
                # launch that stopped after its claim.
                _attempt_record_for(run_id).unlink(missing_ok=True)
                failure = f"{type(exc).__name__}: {exc}"

                def restore(pointer: dict[str, Any]) -> dict[str, Any]:
                    refused = _stamp_failure(
                        dict(request_pointer),
                        failure,
                        moment=datetime.now(UTC),
                        guard=None,
                    )
                    if _retain_launched_record(pointer, refused):
                        return pointer
                    return request_pointer

                _mutate_pointer(run_id, restore)
            else:
                _release_launch_claim(run_id)
            # A refusal can land before the worktree exists — the creation
            # itself is the first thing inside this guard — and there is then
            # nothing of this run's to remove. The pre-existing worktree of an
            # earlier run is left in place either way, which is what its owner
            # expects.
            if worktree is not None:
                _remove_worktree(repo_root, worktree["path"])
        except Exception as rollback_failure:
            print(
                f"crew: undoing run {run_id} failed: {rollback_failure}",
                file=sys.stderr,
            )
            # A launch refusal already carries the error that ended the spawn
            # as its cause. Chaining the unwind onto it would replace that
            # cause and drop the inner error a reader needs to diagnose an
            # absent harness executable, so preserve it and carry the unwind
            # as a note, which a traceback prints beside the cause.
            cause = exc.__cause__
            if cause is None:
                raise exc from rollback_failure
            exc.add_note(f"undoing run {run_id} failed: {rollback_failure}")
            raise exc from cause
        raise
    finally:
        # Released only now, after any unwind has removed the worktree a
        # refusal cut: a next dispatch taking the claim earlier would race its
        # own creation against that removal.
        if node_claim is not None:
            node_claim.release()
    if node_claim is not None and node_claim.reclaimed:
        record["reclaimed_node_claim"] = node_claim.reclaimed
    if deferred_shadow_selection:
        try:
            _start_shadow_picker_selection(
                run_id=run_id,
                node=node,
                config=config,
                project=project,
                repo=repo_root,
                ledger_root=ledger_root,
                session=session,
                comment=comment,
                due=due,
                urgency=urgency,
            )
        except (OSError, TypeError, ValueError) as exc:
            selection = _picker_fallback(
                f"shadow launch failed: {type(exc).__name__}: {exc}", comment
            )
            record["picker_selection"] = selection
            _attach_shadow_picker_selection(run_id, selection)
    return {**record, **read_dispatch_detail(record)}
