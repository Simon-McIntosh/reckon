"""Compose paired and shadow dispatch comparisons."""

from __future__ import annotations

from . import _unbound_dispatch_call
from ._imports import (
    Any,
    CrewError,
    Iterable,
    Mapping,
    Path,
    TaskNode,
    _agent_configuration,
    _resolve_commit,
    dataclasses,
    format_refusal,
    ledger,
    new_run_id,
    resolve_project_repository,
    resolve_role,
    resolve_role_override,
    resolved_time_budget,
    run_dir,
    shadow_worktree_session,
    uuid,
)

# The facade binds cyclic calls after all concerns have been imported.
dispatch = _unbound_dispatch_call
plan_dispatch = _unbound_dispatch_call

_FACADE_BINDINGS = ("dispatch", "plan_dispatch")


def shadow_source(
    run_id: str,
    *,
    repo: str | Path,
) -> dict[str, Any]:
    """Resolve one committed primary and reconstruct its shadow node."""
    from reckon.crew.routing import repository_projects

    repo_root = Path(repo).resolve()
    projects = repository_projects(repo_root)
    if not projects:
        state_root = repo_root / "docs" / "state"
        projects = (
            tuple(
                sorted(
                    path.name
                    for path in state_root.iterdir()
                    if path.is_dir() and (path / "crew.json").is_file()
                )
            )
            if state_root.is_dir()
            else ()
        )
    matches = [
        (project, record)
        for project in projects
        for record in ledger.runs(project, root=repo_root)
        if str(record.get("run_id") or "") == run_id
    ]
    if not matches:
        raise CrewError(
            format_refusal(
                "D20",
                f"run {run_id!r} is not a committed ledger record in repository "
                f"{repo_root}",
            )
        )
    if len(matches) > 1:
        raise CrewError(
            format_refusal(
                "D20",
                f"run {run_id!r} appears in more than one project ledger in "
                f"{repo_root}",
            )
        )
    project, primary = matches[0]
    lineage = primary.get("lineage")
    if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
        raise CrewError(
            format_refusal(
                "D20",
                f"run {run_id!r} is itself a shadow and cannot be a shadow parent",
            )
        )
    agent = primary.get("agent")
    if not isinstance(agent, Mapping) or not agent:
        raise CrewError(
            format_refusal(
                "D20",
                f"committed run {run_id!r} has no recorded agent configuration; "
                "the shadow cannot inherit a configuration without guessing",
            )
        )
    definition = primary.get("node_definition")
    if not isinstance(definition, Mapping):
        raise CrewError(
            format_refusal(
                "D20",
                f"committed run {run_id!r} has no stored node definition and cannot "
                "be shadowed without re-authoring its contract",
            )
        )
    required = ("id", "goal", "plan", "done_when", "write_paths")
    missing = [name for name in required if not definition.get(name)]
    if missing:
        raise CrewError(
            format_refusal(
                "D20",
                f"committed run {run_id!r} has an incomplete stored node definition: "
                + ", ".join(missing),
            )
        )
    base_sha = str(primary.get("base_sha") or "")
    if not base_sha:
        raise CrewError(
            format_refusal("D20", f"committed run {run_id!r} records no base_sha")
        )
    node = TaskNode(
        id=str(definition["id"]),
        goal=str(definition["goal"]),
        plan=str(definition["plan"]),
        section=str(definition.get("section") or ""),
        brief=str(definition.get("brief") or ""),
        brief_sha256=str(definition.get("brief_sha256") or ""),
        brief_path=str(definition.get("brief_path") or ""),
        role=str(definition.get("role") or primary.get("role") or "implement"),
        spec_level=str(definition.get("spec_level") or primary.get("spec_level") or ""),
        done_when=str(definition["done_when"]),
        write_paths=[str(path) for path in definition.get("write_paths") or ()],
        regenerate_paths=[
            str(path) for path in definition.get("regenerate_paths") or ()
        ],
        anchor_paths=[str(path) for path in definition.get("anchor_paths") or ()],
        negative_control=str(definition.get("negative_control") or ""),
        estimated_hours=definition.get("estimated_hours"),
        requires_decisions=[
            str(key) for key in definition.get("requires_decisions") or ()
        ],
    )
    return {
        "project": str(project),
        "primary": dict(primary),
        "node": node,
        "base_sha": base_sha,
    }


