# ruff: noqa: I001, UP035
from __future__ import annotations
import json
import uuid
from dataclasses import (
    dataclass,
    field,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Iterable,
    Mapping,
)
from reckon import (
    _store,
    capability,
    flight,
    ledger,
)
from reckon.crew import (
    bar as bar_module,
)
from reckon.crew import (
    lane_document as _lane_document,
)
from reckon.crew import (
    prescription as prescription_module,
)
from reckon.crew.node import (
    _SAFE_ID,
    BudgetHold,
    CrewError,
    NodeValidation,
    ScopeConflict,
    TaskNode,
    UnreconciledRuns,
    done_when_warnings,
    gate_population_finding,
    negative_control_finding,
    normalize_section,
    role_may_write_repository_paths,
    validate_node,
)
from reckon.crew.refusals import (
    format_refusal,
)
from reckon.crew.routing import (
    _agent_configuration,
    _competence_verdict,
    _fleet_script,
    require_plan_reviewed,
    require_plan_section_visible,
    resolve_dispatch_authority,
    resolve_dispatch_ledger_root,
    resolve_role,
    resolve_role_override,
    resolved_time_budget,
    resolved_time_ceiling,
)
from reckon.crew.runs import (
    list_live,
    new_run_id,
    read_pointer,
    run_dir,
    watch_state,
)



@dataclass
class DispatchPlan:
    """Everything a dispatch resolved, before anything on disk has changed.

    Separating resolution from effect is what lets a dry run be the *same*
    decision as a real dispatch rather than a second implementation of it: a
    caller can see the routing, the filled-in defaults and the verdict without
    a worktree or a process existing.
    """

    run_id: str
    backend: str
    launch: str
    backend_settings: dict[str, Any]
    node: TaskNode
    budget_ceiling: str
    validation: NodeValidation
    execution_fit: capability.ExecutionFit
    token_budget: int | None = None
    local: bool = False
    warnings: list[str] = field(default_factory=list)
    done_when_warnings: list[dict[str, str]] = field(default_factory=list)
    competence: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None
    live_conflicts: list[dict[str, Any]] | None = None
    admission: dict[str, Any] | None = None
    directory_claim_acceptances: list[dict[str, Any]] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None
    requested_backend: str | None = None
    default_backend: str | None = None
    section_routing: dict[str, Any] | None = None
    lane_declaration: dict[str, Any] | None = None
    lane_reading: dict[str, Any] | None = None
    lane_gate: dict[str, Any] | None = None
    lane_allowance: dict[str, Any] | None = None
    orchestrator_lane_stop: dict[str, Any] | None = None
    orchestrator_lane_override: dict[str, str] | None = None
    lane_advisory: dict[str, Any] | None = None
    open_endedness: float | None = None
    picker_selection: dict[str, Any] | None = None
    route: str = "shadow"
    route_override: str | None = None
    watch: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        agent = _stamp_agent_display(
            _agent_configuration(self.backend, self.launch, self.backend_settings),
            self.backend_settings,
        )
        if self.local:
            agent["local"] = True
        payload = {
            "agent": agent,
            "backend": self.backend,
            "picker_selection": self.picker_selection,
            "route": self.route,
            "route_override": self.route_override,
            "default_backend": self.default_backend,
            "execution_fit": self.execution_fit.as_dict(),
            "launch": self.launch,
            "local": self.local,
            "lane_advisory": (
                None if self.lane_advisory is None else dict(self.lane_advisory)
            ),
            "lane_declaration": (
                None if self.lane_declaration is None else dict(self.lane_declaration)
            ),
            "lane_reading": (
                None if self.lane_reading is None else dict(self.lane_reading)
            ),
            "lane_gate": (
                None if self.lane_gate is None else dict(self.lane_gate)
            ),
            "lane_allowance": (
                None if self.lane_allowance is None else dict(self.lane_allowance)
            ),
            "node": self.node.as_dict(),
            "brief": _brief_record(self.node),
            "orchestrator_lane_stop": (
                None
                if self.orchestrator_lane_stop is None
                else dict(self.orchestrator_lane_stop)
            ),
            "orchestrator_lane_override": (
                None
                if self.orchestrator_lane_override is None
                else dict(self.orchestrator_lane_override)
            ),
            "requested_backend": self.requested_backend,
            "run_id": self.run_id,
            "section_routing": (
                None if self.section_routing is None else dict(self.section_routing)
            ),
            "sandbox": {
                "tier": self.backend_settings.get("sandbox"),
                "write_roots": (
                    None
                    if self.sandbox_write_roots is None
                    else [str(path) for path in self.sandbox_write_roots]
                ),
            },
            "time_budget": self.node.time_budget,
            "token_budget": self.token_budget,
            "validation": self.validation.as_dict(),
            "write_paths": list(self.node.write_paths),
            "warnings": list(self.warnings),
            "done_when_warnings": [dict(item) for item in self.done_when_warnings],
        }
        if self.competence is not None:
            payload["competence"] = dict(self.competence)
        if self.authority is not None:
            payload["authority"] = dict(self.authority)
        if self.live_conflicts is not None:
            payload["live_conflicts"] = [dict(item) for item in self.live_conflicts]
        if self.admission is not None:
            payload["admission"] = dict(self.admission)
            payload["record_assignment"] = {
                "state": "unevaluated",
                "fields": "all",
                "wave": "unevaluated",
            }
        if self.directory_claim_acceptances is not None:
            payload["directory_claim_acceptances"] = [
                dict(item) for item in self.directory_claim_acceptances
            ]
        if self.watch is not None:
            payload["watch"] = dict(self.watch)
        return payload