def _shadow_dispatch_config(
    *,
    config: Mapping[str, Any],
    node: TaskNode,
    primary_agent: Mapping[str, Any],
    candidate_backend: str,
    configuration_overrides: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a candidate while retaining unmodified primary agent settings."""
    # The shadow arm resolves against the backend it names rather than against
    # whatever the role's own overlays route to: a candidate the role does not
    # select is the ordinary case, and rejecting it refused the very pairing the
    # flag exists to start. The role's overlay still applies on top of the named
    # backend, so inherited effort and sandbox are unchanged. A candidate no
    # layer defines is the one remaining refusal, and its text comes from the
    # shared resolver so the defined-backend list stays in one place.
    try:
        _resolved_backend, candidate = resolve_role_override(
            config, node.role, node.spec_level, candidate_backend
        )
    except CrewError as exc:
        raise CrewError(format_refusal("D21", str(exc))) from exc

    explicit = {str(config_key) for config_key in configuration_overrides}
    effective = dict(candidate)
    for config_key in ("effort", "sandbox"):
        if config_key not in explicit:
            effective[config_key] = primary_agent.get(config_key)

    shadow_agent = _agent_configuration(
        candidate_backend, str(effective.get("launch") or ""), effective
    )
    substituted: dict[str, dict[str, Any]] = {}
    inherited: dict[str, Any] = {}
    for config_key in ("backend", "launch", "model", "effort", "sandbox"):
        before = primary_agent.get(config_key)
        after = shadow_agent.get(config_key)
        via = (
            "backend"
            if config_key == "backend"
            else "override"
            if config_key in explicit
            else ""
        )
        if config_key in ("launch", "model") and before != after:
            via = "backend"
        if via:
            substituted[config_key] = {
                "primary": before,
                "shadow": after,
                "via": via,
            }
        else:
            inherited[config_key] = after

    override_evidence: dict[str, dict[str, Any]] = {}
    backend_layer = config.get("backends", {}).get(candidate_backend, {})
    role_layer = config.get("roles", {}).get(node.role, {})
    level_layer = (
        role_layer.get("by_spec_level", {}).get(node.spec_level, {})
        if isinstance(role_layer, Mapping)
        else {}
    )
    for config_key in sorted(explicit):
        layers = {
            name: layer[config_key]
            for name, layer in (
                ("backend", backend_layer),
                ("role", role_layer),
                ("spec_level", level_layer),
            )
            if isinstance(layer, Mapping) and config_key in layer
        }
        override_evidence[config_key] = {
            "layers": layers,
            "resolved": shadow_agent.get(config_key, effective.get(config_key)),
        }

    shadow_config = dict(config)
    backends = dict(config.get("backends") or {})
    backends[candidate_backend] = effective
    shadow_config["backends"] = backends
    roles = dict(config.get("roles") or {})
    roles[node.role] = {"backend": candidate_backend}
    shadow_config["roles"] = roles
    return shadow_config, {
        "substituted": substituted,
        "inherited": inherited,
        "overrides": override_evidence,
        "resolved": {"effort": shadow_agent.get("effort")},
    }


def _refuse_arms_that_resolve_alike(comparison: Mapping[str, Any]) -> None:
    """Refuse a pair whose two arms resolve to one backend and one model.

    A pair exists to compare two arms, so two arms that resolve to one backend
    and one model compare nothing and spend that lane twice. The comparison
    ``_shadow_dispatch_config`` already built carries both arms' values: the
    backend is always substituted, and the model is substituted only when the
    arms differ. Equal backends with the model left in ``inherited`` is
    therefore the alike case. A pair whose arms share a backend but run
    different models still compares the two models and is not refused.
    """
    substituted = comparison.get("substituted") or {}
    alike_backend = substituted.get("backend") or {}
    if (
        alike_backend.get("primary") is not None
        and alike_backend.get("primary") == alike_backend.get("shadow")
        and "model" not in substituted
    ):
        shared_backend = alike_backend.get("primary")
        shared_model = (comparison.get("inherited") or {}).get("model")
        raise CrewError(
            format_refusal(
                "D21",
                f"both arms of the pair resolve to backend {shared_backend!r} "
                f"with model {shared_model!r}, so the pair would compare "
                "nothing; the pair backend must differ from the lane the "
                "primary runs on",
            )
        )


def shadow(
    run_id: str,
    *,
    candidate_backend: str,
    config: Mapping[str, Any],
    repo: str | Path,
    session: str,
    wave: str = "",
    member: str = "",
    configuration_overrides: Iterable[str] = (),
    dry_run: bool = False,
    launcher=None,
) -> dict[str, Any]:
    """Dispatch a committed node at its original base as isolated evidence."""
    source = shadow_source(run_id, repo=repo)
    project = source["project"]
    node = source["node"]
    base_sha = source["base_sha"]
    primary = source["primary"]
    dispatching_session = str(session)
    if not dispatching_session:
        raise CrewError(
            format_refusal(
                "D20", "shadow needs a dispatching session on its request or primary"
            )
        )
    wave_id = str(wave or primary.get("wave") or "")
    primary_agent = primary["agent"]
    explicit = {str(config_key) for config_key in configuration_overrides}
    shadow_config, comparison = _shadow_dispatch_config(
        config=config,
        node=node,
        primary_agent=primary_agent,
        candidate_backend=candidate_backend,
        configuration_overrides=explicit,
    )
    _backend_name, shadow_backend = resolve_role(
        shadow_config, node.role, node.spec_level
    )
    role_time_budget = resolved_time_budget(shadow_config, shadow_backend)
    primary_time_budget = str(primary.get("time_budget") or "")
    if "time_budget" in explicit:
        node.time_budget = role_time_budget
        comparison["substituted"]["time_budget"] = {
            "primary": primary_time_budget or None,
            "shadow": role_time_budget,
            "via": "override",
        }
    elif primary_time_budget:
        node.time_budget = primary_time_budget
        comparison["inherited"]["time_budget"] = primary_time_budget
    else:
        node.time_budget = role_time_budget
        comparison["inherited"]["time_budget"] = role_time_budget
        comparison["fallbacks"] = {
            "time_budget": {
                "source": "resolved_role_default",
                "value": role_time_budget,
            }
        }
    comparison["resolved"]["time_budget"] = node.time_budget
    if "time_budget" in explicit:
        backend_layer = config.get("backends", {}).get(candidate_backend, {})
        comparison["overrides"]["time_budget"] = {
            "layers": (
                {"backend": backend_layer["time_budget"]}
                if isinstance(backend_layer, Mapping) and "time_budget" in backend_layer
                else {}
            ),
            "resolved": node.time_budget,
        }
    worktree_component = uuid.uuid4().hex[:12]
    lineage = {
        "kind": "shadow",
        "primary_run_id": run_id,
        "worktree_component": worktree_component,
        "configuration": comparison,
    }
    if dry_run:
        resolution = plan_dispatch(
            node=node,
            config=shadow_config,
            locked_decisions=node.requires_decisions,
            peer_scopes={},
            project=project,
            repo=repo,
            base=base_sha,
            backend_override=_backend_name,
            # A shadow names its candidate backend, so the picker has nothing to
            # select and asking it would only be refused.
            route="deterministic",
        )
        return {
            "dry_run": True,
            "primary_run_id": run_id,
            "project": project,
            "base_sha": base_sha,
            "lineage": lineage,
            **resolution.as_dict(),
        }
    return dispatch(
        node=node,
        project=project,
        repo=repo,
        config=shadow_config,
        session=dispatching_session,
        wave=wave_id,
        worktree_session=shadow_worktree_session(
            run_id, _backend_name, worktree_component
        ),
        base=base_sha,
        locked_decisions=node.requires_decisions,
        peer_scopes={},
        member=member,
        launcher=launcher,
        lineage_override=lineage,
        backend_override=_backend_name,
        route="deterministic",
    )


def _paired_arm_kwargs(
    dispatch_kwargs: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """One dispatch arm's keyword surface: the caller's, minus what the arm fixes."""
    result = dict(dispatch_kwargs)
    for key in overrides:
        result.pop(key, None)
    result.update(overrides)
    return result


def dispatch_pair(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path | None,
    config: Mapping[str, Any],
    session: str,
    pair_backend: str,
    dry_run: bool = False,
    **dispatch_kwargs: Any,
) -> dict[str, Any]:
    """Dispatch one node twice from one base: a primary arm and a shadow arm.

    A shadow is meaningful only when it never read its primary's answer, and a
    primary that has already landed leaves that answer in the project's main
    checkout and in git history where no fence reaches. So the pair is started
    together instead: both arms are cut from one base before either can land,
    which is the channel this removes rather than hides.

    Both run ids, both run directories and both worktree paths are fixed here,
    before either arm launches, so each arm's fence is composed from its
    partner's *allocation* rather than from a live pointer or a ledger row the
    partner does not have yet: a paired primary is uncommitted until it is
    promoted, and a shadow's pointer is written only at the end of its own
    dispatch. The masks are resolved through
    ``promotion_scope._allocated_read_surfaces``, which names a partner's run
    directory, worktree and any stored review records.

    The primary arm is routed as any dispatch is, and its resolved agent
    configuration is read from its own planning resolution rather than from a
    committed ledger row, so a paired primary is never refused for being
    uncommitted. The shadow arm runs on the named backend at the primary's
    inherited effort and sandbox, exactly as ``crew shadow`` configures one.
    """
    from reckon.crew.promotion_scope import _allocated_read_surfaces
    from reckon.crew.routing import _launch_worktree_path

    candidate_backend = str(pair_backend or "").strip()
    if not candidate_backend:
        raise CrewError(
            format_refusal("D20", "--pair-backend needs a backend to run the shadow on")
        )
    repo_root = resolve_project_repository(project, repo)
    base = str(dispatch_kwargs.get("base") or "HEAD")
    # Both ids are minted from one clock read each, so the pair is ordered and
    # neither arm's id is derived from the other's: a shadow names its primary
    # through its lineage rather than through its id, so the primary's run id
    # never reaches the shadow's worktree path or composed prompt.
    primary_run_id = new_run_id(node.id)
    shadow_run_id = new_run_id(node.id)

    locked_decisions = dispatch_kwargs.get("locked_decisions") or ()
    peer_scopes = dispatch_kwargs.get("peer_scopes") or {}
    route = dispatch_kwargs.get("route")
    picker_selection = dispatch_kwargs.get("picker_selection")
    planning_kwargs: dict[str, Any] = {
        "config": config,
        "locked_decisions": locked_decisions,
        "peer_scopes": peer_scopes,
        "project": project,
        "repo": repo_root,
        "base": base,
        "execution_override": dispatch_kwargs.get("execution_override", False),
        "orchestrator_lane_reason": dispatch_kwargs.get("orchestrator_lane_reason"),
        "local": dispatch_kwargs.get("local", False),
        "member": dispatch_kwargs.get("member", ""),
        "allow_unreviewed_plan": dispatch_kwargs.get("unreviewed_plan_override", False),
        "session": session,
        "route": route,
        "picker_selection": picker_selection,
    }
    # Each arm resolves its own manifest under its own run directory: a node
    # helper's default manifest path is one path, and two workers writing it
    # would each read the other's delivery. The pairing is what makes a fresh
    # per-arm manifest necessary, so it is fixed here rather than left to the
    # caller to remember.
    primary_node = dataclasses.replace(node, manifest_path="")
    primary_plan = plan_dispatch(
        node=primary_node,
        run_id=primary_run_id,
        backend_override=dispatch_kwargs.get("backend_override"),
        default_backend_override=dispatch_kwargs.get("default_backend_override"),
        **planning_kwargs,
    )
    primary_agent = primary_plan.as_dict()["agent"]
    shadow_node = dataclasses.replace(node, manifest_path="")
    shadow_config, comparison = _shadow_dispatch_config(
        config=config,
        node=shadow_node,
        primary_agent=primary_agent,
        candidate_backend=candidate_backend,
        configuration_overrides=(),
    )
    # A pair refused here spends nothing: the check is before the dry-run
    # return and before any run directory or worktree exists.
    _refuse_arms_that_resolve_alike(comparison)
    shadow_backend_name, _shadow_backend = resolve_role(
        shadow_config, node.role, node.spec_level
    )
    worktree_component = uuid.uuid4().hex[:12]
    shadow_session = shadow_worktree_session(
        primary_run_id, shadow_backend_name, worktree_component
    )
    primary_worktree = str(_launch_worktree_path(repo_root, session, node.id))
    shadow_worktree = str(_launch_worktree_path(repo_root, shadow_session, node.id))
    # The shadow's worktree is created by the shadow's own dispatch, after the
    # primary has launched, and the fence skips a masked path that does not
    # exist yet — so masking that path alone would hide nothing while the
    # primary runs. The session directory the worktree is created under is the
    # mask instead: created on the launch path below, it is a directory the
    # primary's namespace can hide, and hiding it covers whatever the shadow
    # writes beneath it later. Only the parent exists, so the shadow's own
    # `git worktree add` still finds its path free.
    shadow_session_dir = _launch_worktree_path(
        repo_root, shadow_session, node.id
    ).parent
    lineage: dict[str, Any] = {
        "kind": "shadow",
        "primary_run_id": primary_run_id,
        "worktree_component": worktree_component,
        "configuration": comparison,
    }
    primary_masks = (
        *_allocated_read_surfaces(
            run_id=shadow_run_id, worktree=shadow_worktree, project=project
        ),
        str(shadow_session_dir),
    )
    shadow_masks = _allocated_read_surfaces(
        run_id=primary_run_id, worktree=primary_worktree, project=project
    )

    if dry_run:
        shadow_plan = plan_dispatch(
            node=shadow_node,
            run_id=shadow_run_id,
            config=shadow_config,
            locked_decisions=locked_decisions,
            peer_scopes={},
            project=project,
            repo=repo_root,
            base=base,
            local=False,
            backend_override=shadow_backend_name,
            member=dispatch_kwargs.get("member", ""),
            session=session,
            route="deterministic",
            picker_selection=None,
        )
        return {
            "dry_run": True,
            "paired": True,
            "base_sha": str(_resolve_commit(repo_root, base) or ""),
            "primary_run_id": primary_run_id,
            "shadow_run_id": shadow_run_id,
            "primary_masks": list(primary_masks),
            "shadow_masks": list(shadow_masks),
            "primary": {
                **primary_plan.as_dict(),
                "worktree": primary_worktree,
                "paired_shadow_run_id": shadow_run_id,
            },
            "shadow": {
                **shadow_plan.as_dict(),
                "worktree": shadow_worktree,
                "lineage": lineage,
            },
        }

    # The run directories exist before either arm launches, so an arm's own
    # worker can write its own stream and records into its own directory, and so
    # each arm's mask names a directory that is there rather than one invented
    # later: the shadow's run directory is masked from the primary, and the
    # primary launches first. The shadow's session directory is created for the
    # same reason — a mask over an absent path hides nothing.
    for run_id in (primary_run_id, shadow_run_id):
        run_dir(run_id).mkdir(parents=True, exist_ok=True)
    shadow_session_dir.mkdir(parents=True, exist_ok=True)

    primary_record = dispatch(
        node=primary_node,
        project=project,
        repo=repo,
        config=config,
        session=session,
        **_paired_arm_kwargs(
            dispatch_kwargs,
            {
                "base": base,
                "run_id": primary_run_id,
                "worktree_session": session,
                "partner_masks": primary_masks,
                "paired_shadow_run_id": shadow_run_id,
                "backend_override": dispatch_kwargs.get("backend_override"),
                "local": dispatch_kwargs.get("local", False),
            },
        ),
    )
    shadow_record = dispatch(
        node=shadow_node,
        project=project,
        repo=repo,
        config=shadow_config,
        session=session,
        **_paired_arm_kwargs(
            dispatch_kwargs,
            {
                "base": base,
                "run_id": shadow_run_id,
                "worktree_session": shadow_session,
                "partner_masks": shadow_masks,
                "route": "deterministic",
                "picker_selection": None,
                "backend_override": shadow_backend_name,
                "default_backend_override": None,
                "lineage_override": lineage,
                "local": False,
                "peer_scopes": {},
            },
        ),
    )
    return {
        "ok": True,
        "paired": True,
        "primary_run_id": primary_run_id,
        "shadow_run_id": shadow_run_id,
        "primary": primary_record,
        "shadow": shadow_record,
    }