def plan_dispatch(
    *,
    node: TaskNode,
    config: Mapping[str, Any],
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    run_id: str | None = None,
    project: str = "",
    repo: str | Path | None = None,
    base: str = "HEAD",
    execution_override: bool = False,
    orchestrator_lane_reason: str | None = None,
    authority: Mapping[str, Any] | None = None,
    report_live_conflicts: bool = False,
    local: bool = False,
    backend_override: str | None = None,
    default_backend_override: str | None = None,
    declared_backend: str | None = None,
    member: str = "",
    allow_unreviewed_plan: bool = False,
    session: str = "",
    watch_required: bool = False,
    watch_override: bool = False,
    repairs: str = "",
    accept_directory_claim: bool = False,
    route: str | None = None,
    picker_selection: Mapping[str, Any] | None = None,
) -> DispatchPlan:
    """Resolve routing and defaults for one node and judge it. No side effects.

    Mutates only the node it was handed, filling the defaults a dispatch would
    fill — the time budget from the resolved fence and the manifest path from
    the run directory — so the verdict is the one a real dispatch would reach.

    ``backend_override`` and ``default_backend_override`` re-resolve the role's
    own overlay against a caller-requested backend instead of the configured
    default. Keeping both request surfaces here makes them inherit the same
    disagreement refusal and every later check this function performs
    (execution fit, sandbox reachability, and write-path scope).
    """
    if not _SAFE_ID.fullmatch(node.id):
        raise CrewError(f"node id {node.id!r} must match {_SAFE_ID.pattern}")
    if node.spec_level not in ("", "exact", "guided", "open"):
        raise CrewError(
            f"spec level {node.spec_level!r} is not one of exact, guided, open, "
            "or empty (undeclared)"
        )
    fleet_gate = _dispatch_fleet_gate()
    if fleet_gate["state"] in _LANE_GATE_WAITING_STATES:
        raise LanePaused(fleet_gate)
    require_worker_scratch_headroom(config)
    # Proven here rather than at worktree creation so that a dry run, whose
    # documented job is to validate the call, cannot report a dispatchable
    # node that the real dispatch then refuses on a missing precondition.
    _fleet_script()
    # A dry run must reach the verdict the real dispatch reaches, and the real
    # dispatch refuses a repository that is not the project's mount, so the
    # same resolution runs here. A caller that named no repository keeps its
    # ``None``: only the dispatch path turns that into the mount.
    if repo is not None and project_mount_repository(project) is not None:
        repo = resolve_project_repository(project, repo)
    # The run a --repairs dispatch repairs must already have landed, so the
    # declaration is judged here, beside the other preconditions, before a
    # worktree, pointer or process exists. A dry run reaches the same refusal
    # because this is the one place the check runs.
    if repairs:
        _require_repairs_target(project, repairs, authority=authority)
    requested_backend = str(backend_override or default_backend_override or "").strip()
    route_override = route
    route = resolve_dispatch_route(config, route)
    selection_absent = route == "picker" and picker_selection is None
    if selection_absent:
        # A caller reaches this function without a selection whenever it does
        # not run dispatch's own picker step — a validating dry run, or an
        # internal re-dispatch. The route still has to resolve, so fall back to
        # the routing that would have run without the picker and record why the
        # picker's answer is absent rather than refusing the whole dispatch.
        picker_selection = _picker_fallback("picker-selection-absent", "")
    if route == "picker" and picker_selection is not None:
        action = str(picker_selection.get("action") or "")
        if action == "hold":
            raise BudgetHold(
                {
                    "held": True,
                    "reason": _picker_refusal_reasons(picker_selection),
                    "picker_selection": dict(picker_selection),
                }
            )
        named_backend = (
            str(picker_selection["backend"])
            if action == "route" and picker_selection.get("backend")
            else ""
        )
        if named_backend:
            requested_backend = named_backend
        elif action in ("route", "refuse", "fallback"):
            # The picker names no backend it can route to — it refused, its route
            # carries no backend, or it fell back — so the dispatch continues
            # exactly as deterministic routing would: the configured default
            # stands in and the gates below produce their own refusal or hold.
            # A picker that finds nothing eligible must never make a refuse worse
            # than the deterministic routing it replaces. The selection stays
            # recorded on the run so the reader sees the picker found nothing.
            # A caller that already named a backend keeps its request; otherwise
            # the configured default stands in, which is the routing a
            # deterministic dispatch would have resolved on its own.
            if not selection_absent:
                requested_backend = str(config.get("default_backend") or "")
        else:
            raise CrewError(f"picker returned unknown action {action!r}")
    # The configured local lane, named here so a ``--local`` dispatch has one
    # concrete backend to agree or disagree with. The CLI has already merged it
    # into ``default_backend``, so this is the same value role routing would
    # fall through to; reading it directly is what makes the flag a request
    # rather than a silent default a member's harness can displace.
    local_backend_name = str(config.get("local_backend") or "").strip() if local else ""
    caller_declared_backend = str(
        requested_backend if declared_backend is None else declared_backend
    ).strip()
    # The command passes an empty string when its option is omitted. ``None``
    # belongs to internal callers that did not invoke that routing surface.
    if member and (
        local or backend_override is not None or default_backend_override is not None
    ):
        # A caller that named a lane and no repository reaches this function
        # with ``None``, while the launching path resolves the project's
        # registered mount before it ever gets here. Resolving the same mount
        # here keeps the validating and launching answers one decision, and a
        # project with no registered mount still refuses with the flag named.
        if repo is None:
            repo = resolve_project_repository(project, None)
        member_authority = dict(
            authority or resolve_dispatch_authority(project, Path(repo).resolve())
        )
        roster_member = ledger.member(
            project,
            member,
            root=resolve_dispatch_ledger_root(member_authority),
        )
        if roster_member is None:
            raise CrewError(
                format_refusal(
                    "D14",
                    f"project {project!r} has no crew member {member!r}; register it "
                    "with `reckon crew member add` before dispatching to it",
                )
            )
        member_harness = str(roster_member.get("harness") or "").strip()
        # A ``--local`` request names the local lane, so a member whose harness
        # is a different backend cannot run the node: refuse rather than let the
        # harness silently displace the flag and report a local run that landed
        # on a metered lane.
        if (
            local_backend_name
            and member_harness
            and member_harness != local_backend_name
        ):
            raise CrewError(
                format_refusal(
                    "D15",
                    f"--local resolves the configured local backend "
                    f"{local_backend_name!r}, but crew member {member!r} declares "
                    f"harness {member_harness!r}",
                )
            )
        if requested_backend and member_harness and requested_backend != member_harness:
            raise CrewError(
                format_refusal(
                    "D15",
                    f"dispatch requests backend {requested_backend!r}, but crew member "
                    f"{member!r} declares harness {member_harness!r}",
                )
            )
        requested_backend = requested_backend or member_harness
    # Only a dispatch with no caller or roster request may fall through to role
    # and default routing. A wrong lane that announces itself costs one
    # redispatch; a wrong lane that reports success can look merely quiet
    # indefinitely.
    section_routing: dict[str, Any] | None = None
    if requested_backend:
        backend_name, backend = resolve_role_override(
            config, node.role, node.spec_level, requested_backend
        )
    else:
        # The section's own record steers the lane, so a section that keeps
        # costing attempts lands on the class its count earns without anyone
        # deciding it by hand. A node with no readable record resolves exactly
        # as role routing always resolved it, and so does one whose rule raised
        # while being read: the failure is recorded instead of the lane, since a
        # rule that cannot be resolved must not stop a node dispatching.
        section_routing = _dispatch_section_routing(
            config,
            node=node,
            project=project,
            repo=repo,
            authority=authority,
        )
        if section_routing is None or section_routing.get("failure") is not None:
            backend_name, backend = resolve_role(config, node.role, node.spec_level)
        else:
            backend_name = str(section_routing["backend"])
            backend = dict(section_routing["backend_settings"])
    launch_kind = backend.get("launch")
    if launch_kind not in ("cli", "in-harness"):
        raise CrewError(
            format_refusal(
                "D22",
                f"backend {backend_name!r} declares launch {launch_kind!r}; "
                "expected 'cli' or 'in-harness'",
            )
        )
    # A placement's declared requirements are checked here rather than at
    # launch, so a dry run reports what a real dispatch would and the refusal
    # lands before a worktree, a pointer or a job exists. A requirement the
    # target node cannot see fails inside the worker and reads as a worker
    # defect, which is the reading this refuses to hand anyone.
    check_placement_requirements(backend.get("placement"), backend_name=backend_name)
    # Local is a property of where the dispatch actually landed, not of the
    # flag the caller passed: a request that resolved onto another backend — a
    # budget fallback, or a lane the caller named alongside the flag — is not a
    # local run and must not be recorded as one.
    local_resolved = bool(
        local and local_backend_name and backend_name == local_backend_name
    )
    default_budget = resolved_time_budget(config, backend)
    budget_ceiling = resolved_time_ceiling(config)
    default_token_budget = _resolved_token_budget(config, backend)
    node.time_budget = node.time_budget or default_budget
    node.section = normalize_section(node.section)
    resolved_run_id = run_id or new_run_id(node.id)
    durable_manifest = str(run_dir(resolved_run_id) / "manifest.md")
    caller_manifest = bool(node.manifest_path)
    node.manifest_path = node.manifest_path or durable_manifest
    warnings = list(getattr(config, "warnings", ()))
    if caller_manifest and _path_is_tmpfs(node.manifest_path):
        warnings.append(
            f"manifest path {node.manifest_path!r} is on tmpfs; use the durable "
            f"default {durable_manifest!r} so delivery survives session cleanup"
        )
    if not node.write_paths:
        node.write_paths = _resolved_write_paths(
            backend, run_directory=run_dir(resolved_run_id)
        )
    node.peer_scopes = {
        name: list(paths) for name, paths in (peer_scopes or {}).items()
    }
    execution_fit = capability.assess_execution_fit(
        node.done_when,
        role=node.role,
        execution_capable=backend.get("execution_capable"),
        override=execution_override,
    )
    verdict = validate_node(
        node, locked_decisions=locked_decisions, budget_ceiling=budget_ceiling
    )
    # A node whose scope includes a test file writes a check, and a check whose
    # author never named the mutation it must fail against is a guard that
    # passed by not exercising anything. The trigger is the declared write path
    # rather than the done-when prose, so the refusal rests on a structured
    # field the dispatcher can read.
    control_finding = negative_control_finding(node)
    if control_finding is not None:
        verdict = NodeValidation(
            ok=False, findings=[*verdict.findings, control_finding]
        )
    # The gate command is the check the brief tells the worker to run, so a
    # population that names no file the repository holds is caught here, where
    # it costs a refusal, rather than inside the worker, where it costs the
    # worker's judgement about which substitute the coordinator meant. A
    # caller that named no repository has no store to ask and is left alone.
    if repo is not None:
        population_finding = gate_population_finding(
            node, repository=Path(repo).resolve()
        )
        if population_finding is not None:
            verdict = NodeValidation(
                ok=False, findings=[*verdict.findings, population_finding]
            )
    if not execution_fit.allowed:
        verdict = NodeValidation(
            ok=False,
            findings=[
                *verdict.findings,
                {
                    "property": "fully-specified",
                    "detail": execution_fit.refusal_detail(),
                },
            ],
        )
    # Judged on the scope the node itself declares, before the shared landing
    # paths are appended to it below. The three of those are dispatch's own
    # bookkeeping -- every node on a plan carries them -- so counting them as
    # the node's artifacts would score every node on a plan above the
    # prescribed band the bar's local outcome is defined by, and the band would
    # describe nothing rather than the work.
    open_endedness = open_endedness_score(node)
    resolved_authority: dict[str, Any] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None
    if verdict.ok and repo is not None:
        resolved_authority = dict(
            authority or resolve_dispatch_authority(project, repo)
        )
        # A landing grant is for a role that lands work in the tree. Sandbox
        # writability is necessary but not sufficient: a verifier role's
        # sandbox can write the worktree (the `test` role is `worktree-full`),
        # yet promotion refuses a verifier commit that touches any repository
        # path. Gating on the role predicate as well keeps the default fragment
        # scope and the promotion refusal reading one authority, so a verifier
        # is never offered a repository write path it cannot land.
        if role_may_write_repository_paths(node.role) and _can_write_worktree(
            backend,
            repository=Path(repo).resolve(),
            run_directory=run_dir(resolved_run_id),
        ):
            _grant_landing_write_paths(
                node,
                project=project,
                authority=resolved_authority,
                warnings=warnings,
            )
        _require_write_paths_in_authority(node, resolved_authority)
        if node.brief.strip():
            # A brief is authority text whether it stands alone or beside a
            # plan section, so its digest is taken either way. It is taken
            # from the run's own stored copy whenever dispatch has made one —
            # a later read (a lane change, a resume) rebuilds the node from the
            # pointer, and the source path a coordinator handed in may be a
            # scratch file no longer on disk. At first dispatch no copy exists
            # yet, so the source is read.
            node.brief_sha256 = _brief_digest(node.brief_path or node.brief)
        if not node.plan.strip():
            # A brief alone names no committed plan section, so the gates that
            # join a node to a base blob and a stored review have nothing to
            # read and are skipped around their call sites rather than inside
            # the shared gate. A brief beside a plan section takes the plan
            # branch below: the plan is still the authority the gates read.
            resolved_authority["plan"] = {
                **resolved_authority["plan"],
                "base_sha": "",
            }
        else:
            plan_commit = require_plan_section_visible(
                node=node,
                project=project,
                repo=repo,
                base=base,
                authority=resolved_authority,
            )
            review_warning = require_plan_reviewed(
                node=node,
                project=project,
                repo=repo,
                authority=resolved_authority,
                allow_unreviewed=allow_unreviewed_plan,
                enforce=flight.plan_review_gate_enforces(config),
            )
            if review_warning is not None:
                warnings.append(review_warning)
            resolved_authority["plan"] = {
                **resolved_authority["plan"],
                "base_sha": plan_commit,
            }
            overlap_warning = _done_when_plan_overlap_warning(
                node=node,
                project=project,
                authority=resolved_authority,
                plan_commit=plan_commit,
            )
            if overlap_warning is not None:
                warnings.append(overlap_warning)
        sandbox_write_roots, sandbox_findings = _sandbox_reachability(
            node,
            backend=backend,
            repository=Path(repo).resolve(),
            run_directory=run_dir(resolved_run_id),
        )
        if sandbox_findings:
            verdict = NodeValidation(
                ok=False,
                findings=[*verdict.findings, *sandbox_findings],
            )
    lane_declaration: dict[str, Any] | None = None
    lane_advisory: dict[str, Any] | None = None
    if verdict.ok:
        ledger_root = (
            resolve_dispatch_ledger_root(resolved_authority)
            if resolved_authority is not None
            else None
        )
        observation = (
            _dispatch_lane_observation(
                project,
                root=ledger_root,
                config=config,
                backend_name=backend_name,
                backend=backend,
            )
            if backend.get("budget_check")
            else None
        )
        lane_declaration = _lane_declaration_evidence(
            declared_backend=caller_declared_backend,
            resolved_backend=backend_name,
            observation=observation,
        )
        lane_advisory = _dispatch_lane_advisory(
            backend_name=backend_name,
            metered=not ledger.is_unmetered_backend(backend_name),
            observation=observation,
            node=node,
            cheaper_lane={"lane": None, "state": "not_evaluated", "detail": ""},
        )
        if lane_advisory["state"] == "emitted":
            lane_advisory["cheaper_lane"] = _lane_advisory_cheaper_lane(
                _lane_advisory_ledger_runs(project, ledger_root),
                resolved_lane=backend_name,
                role=node.role,
                spec_level=node.spec_level,
                configured_lanes=sorted(
                    str(name) for name in (config.get("backends") or {})
                ),
            )
        if backend.get("budget_check") and not caller_declared_backend:
            verdict = NodeValidation(
                ok=False,
                findings=[
                    *verdict.findings,
                    _lane_declaration_finding(
                        backend_name=backend_name,
                        observation=observation,
                        alternatives=_unmetered_dispatch_alternatives(
                            config, role=node.role, spec_level=node.spec_level
                        ),
                    ),
                ],
            )
    if not verdict.ok:
        verdict = NodeValidation(
            ok=False,
            findings=[
                {
                    **finding,
                    "detail": format_refusal("D07", str(finding["detail"])),
                }
                for finding in verdict.findings
            ],
        )
    lane_reading = _dispatch_lane_reading(backend)
    lane_gate = _dispatch_lane_gate(backend)
    lane_allowance = _dispatch_lane_allowance(backend, session=session)
    orchestrator_lane_stop = _dispatch_orchestrator_lane_stop(
        backend_name=backend_name,
        backend=backend,
        config=config,
        role=node.role,
        spec_level=node.spec_level,
    )
    reason = (
        None
        if orchestrator_lane_reason is None
        else str(orchestrator_lane_reason).strip()
    )
    if reason == "":
        raise CrewError("--allow-orchestrator-lane requires a non-empty reason")
    if reason and orchestrator_lane_stop["state"] != "declared":
        raise CrewError(
            f"resolved lane {backend_name!r} does not declare "
            f"{ORCHESTRATOR_LANE_DECLARATION_KEY}; --allow-orchestrator-lane "
            "override does not apply"
        )
    orchestrator_lane_override = None
    if reason and orchestrator_lane_stop["state"] == "declared":
        orchestrator_lane_override = {"lane": backend_name, "reason": reason}
        orchestrator_lane_stop = {
            **orchestrator_lane_stop,
            "state": "overridden",
            "severity": "overridden",
            "reason": reason,
        }
    resolution = DispatchPlan(
        run_id=resolved_run_id,
        backend=backend_name,
        launch=str(launch_kind),
        backend_settings=backend,
        node=node,
        budget_ceiling=budget_ceiling,
        token_budget=default_token_budget,
        validation=verdict,
        execution_fit=execution_fit,
        local=local_resolved,
        warnings=warnings,
        done_when_warnings=done_when_warnings(node.done_when),
        authority=resolved_authority,
        requested_backend=requested_backend or None,
        default_backend=str(config.get("default_backend") or "") or None,
        section_routing=(
            None
            if section_routing is None
            else _section_routing_evidence(section_routing)
        ),
        lane_declaration=lane_declaration,
        lane_reading=lane_reading,
        lane_gate=lane_gate,
        lane_allowance=lane_allowance,
        orchestrator_lane_stop=orchestrator_lane_stop,
        orchestrator_lane_override=orchestrator_lane_override,
        lane_advisory=lane_advisory,
        open_endedness=open_endedness,
        # A resolved plan records the picker answer only when the route used it.
        # A shadow (or deterministic) preview reads the same whatever the picker
        # happened to say, so two dry runs of one node differ in no field the
        # picker touched; a routed preview keeps the decision it routed by, minus
        # the per-call measurements that differ between two asks.
        picker_selection=(
            _reportable_picker_selection(picker_selection)
            if route == "picker"
            else None
        ),
        route=route,
        route_override=route_override,
    )
    if orchestrator_lane_stop["state"] == "declared":
        # The stop reaches the warnings a caller reads and the record a later
        # reader opens, so a run that spent an orchestrator lane carries the
        # fact whether or not anyone read the dispatch payload at the time.
        resolution.warnings.append(_orchestrator_lane_stop_line(orchestrator_lane_stop))
    if verdict.ok and repo is not None:
        resolution.competence = _competence_verdict(
            resolution=resolution, project=project, repo=Path(repo).resolve()
        )
        if report_live_conflicts:
            repo_root = Path(repo).resolve()
            claims = _repository_scope_claims()
            # The directory-claim judgement reads exactly the rows the
            # exclusive-claim walk produced at base, so the granted report
            # below adds rows to the record but nothing it adds can drop a
            # refusal or judge a collision the walk never saw.
            refusal_rows = _live_conflict_rows(
                node,
                project=project,
                repo=repo_root,
                authority=resolved_authority,
                claims=claims,
                disregarded=resolution.warnings,
                include_granted_landing=False,
            )
            resolution.live_conflicts = _live_conflict_rows(
                node,
                project=project,
                repo=repo_root,
                authority=resolved_authority,
                claims=claims,
                disregarded=resolution.warnings,
            )
            directory_rows = [
                row
                for row in refusal_rows
                if _live_conflict_is_a_directory_claim(row, repo_root)
            ]
            if directory_rows:
                if accept_directory_claim:
                    resolution.directory_claim_acceptances = [
                        {
                            "candidate_path": entry["left_path"],
                            "claimed_path": row["claimed_path"],
                            "run_id": row["run_id"],
                            "node": row["node"],
                            "project": row.get("project", project),
                        }
                        for row in directory_rows
                        for entry in row["paths"]
                    ]
                    resolution.warnings.extend(
                        _directory_claim_acceptance_line(entry)
                        for entry in resolution.directory_claim_acceptances
                    )
                else:
                    for row in directory_rows:
                        for entry in row["paths"]:
                            alternatives = _directory_claim_alternatives(
                                node,
                                repo=repo_root,
                                candidate=entry["left_path"],
                                claim_path=row["claimed_path"],
                            )
                            resolution.warnings.append(
                                _directory_claim_warning_line(
                                    candidate=entry["left_path"],
                                    claimed_path=row["claimed_path"],
                                    run_id=row["run_id"],
                                    node_id=row["node"],
                                    alternatives=alternatives,
                                )
                            )
                    resolution.validation = NodeValidation(
                        ok=False,
                        findings=[
                            *resolution.validation.findings,
                            {
                                "property": "write-scope",
                                "detail": (
                                    "a declared directory write path overlaps a "
                                    "live claim; declare the files the brief "
                                    "names, or pass --accept-directory-claim"
                                ),
                            },
                        ],
                    )
            try:
                _raise_repository_scope_conflict(
                    node,
                    project=project,
                    repo=repo_root,
                    authority=resolved_authority,
                    claims=claims,
                    disregarded=resolution.warnings,
                    **_directory_claim_acceptance_kwargs(accept_directory_claim, []),
                )
            except ScopeConflict as exc:
                resolution.admission = {
                    "state": "refused",
                    "error": "scope-conflict",
                    "detail": str(exc),
                    "conflicting_run_id": exc.run_id,
                    "conflicting_node_id": exc.node_id,
                    "candidate_path": exc.candidate_path,
                    "claimed_path": exc.claimed_path,
                }
            else:
                resolution.admission = {"state": "admitted"}
    resolution.sandbox_write_roots = sandbox_write_roots
    # A dry run must reach the verdict a real dispatch reaches, so the watcher
    # gate is evaluated here too when the caller asks for it. It reads the
    # watcher state and never starts a producer — arming is the real dispatch's
    # effect, and a validating caller must not leave one behind. An attached
    # session passes, a released one is warned and proceeds, and one that never
    # registered a follower is refused, exactly as the launching path decides.
    if watch_required and not watch_override and session and str(launch_kind) == "cli":
        preview = watch_state(project, session=session)
        # Only a live watcher settles the delivery question; without a producer
        # the real dispatch may still arm one, so a validating caller leaves the
        # verdict to the launch rather than reporting a refusal it cannot know.
        if preview["watcher_live"]:
            delivery = "monitor"
            attached = bool(preview["session_attached"])
            # The real admission reports host delivery in two cases, and the
            # dry run mirrors both without its one forbidden write. A waiting
            # host attaches the session on the real request, so the delivery
            # that request would reach is read from the host's own liveness
            # rather than by asking it. A session a follower already runs is
            # host delivery only when the host's own census names that follower,
            # which is how the real dispatch tells a host's follower from one a
            # coordinator armed by hand.
            if not attached and _session_host_waiting():
                delivery = "host"
                attached = True
            elif _session_host_runs_follower(
                project, session, (preview.get("follower") or {}).get("pid")
            ):
                delivery = "host"
            admission = _watcher_delivery_admission(
                project,
                {**dict(preview), "session_attached": attached},
                session=session,
                launch_kind=str(launch_kind),
                delivery=delivery,
            )
            if admission:
                resolution.warnings.append(admission)
            resolution.watch = {
                "delivery": delivery,
                "predicted": True,
                "watcher_live": True,
                "session": session,
                "session_attached": attached,
            }
    return resolution


def _session_unreconciled_refusal(
    runs: Iterable[Mapping[str, Any]],
    peer_runs: Iterable[Mapping[str, Any]],
    grace: str,
) -> UnreconciledRuns:
    """Build an own-session refusal that keeps observed peer rows visible."""
    refusal = UnreconciledRuns(runs, grace)
    refusal.peer_runs = [dict(row) for row in peer_runs]
    if refusal.peer_runs:
        peer_lines = "\n".join(
            f"- {row['run_id']} (session {(row.get('session') or '<unknown>')!s}): visible, not counted"
            for row in refusal.peer_runs
        )
        peer_heading = (
            f"{refusal!s}\nPeer-session unreconciled runs observed but not counted "
        )
        refusal.args = (peer_heading + f"toward this session's refusal:\n{peer_lines}",)
    return refusal


def _resolved_wave_id(project: str, session: str, requested: str) -> str:
    """Return an explicit wave or the newest non-empty wave in this session."""
    if requested:
        return requested
    for record in reversed(list_live(project=project)):
        if str(record.get("session") or "") != session:
            continue
        if wave := str(record.get("wave") or ""):
            return wave
    return f"wave-{uuid.uuid4().hex}"


# How much of its own shape a node leaves a worker to decide, per declared
# input, each banded on that one axis. Declared here rather than inferred from
# a node's name, and declared as a total order over each vocabulary so a score
# can be compared against a bar that moves. A value outside a map bands at the
# middle rather than at an extreme: an unrecognised level or role is not
# evidence of a maximally open-ended node, and scoring one as though it were
# would move a dispatch off the metered lane on a typo.
SPEC_LEVEL_OPENNESS = {"exact": 0.0, "guided": 0.5, "open": 1.0}
ROLE_OPENNESS = {
    "cleanup": 0.0,
    "documentation": 0.0,
    "review": 0.0,
    "test": 0.0,
    "verify": 0.0,
    "implement": 0.5,
    "design": 1.0,
    "investigate": 1.0,
}
UNKNOWN_OPENNESS = 0.5


def open_endedness_score(node: TaskNode) -> float:
    """Score how much of its own shape a node leaves a worker to decide.

    The bar admits a node to the metered lane on this score, so it is read from
    what dispatch already holds about the node and nothing else: the
    specification level the node declares, its role, and how completely it is
    prescribed. The last is the prescription module's own judgement, read rather
    than restated -- it names every property the node fails, and the score reads
    that fraction rather than a second opinion about it.

    The prescription verdict selects the band and the declared inputs order a
    node within it. That is the arrangement in which the two constants the
    design already fixes agree exactly: the prescription module decides whether
    a node is prescribed at all, and the bar's ``PRESCRIBED_MAX`` is the top of
    the band a prescribed node scores inside. So a node failing any property
    scores above the band however fixed its level and role look, which is what
    keeps work that is not prescribed off the free lane, and a node failing none
    scores inside it however open those look, which is what makes prescription
    decidable before the window is read and a prescribed node never held at a
    full one. Below the band the third input is identically zero, so the two
    declared inputs are averaged across the band's own width; above it the
    failures take half the remaining scale and the declared inputs the other
    half, since the third input is the one the evidence behind the score is
    about and it is what the two bands are told apart by.

    The score is reported rounded, because rows are compared against one another
    and last-bit noise would make two identically shaped nodes read as
    different. A maximally prescribed node scores exactly zero and a node
    leaving everything to invent scores exactly one, so the two ends the bar
    names are the two ends of this scale rather than approximations of them.
    """
    prescribed = prescription_module.judge_prescribed(node)
    failures = [str(name) for name in prescribed.get("failures") or ()]
    properties = prescription_module.PRESCRIBED_PROPERTIES
    declared = (
        SPEC_LEVEL_OPENNESS.get(
            str(node.spec_level or "").strip().lower(), UNKNOWN_OPENNESS
        )
        + ROLE_OPENNESS.get(str(node.role or "").strip().lower(), UNKNOWN_OPENNESS)
    ) / 2.0
    if not failures:
        return round(bar_module.PRESCRIBED_MAX * declared, 6)
    failed = len(failures) / len(properties) if properties else 0.0
    return round(
        bar_module.PRESCRIBED_MAX
        + (1.0 - bar_module.PRESCRIBED_MAX) * (declared + failed) / 2.0,
        6,
    )


def _plan_impl_at_dispatch(
    project: str, plan: str, root: str | Path | None
) -> float | None:
    """Read the authored implementation fraction for promotion-time comparison.

    Imported lazily because the promotion module imports this one at module
    load; the reader lives there so the value recorded here and the value
    compared at promotion come from one implementation.
    """
    from reckon.crew.promotion import plan_impl_at

    return plan_impl_at(project, plan, root)


def _repairs_ledger_root(
    project: str, authority: Mapping[str, Any] | None
) -> Path | None:
    """Resolve the checkout owning the ledger a --repairs target is read from.

    The dispatch path already carries a resolved authority; a dry run may not,
    so the project's registered docs mount is the fallback. ``None`` leaves
    ``ledger.load`` its config-home default rather than making the refusal
    depend on a resolution that failed for an unrelated reason.
    """
    if authority is not None:
        return resolve_dispatch_ledger_root(authority)
    try:
        docs = flight.mounted_project_docs().get(project)
    except flight.FlightConfigError:
        return None
    if docs is None:
        return None
    return docs.parent.resolve()


def _require_repairs_target(
    project: str, repairs: str, *, authority: Mapping[str, Any] | None
) -> None:
    """Refuse a --repairs target that is not a promoted run of this project.

    A repair declares that the plan movement its predecessor produced carries
    forward, so the named run must already be in this project's ledger — the
    bookkeeping it inherits exists only for a run that has landed. An unknown
    run, a run still in flight, and a run from another project are three
    distinct mistakes, refused separately so the caller reads which one it made
    rather than a generic rejection. A ledger that cannot be read is a fourth:
    the target may well be promoted in it, so refusing it as an unknown run
    would state a cause the check never established. That refusal names the
    ledger and the error the read reported.
    """
    target = str(repairs or "").strip()
    if not target:
        return
    root = _repairs_ledger_root(project, authority)
    if _SAFE_ID.fullmatch(target):
        try:
            data, _version = ledger.load(project, root=root)
        except (ledger.LedgerError, _store.CorruptEnvelopeError) as exc:
            raise CrewError(
                f"--repairs {target!r} cannot be checked: the ledger at "
                f"{ledger.ledger_path(project, root)} could not be read ({exc}); "
                "a ledger whose history cannot be read is not proof the run is "
                "absent, so repair the ledger before dispatching a repair "
                "against it"
            ) from exc
        if any(
            isinstance(row, Mapping) and str(row.get("run_id")) == target
            for row in data.get("runs", [])
        ):
            return
        try:
            pointer = read_pointer(target)
        except CrewError:
            pointer = None
        if isinstance(pointer, Mapping):
            owner = str(pointer.get("project") or "").strip()
            if owner and owner != project:
                raise CrewError(
                    f"--repairs {target!r} belongs to project {owner!r}, not "
                    f"{project!r}; a repair is declared against this project's "
                    "own ledger"
                )
            raise CrewError(
                f"--repairs {target!r} is not yet promoted in project "
                f"{project!r}; a repair names a run that has already landed, "
                "so retire it with `reckon crew complete` first"
            )
    raise CrewError(
        f"--repairs {target!r} names no run in project {project!r}'s ledger; the "
        "promoted run this dispatch would repair does not exist"
    )


def _lane_allowance_unknown(detail: str) -> dict[str, Any]:
    """The allowance decision when no slot figure and no headroom could be read."""
    return {
        "state": "unknown",
        "allowance": None,
        "source": "none",
        "rests_on_observed_window": False,
        "held": False,
        "verdict": _lane_document.UNKNOWN,
        "headroom": None,
        "session": "",
        "reason": detail,
        "detail": detail,
    }


# The router averages its slot arithmetic over a window it reports as
# ``observed_seconds``, and it reports that window from its first reading. A
# slot figure is therefore used only when that window is positive: a zero or
# negative figure states that no window has been observed, and a figure resting
# on no history is read exactly as a block that states no window at all, so the
# allowance falls back to headroom, which needs none.
def _lane_worker_allowance(document: object, *, session: str) -> dict[str, Any]:
    """Choose the extra-worker allowance the lane's router grants this session.

    The router's own arithmetic is the authority and its own preference orders
    the choice, most specific first: the session's share from the admission
    block's ``sessions`` map when that map lists this session; the
    ``new_session_worker_slots`` share when the map is present and does not
    list it; the global ``worker_slots``; and, only when no slot figure is
    published, the request ``headroom`` read as a worker count. A slot figure
    is used as the router published it once the block states a positive window
    it was averaged over; a window of zero or below is no history, so the
    allowance falls back to headroom, which needs none, exactly as it does when
    the block states no window at all.

    An allowance of zero or less *holds*: the router has granted no room and
    the reason names the router's own verdict. An allowance that could not be
    read holds nothing, because absence of a signal is not exhaustion. Reckon
    does no fairness arithmetic of its own: every figure carried here is one
    the router published.

    The ``source`` label is prose for a reader. A caller that must decide
    *whether* the figure rests on observed history reads
    ``rests_on_observed_window`` instead: the structured field is true exactly
    when the allowance was taken from a router slot figure inside the block
    that states the window it was averaged over, and it stays true for every
    spelling of the label, so no caller needs to match the label's text.
    """
    reading = _lane_document.read_lane_document(document)
    admission = _lane_document.read_lane_admission(document)
    headroom = _metric_number(reading.get("headroom"))
    verdict = str(reading.get("admission_verdict") or _lane_document.UNKNOWN)
    verdict_reason = str(reading.get("admission_reason") or "")
    session_id = str(session or "").strip()

    observed = admission.get(_lane_document.ADMISSION_OBSERVED_SECONDS_KEY)
    history_is_trusted = (
        isinstance(observed, (int, float))
        and not isinstance(observed, bool)
        and observed > 0
    )

    allowance: int | float | None = None
    source = "none"
    if admission.get("present") and history_is_trusted:
        sessions_present = bool(admission.get("sessions_present"))
        listed = (
            admission["sessions"].get(session_id)
            if sessions_present and session_id
            else None
        )
        if listed is not None:
            share = _metric_number(listed.get("worker_slots"))
            if share is not None:
                allowance = share
                source = "the session's own worker slots"
        elif sessions_present:
            share = _metric_number(
                admission.get(_lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY)
            )
            if share is not None:
                allowance = share
                source = "the new-session worker slots"
        if allowance is None:
            share = _metric_number(
                admission.get(_lane_document.ADMISSION_WORKER_SLOTS_KEY)
            )
            if share is not None:
                allowance = share
                source = "the global worker slots"
    # Captured before the headroom fallback: only a figure taken inside the
    # trusted-window block above rests on observed history, and headroom --
    # which needs no window -- never does.
    rests_on_observed_window = allowance is not None

    if allowance is None and headroom is not None:
        allowance = headroom
        source = "the request headroom"

    if allowance is None:
        detail = str(admission.get("detail") or reading.get("detail") or "").strip()
        detail = detail or "no worker-slot figure and no headroom were published"
        return _lane_allowance_unknown(detail) | {"session": session_id}

    held = allowance <= 0
    grant = (
        f"the lane's router grants {allowance:g} extra workers to session "
        f"{session_id or 'unidentified'} ({source})"
    )
    if held:
        reason = f"{grant}; the router's own verdict is {verdict}"
        if verdict_reason and verdict_reason != _lane_document.UNKNOWN:
            reason = (
                f"{grant}; the router's own verdict is {verdict} — {verdict_reason}"
            )
        state = "held"
    else:
        reason = grant
        detail = (
            reason
            if admission.get("present")
            else f"{reason}; no admission block was published"
        )
        state = "measured"
    return {
        "state": state,
        "allowance": allowance,
        "source": source,
        "rests_on_observed_window": rests_on_observed_window,
        "held": held,
        "verdict": verdict,
        "headroom": headroom,
        "session": session_id,
        "reason": reason,
        "detail": reason if held else detail,
    }


def _dispatch_lane_allowance(
    backend: Mapping[str, Any], *, session: str
) -> dict[str, Any] | None:
    """Read the resolved lane's published allowance for this coordinator session.

    A backend may declare ``lane_document``, the local JSON the lane publishes
    about itself. The document is resolved through the shared lane reader and
    the allowance is chosen from the router's own figures. An absent
    declaration returns ``None`` -- a lane that publishes nothing has nothing
    to hold on -- and a document that cannot be read or parsed resolves to an
    unknown allowance that holds nothing, because absence of a signal is not
    exhaustion.
    """
    declared = backend.get("lane_document")
    if not declared:
        return None
    path = Path(str(declared)).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _lane_allowance_unknown(
            f"lane document {str(path)!r} cannot be read — {exc}"
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return _lane_allowance_unknown(
            f"lane document {str(path)!r} is not valid JSON — {exc}"
        )
    if not isinstance(payload, Mapping):
        return _lane_allowance_unknown(
            f"lane document {str(path)!r} is not a JSON object"
        )
    return _lane_worker_allowance(payload, session=session)


def _carry_declared_gate_documents(
    backend: dict[str, Any],
    record: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> None:
    """Use the current lane's gate declarations when rebuilding a resumed run."""
    configured = ((config or {}).get("backends") or {}).get(
        str(record.get("backend") or "")
    )
    if not isinstance(configured, Mapping):
        return
    for key in ("gate_document", "lane_document"):
        if configured.get(key):
            backend[key] = configured[key]

from .dispatch_admission import (  # noqa: E402
    LanePaused,
    ORCHESTRATOR_LANE_DECLARATION_KEY,
    _LANE_GATE_WAITING_STATES,
    _brief_digest,
    _brief_record,
    _dispatch_fleet_gate,
    _dispatch_lane_advisory,
    _dispatch_lane_gate,
    _dispatch_lane_observation,
    _dispatch_lane_reading,
    _dispatch_orchestrator_lane_stop,
    _lane_advisory_cheaper_lane,
    _lane_advisory_ledger_runs,
    _lane_declaration_evidence,
    _lane_declaration_finding,
    _metric_number,
    _orchestrator_lane_stop_line,
    _path_is_tmpfs,
    _require_write_paths_in_authority,
    _resolved_token_budget,
    _resolved_write_paths,
    _sandbox_reachability,
    _unmetered_dispatch_alternatives,
    check_placement_requirements,
)

from .dispatch_claims import (  # noqa: E402
    _can_write_worktree,
    _directory_claim_acceptance_kwargs,
    _directory_claim_acceptance_line,
    _directory_claim_alternatives,
    _directory_claim_warning_line,
    _grant_landing_write_paths,
    _live_conflict_is_a_directory_claim,
    _live_conflict_rows,
    _raise_repository_scope_conflict,
    _repository_scope_claims,
)

from .dispatch_launch import (  # noqa: E402
    require_worker_scratch_headroom,
)

from .dispatch_peer import (  # noqa: E402
    _stamp_agent_display,
)

from .dispatch_picker import (  # noqa: E402
    _picker_fallback,
    _picker_refusal_reasons,
    _reportable_picker_selection,
    resolve_dispatch_route,
)

from .dispatch_sections import (  # noqa: E402
    _dispatch_section_routing,
    _done_when_plan_overlap_warning,
    _section_routing_evidence,
    project_mount_repository,
    resolve_project_repository,
)

from .dispatch_watch import (  # noqa: E402
    _session_host_runs_follower,
    _session_host_waiting,
    _watcher_delivery_admission,
)
