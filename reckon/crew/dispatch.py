# ruff: noqa: I001, UP035, F401, PLC0414
from __future__ import annotations
import argparse as argparse
import ast as ast
import contextlib as contextlib
import ctypes as ctypes
import dataclasses as dataclasses
import errno as errno
import fcntl as fcntl
import hashlib as hashlib
import inspect as inspect
import json as json
import os as os
import re as re
import select as select
import shutil as shutil
import signal as signal
import socket as socket
import subprocess as subprocess
import sys as sys
import threading as threading
import time as time
import uuid as uuid
from collections import (
    defaultdict as defaultdict,
)
from dataclasses import (
    dataclass as dataclass,
    field as field,
)
from datetime import (
    UTC as UTC,
    datetime as datetime,
    timedelta as timedelta,
)
from pathlib import (
    Path as Path,
)
from typing import (
    Any as Any,
    Callable as Callable,
    Iterable as Iterable,
    Iterator as Iterator,
    Mapping as Mapping,
)
from reckon import (
    _backends as _backends,
    _store as _store,
    capability as capability,
    flight as flight,
    ledger as ledger,
)
from reckon.figures import rasterise_svg
from reckon._plan_html import (
    _strip_tags as _strip_tags,
    plan_headings as plan_headings,
    section_id_candidates as section_id_candidates,
    section_prose as section_prose,
)
from reckon._timestamps import (
    parse_iso as parse_iso,
    parse_utc as parse_utc,
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
from reckon.crew import (
    summary as summary,
)
from reckon.crew.fleet_supervisor import (
    REQUEST_FIFO_NAME as REQUEST_FIFO_NAME,
)
from reckon.crew.node import (
    _SAFE_ID as _SAFE_ID,
    _TERMINAL_RUN_PHASES as _TERMINAL_RUN_PHASES,
    DEFAULT_MEMBER_IDLE_WINDOW as DEFAULT_MEMBER_IDLE_WINDOW,
    NEEDS_HELP_MARKER as NEEDS_HELP_MARKER,
    BudgetHold as BudgetHold,
    CompetenceLimit as CompetenceLimit,
    CrewError as CrewError,
    NodeValidation as NodeValidation,
    PlanVisibilityError as PlanVisibilityError,
    ScopeConflict as ScopeConflict,
    TaskNode as TaskNode,
    UnreconciledRuns as UnreconciledRuns,
    WatcherRequired as WatcherRequired,
    claim_disposition as claim_disposition,
    claim_repository as claim_repository,
    done_when_warnings as done_when_warnings,
    gate_population_finding as gate_population_finding,
    member_in_flight_verdict as member_in_flight_verdict,
    negative_control_finding as negative_control_finding,
    normalize_section as normalize_section,
    parse_duration as parse_duration,
    placement_query_undeclared as placement_query_undeclared,
    placement_requirement_node_local as placement_requirement_node_local,
    placement_requirement_unmet as placement_requirement_unmet,
    refuse_member_in_flight as refuse_member_in_flight,
    repository_identity as repository_identity,
    role_may_write_repository_paths as role_may_write_repository_paths,
    validate_node as validate_node,
)
from reckon.crew.prompts import (
    compose_prompt as compose_prompt,
    domain_rules_block as domain_rules_block,
    time_fence_statement as time_fence_statement,
)
from reckon.crew.recovery import (
    REVIEW_NODE_PREFIX as REVIEW_NODE_PREFIX,
    _resolve_commit as _resolve_commit,
    resume_window_refusal as resume_window_refusal,
    stream_paths_newest_first as stream_paths_newest_first,
)
from reckon.crew.refusals import (
    format_refusal as format_refusal,
)
from reckon.crew.reserve import (
    admit_windows as reserve_admit_windows,
)
from reckon.crew.review import (
    review_store_root as review_store_root,
)
from reckon.crew.routing import (
    _agent_configuration as _agent_configuration,
    _boundary_tree_roots as _boundary_tree_roots,
    _budget_verdict as _budget_verdict,
    _competence_verdict as _competence_verdict,
    _create_worktree as _create_worktree,
    _disposable_member_id as _disposable_member_id,
    _fleet_script as _fleet_script,
    _remove_worktree as _remove_worktree,
    _repository_tree_snapshot as _repository_tree_snapshot,
    _signal_process_group as _signal_process_group,
    _workspace_roots as _workspace_roots,
    mounted_repository_projects as mounted_repository_projects,
    reap_idle_session_members as reap_idle_session_members,
    require_plan_reviewed as require_plan_reviewed,
    require_plan_section_visible as require_plan_section_visible,
    resolve_budget_fallback as resolve_budget_fallback,
    resolve_dispatch_authority as resolve_dispatch_authority,
    resolve_dispatch_ledger_root as resolve_dispatch_ledger_root,
    resolve_role as resolve_role,
    resolve_role_override as resolve_role_override,
    resolve_scope_repository as resolve_scope_repository,
    resolve_section_routing as resolve_section_routing,
    resolved_grace_multiple as resolved_grace_multiple,
    resolved_time_budget as resolved_time_budget,
    resolved_time_ceiling as resolved_time_ceiling,
    shadow_worktree_session as shadow_worktree_session,
    shared_verdict_inputs as shared_verdict_inputs,
    signal_worker as signal_worker,
)
from reckon.crew.runs import (
    WATCH_LOG_ENV as WATCH_LOG_ENV,
    _expanded_scope_paths as _expanded_scope_paths,
    _manifest_freshness as _manifest_freshness,
    _manifest_mtime_ns as _manifest_mtime_ns,
    _merge_peer_scopes as _merge_peer_scopes,
    _mutate_pointer as _mutate_pointer,
    _pointer_lock as _pointer_lock,
    _process_start_time as _process_start_time,
    _project_derivations as _project_derivations,
    _repository_relative_scope as _repository_relative_scope,
    _scopes_overlap as _scopes_overlap,
    _shared_write_paths as _shared_write_paths,
    _utc_now as _utc_now,
    _watch_arming_line as _watch_arming_line,
    _watch_attach_line as _watch_attach_line,
    _write_json as _write_json,
    capture_run_session as capture_run_session,
    crew_home as crew_home,
    delivery_roots as delivery_roots,
    existing_node_run as existing_node_run,
    list_live as list_live,
    new_run_id as new_run_id,
    placement_job_alive as placement_job_alive,
    pointer_path as pointer_path,
    process_alive as process_alive,
    QueueFull as QueueFull,
    queue_dispatch as queue_dispatch,
    read_pointer as read_pointer,
    record_process_alive as record_process_alive,
    record_stacked_dispatch as record_stacked_dispatch,
    reports_dir as reports_dir,
    run_dir as run_dir,
    scheduler_job_reason as scheduler_job_reason,
    scheduler_job_state as scheduler_job_state,
    scheduler_kill_class as scheduler_kill_class,
    watch_lock_path as watch_lock_path,
    watch_log_path as watch_log_path,
    watch_observer_alive as watch_observer_alive,
    watch_state as watch_state,
    watch_stream_path as watch_stream_path,
)


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
        target = queue_admission.resolve_waits(
            record,
            live=[*(live if live is not None else list_live()), record],
            docs_dir=Path(str(record["repo"])) / "docs",
            cache=cache if cache is not None else {},
        )
        if target != phase and queue_admission._move_pointer(
            str(queued["run_id"]), target
        ):
            phase = target
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
) -> dict[str, Any]:
    """Validate a request batch, then use dispatch's queue writer for each."""
    from reckon.crew import queue_admission
    from reckon.crew.dispatch_admission import (
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
    for node in nodes:
        findings = admission_domain_findings(
            node, project=project, repository=repo_root
        )
        refusals, _warnings = admission_refusals_and_warnings(findings)
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
    for index, node in enumerate(nodes):
        run_id = assigned_ids[node.id]
        queued, position, phase = _queue_validated_waits(
            _queued_request_record(
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
                },
                queued_at=_utc_now(),
            ),
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
    from reckon.crew import queue_admission, queue_order, runs as runs_module

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
    from reckon.crew import queue_admission, runs as runs_module

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


def _offered_local_backend(
    selection: Mapping[str, Any] | None, config: Mapping[str, Any]
) -> str | None:
    """The local backend among a picker answer's offered candidates, or ``None``.

    A picker answer lists the candidates it weighed, each marked with the lane
    it names. A hold whose offered candidates include the local lane is a wait
    for that lane, not a refusal, so the caller continues the request there. The
    local backend is the configured ``local_backend``; a candidate is local when
    the picker marked it so, or when its backend names the configured local lane.
    """
    if selection is None:
        return None
    local_backend = str(config.get("local_backend") or "").strip()
    if not local_backend:
        return None
    for candidate in selection.get("offered") or ():
        if not isinstance(candidate, Mapping):
            continue
        name = str(candidate.get("backend") or "")
        if candidate.get("local") is True or name == local_backend:
            return name or local_backend
    return None


def _overridden_hold_route_mode() -> str:
    """The route mode a hold continued onto its offered local lane records.

    Imported lazily: the picker package's ``__init__`` pulls the snapshot and
    lane-context modules, which import back into this one, so resolving the
    constant at module load would read a partially initialized dispatcher.
    """
    from reckon.crew.picker.outcomes import OVERRIDDEN_HOLD_ROUTE_MODE

    return OVERRIDDEN_HOLD_ROUTE_MODE


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


def _recorded_capability(
    resolution: Any,
    *,
    config: Mapping[str, Any],
    node: TaskNode,
    project: str,
    repo_root: Path,
    authority: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The capability requirement the section routing resolved for this run.

    A route that resolved its lane from the section's own record already
    carries the capability, so it is read back rather than resolved a second
    time. An explicit backend bypasses section routing for the lane while the
    plan's declaration still stands, so the section routing is resolved for the
    record alone there; a declaration that cannot be read leaves the field null
    rather than failing a dispatch that has already chosen its lane.
    """
    evidence = getattr(resolution, "section_routing", None)
    if isinstance(evidence, Mapping) and evidence.get("failure") is None:
        capability = evidence.get("capability")
        if capability:
            return dict(capability)
    resolved = _dispatch_section_routing(
        config, node=node, project=project, repo=repo_root, authority=authority
    )
    if isinstance(resolved, Mapping) and resolved.get("failure") is None:
        capability = resolved.get("capability")
        if capability:
            return dict(capability)
    return None


def _resolved_suite_gate(
    suite_command: str | None,
    *,
    node: TaskNode,
    worktree: str,
) -> tuple[str, str]:
    """The armed suite command resolved for this node, and whether it is armed.

    Promotion resolves a scoped declaration against the node's own fenced test
    files, so a worker that is told a different command measures a different
    suite and its pair is refused. The resolution is therefore taken from
    promotion's own substitution rather than reimplemented here, over the same
    two facts promotion reads: the declared template and the node's write
    scope within the run's tree.

    A scope holding no test file resolves to nothing, and the gate is then
    reported ``unarmed`` rather than as an empty command, which reads as a
    command no one could run. A project that declares no command at all
    reports no gate state, because an undeclared project is a different fact
    from a declaration this node's scope empties.
    """
    if not suite_command:
        return "", ""
    from reckon.crew.promotion_records import _scoped_suite_command

    resolved = _scoped_suite_command(
        {
            "suite_command": suite_command,
            "worktree": worktree,
            "node": {"write_paths": list(node.write_paths)},
        }
    )
    return (resolved, "armed") if resolved else ("", "unarmed")


@contextlib.contextmanager
def _request_claim_restored_on_refusal(run_id, request):
    """Return an admitted request to its waiting phase without an absent pointer."""
    try:
        yield
    except Exception:
        from reckon.crew.promotion import discard_recorded

        if discard_recorded(run_id):
            _release_launch_claim(run_id)
        else:
            _write_json(pointer_path(run_id), request)
        raise


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

    An unreconciled-run override is narrower: it waives only the terminal
    backlog observed by this dispatch. The exact runs and resolving commands
    are copied onto the new record so the exception survives its command line.

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
    # A hold whose offered candidates include the local lane is a wait for that
    # lane, not a refusal: the request is validated and continued there, and the
    # lane allowance then launches it if the lane has room or queues it. The hold
    # is kept aside so the run still records Jev's answer, and the picker-hold
    # record is written with the run it became.
    held_selection: dict[str, Any] | None = None
    if (
        picker_selection is not None
        and resolve_dispatch_route(config, route) == "picker"
        and picker_selection.get("action") == "hold"
    ):
        held_local_backend = _offered_local_backend(picker_selection, config)
        lifted_serving_lane = any(
            isinstance(candidate, Mapping)
            and not candidate.get("local")
            and not candidate.get("reasons")
            and candidate.get("worker_slots") != 0
            and (candidate.get("congestion") or {}).get("lift")
            for candidate in picker_selection.get("offered") or ()
        )
        if held_local_backend and not ((due or urgency) and lifted_serving_lane):
            held_selection = picker_selection
            picker_selection = {
                **picker_selection,
                "action": "route",
                "backend": held_local_backend,
                "local": True,
            }
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

    dispatch_options = dict(dispatch_options or {})
    dispatch_options.setdefault("route", route or resolution.route)
    if not admission_launch:
        if picker_selection is not None:
            dispatch_options["picker_selection"] = dict(
                held_selection or picker_selection
            )
        if caller_named_route:
            dispatch_options["named_route"] = {
                "backend": resolution.backend,
                "set_by": "dispatch",
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
        raise CompetenceLimit(competence)

    fences = config.get("fences") or {}
    unreconciled_grace = str(fences.get("unreconciled_run_grace") or "")
    from reckon.crew.recovery import (
        _partition_session_rows,
        overdue_unreconciled_runs,
    )

    project_unreconciled = overdue_unreconciled_runs(
        project=project,
        grace=unreconciled_grace,
    )
    unreconciled, peer_unreconciled = _partition_session_rows(
        project_unreconciled, session
    )
    if unreconciled and not unreconciled_override:
        raise _session_unreconciled_refusal(
            unreconciled, peer_unreconciled, unreconciled_grace
        )
    waiver = (
        {
            "requested": True,
            # The waived backlog is copied as a field rather than referenced, so
            # the record states exactly which runs the exception covered rather
            # than pointing at the ledger's state in a later moment.
            "grace": unreconciled_grace,
            "waived_runs": unreconciled,
        }
        if unreconciled_override
        else None
    )
    # A waived plan review is narrower than the unreconciled waiver: it excuses
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
                state.get("headroom") == "known"
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
                if caller_named_route and not admission_launch:
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
    if stored_request and not member_refuses:
        validated_refs, wait_projects, waiting = queue_admission.dispatch_waits(
            node,
            repo_root=repo_root,
            project=project,
            run_id=str((existing or {}).get("run_id") or run_id),
        )
    if waiting:
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
            ),
            refs=validated_refs,
            wait_projects=wait_projects,
            max_requests=flight.queue_max_requests(config),
        )
        held = LaneHeld(
            {
                "detail": "waiting on " + ", ".join(waiting),
                "reason": "this section's waits have not landed",
            }
        )
        held.queued = {
            "run_id": str(queued["run_id"]),
            "position": position,
            # A request the writer could not move off ``queued`` is reported
            # as the phase it really holds, so the answer never claims a wait
            # the pointer does not carry.
            "phase": phase,
            "waits": list(validated_refs),
            "reason": "this section's waits have not landed",
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
        wave_id = _resolved_wave_id(project, session, wave)

    worktree: dict[str, Any] | None = None
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
        worktree = _create_worktree(repo_root, worktree_identity, node.id, base)
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
            suite_command, node=node, worktree=worktree["path"]
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
            suite_command=suite_command_resolved,
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
                # composition, so it is rewritten here from the composed text —
                # the text every dialect carries, since every dialect takes its
                # prompt on stdin. A fresh figure review on such a lane is then
                # handed its image paths by the file it actually reads.
                prompt_path.write_text(plan.stdin_text, encoding="utf-8")
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
                    spawned_pid = _start_supervisor(spec_path, directory, run_id)
                else:
                    _require_fleet_gate_open()
                    _refuse_a_discarded_launch(run_id)
                    spawned_pid = launcher(
                        plan,
                        log_path=log_path,
                        stderr_path=stderr_path,
                        prompt_path=prompt_path,
                    )
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

            # The supervisor may already have advanced this pointer's phase.
            # Merge the launch identity under the same lock as that advance so
            # neither writer replaces the other's newer fields with its copy.
            def attach_launch_identity(pointer: dict[str, Any]) -> dict[str, Any]:
                pointer["pid"] = spawned_pid
                pointer["pid_start_time"] = spawned_start_time
                if "repository_tree_snapshot" in record:
                    pointer["repository_tree_snapshot"] = record[
                        "repository_tree_snapshot"
                    ]
                return pointer

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
                _write_json(pointer_path(run_id), request_pointer)
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
    return record


from .dispatch_accounting import (  # noqa: E402
    _ancestor_processes as _ancestor_processes,
    _charged_claude_tokens as _charged_claude_tokens,
    _claude_authoring_turn as _claude_authoring_turn,
    _codex_authoring_turn as _codex_authoring_turn,
    _coordinator_accounting as _coordinator_accounting,
    _coordinator_runtime as _coordinator_runtime,
    _jsonl_events as _jsonl_events,
    _latest_transcript as _latest_transcript,
)

from .dispatch_admission import (  # noqa: E402
    LANE_GATE_READ_DEADLINE_SECONDS as LANE_GATE_READ_DEADLINE_SECONDS,
    LaneHeld as LaneHeld,
    LanePaused as LanePaused,
    ORCHESTRATOR_LANE_DECLARATION_KEY as ORCHESTRATOR_LANE_DECLARATION_KEY,
    ORCHESTRATOR_LANE_STOP_SEVERITY as ORCHESTRATOR_LANE_STOP_SEVERITY,
    _GateReadDeadline as _GateReadDeadline,
    _LANE_ADVISORY_DEFAULT_HORIZON_SECONDS as _LANE_ADVISORY_DEFAULT_HORIZON_SECONDS,
    _LANE_ADVISORY_MINIMUM_SAMPLES as _LANE_ADVISORY_MINIMUM_SAMPLES,
    _LANE_GATE_CONFIG_PATH_KEY as _LANE_GATE_CONFIG_PATH_KEY,
    _LANE_GATE_PAUSED_KEY as _LANE_GATE_PAUSED_KEY,
    _LANE_GATE_REASON_KEY as _LANE_GATE_REASON_KEY,
    _LANE_GATE_WAITING_STATES as _LANE_GATE_WAITING_STATES,
    _REQUIREMENT_PROBE_TIMEOUT_SECONDS as _REQUIREMENT_PROBE_TIMEOUT_SECONDS,
    _actionable_budget_hold as _actionable_budget_hold,
    _brief_digest as _brief_digest,
    _brief_record as _brief_record,
    _brief_text as _brief_text,
    _dispatch_fleet_gate as _dispatch_fleet_gate,
    _dispatch_lane_advisory as _dispatch_lane_advisory,
    _dispatch_lane_gate as _dispatch_lane_gate,
    _dispatch_lane_observation as _dispatch_lane_observation,
    _dispatch_lane_reading as _dispatch_lane_reading,
    _dispatch_orchestrator_lane_stop as _dispatch_orchestrator_lane_stop,
    _endpoint_answers as _endpoint_answers,
    _fence_write_roots as _fence_write_roots,
    _fleet_gate_text_reader as _fleet_gate_text_reader,
    _gate_path_check as _gate_path_check,
    _gate_paths_agree as _gate_paths_agree,
    _gate_rows_from_payload as _gate_rows_from_payload,
    _gate_text_reader as _gate_text_reader,
    _is_node_local_path as _is_node_local_path,
    _lane_advisory_cheaper_lane as _lane_advisory_cheaper_lane,
    _lane_advisory_costs as _lane_advisory_costs,
    _lane_advisory_horizon_seconds as _lane_advisory_horizon_seconds,
    _lane_advisory_instant as _lane_advisory_instant,
    _lane_advisory_lane as _lane_advisory_lane,
    _lane_advisory_ledger_runs as _lane_advisory_ledger_runs,
    _lane_declaration_evidence as _lane_declaration_evidence,
    _lane_declaration_finding as _lane_declaration_finding,
    _lane_reading_carry as _lane_reading_carry,
    _lane_reading_unknown as _lane_reading_unknown,
    _live_runs_across_backends as _live_runs_across_backends,
    _live_runs_on_backend as _live_runs_on_backend,
    _metric_number as _metric_number,
    _orchestrator_lane_discharge_candidates as _orchestrator_lane_discharge_candidates,
    _orchestrator_lane_stop_line as _orchestrator_lane_stop_line,
    _path_is_tmpfs as _path_is_tmpfs,
    _placement_requirement_targets as _placement_requirement_targets,
    _read_text_under_deadline as _read_text_under_deadline,
    _refuse_against_the_bookend_reserve as _refuse_against_the_bookend_reserve,
    _refuse_over_concurrency_ceiling as _refuse_over_concurrency_ceiling,
    _refuse_over_reservation_roster as _refuse_over_reservation_roster,
    _require_fleet_gate_open as _require_fleet_gate_open,
    _require_write_paths_in_authority as _require_write_paths_in_authority,
    _resolved_token_budget as _resolved_token_budget,
    _resolved_write_paths as _resolved_write_paths,
    _sandbox_reachability as _sandbox_reachability,
    _store_brief as _store_brief,
    _unmetered_dispatch_alternatives as _unmetered_dispatch_alternatives,
    check_placement_requirements as check_placement_requirements,
    fleet_gate_path as fleet_gate_path,
)

from .dispatch_claims import (  # noqa: E402
    DirectoryClaimConflict as DirectoryClaimConflict,
    NODE_DISPATCH_CLAIM_DIRECTORY as NODE_DISPATCH_CLAIM_DIRECTORY,
    RACING_WINNER_POLL_SECONDS as RACING_WINNER_POLL_SECONDS,
    _NODE_CLAIM_ATTEMPTS as _NODE_CLAIM_ATTEMPTS,
    _NODE_CLAIM_EMPTY_RECORD_STALE_SECONDS as _NODE_CLAIM_EMPTY_RECORD_STALE_SECONDS,
    _NODE_CLAIM_RECORD_ATTEMPTS as _NODE_CLAIM_RECORD_ATTEMPTS,
    _NODE_CLAIM_RECORD_INTERVAL_SECONDS as _NODE_CLAIM_RECORD_INTERVAL_SECONDS,
    _NodeDispatchClaim as _NodeDispatchClaim,
    _RepositoryScopeClaim as _RepositoryScopeClaim,
    _UNLAUNCHED_CLAIM_PHASES as _UNLAUNCHED_CLAIM_PHASES,
    _absent_path_names_a_directory as _absent_path_names_a_directory,
    _can_write_worktree as _can_write_worktree,
    _candidate_scope_entries as _candidate_scope_entries,
    _claim_holder_is_alive as _claim_holder_is_alive,
    _claim_node_dispatch as _claim_node_dispatch,
    _claim_released_on_refusal as _claim_released_on_refusal,
    _compose_dispatch_prompt as _compose_dispatch_prompt,
    _directory_claim_acceptance_kwargs as _directory_claim_acceptance_kwargs,
    _directory_claim_acceptance_line as _directory_claim_acceptance_line,
    _directory_claim_alternatives as _directory_claim_alternatives,
    _directory_claim_overlaps as _directory_claim_overlaps,
    _directory_claim_row as _directory_claim_row,
    _directory_claim_warning_line as _directory_claim_warning_line,
    _empty_claim_is_stale as _empty_claim_is_stale,
    _fractional_digits as _fractional_digits,
    _grant_landing_write_paths as _grant_landing_write_paths,
    _granted_landing_paths as _granted_landing_paths,
    _landing_fragment_paths as _landing_fragment_paths,
    _live_conflict_is_a_directory_claim as _live_conflict_is_a_directory_claim,
    _live_conflict_path as _live_conflict_path,
    _live_conflict_rows as _live_conflict_rows,
    _node_dispatch_claim_path as _node_dispatch_claim_path,
    _node_dispatch_in_flight_text as _node_dispatch_in_flight_text,
    _peer_claim_is_a_later_racing_arrival as _peer_claim_is_a_later_racing_arrival,
    _peer_claim_is_an_unlaunched_racing_winner as _peer_claim_is_an_unlaunched_racing_winner,
    _peer_scopes_without_shared_landing_paths as _peer_scopes_without_shared_landing_paths,
    _publish_launch_claim as _publish_launch_claim,
    _racing_claim_current as _racing_claim_current,
    _racing_clock as _racing_clock,
    _refuse_a_discarded_launch as _refuse_a_discarded_launch,
    _racing_pause as _racing_pause,
    _raise_repository_scope_conflict as _raise_repository_scope_conflict,
    _read_node_dispatch_claim as _read_node_dispatch_claim,
    _reclaim_stale_node_dispatch_claim as _reclaim_stale_node_dispatch_claim,
    _release_launch_claim as _release_launch_claim,
    _repository_scope_claims as _repository_scope_claims,
    _resolve_declared_path as _resolve_declared_path,
    _resolved_node_scope_entries as _resolved_node_scope_entries,
    _resolved_scope_entries as _resolved_scope_entries,
    _scope_derivation_project as _scope_derivation_project,
    _settle_racing_winner as _settle_racing_winner,
    _shared_landing_paths as _shared_landing_paths,
    _writes_its_landing_fragment as _writes_its_landing_fragment,
    refuse_widen_scope_conflicts as refuse_widen_scope_conflicts,
)

from .dispatch_launch import (  # noqa: E402
    ATTEMPT_RECORD_NAME as ATTEMPT_RECORD_NAME,
    CREW_STATE_ENVIRONMENT as CREW_STATE_ENVIRONMENT,
    EXIT_RECORD_NAME as EXIT_RECORD_NAME,
    FLEET_FIFO_NAME as FLEET_FIFO_NAME,
    FLEET_RECORD_PATH_ENV as FLEET_RECORD_PATH_ENV,
    FLEET_REQUEST_POLL_SECONDS as FLEET_REQUEST_POLL_SECONDS,
    FLEET_SPAWN_ACK_BOUND_SECONDS as FLEET_SPAWN_ACK_BOUND_SECONDS,
    FLEET_SPAWN_ACK_NAME as FLEET_SPAWN_ACK_NAME,
    FLEET_SPAWN_ENV as FLEET_SPAWN_ENV,
    LAUNCH_FAILED_PHASE as LAUNCH_FAILED_PHASE,
    LaunchResolutionError as LaunchResolutionError,
    SANDBOX_STARTUP_WINDOW_SECONDS as SANDBOX_STARTUP_WINDOW_SECONDS,
    STOP_GRACE_DEFAULT as STOP_GRACE_DEFAULT,
    STOP_GRACE_ENV as STOP_GRACE_ENV,
    SUPERVISOR_ENTRY as SUPERVISOR_ENTRY,
    SUPERVISOR_SPEC_NAME as SUPERVISOR_SPEC_NAME,
    SUPERVISOR_SURVIVAL_POLL_SECONDS as SUPERVISOR_SURVIVAL_POLL_SECONDS,
    SUPERVISOR_SURVIVAL_SECONDS as SUPERVISOR_SURVIVAL_SECONDS,
    TERMINAL_MANIFEST_GRACE_DEFAULT as TERMINAL_MANIFEST_GRACE_DEFAULT,
    TERMINAL_MANIFEST_GRACE_ENV as TERMINAL_MANIFEST_GRACE_ENV,
    TREE_SNAPSHOT_NAME as TREE_SNAPSHOT_NAME,
    TmpHeadroomError as TmpHeadroomError,
    VANISHED_BIND_STARTUP_POLL_SECONDS as VANISHED_BIND_STARTUP_POLL_SECONDS,
    WORKER_RECORD_NAME as WORKER_RECORD_NAME,
    WORKER_SCRATCH_BUDGET_BYTES as WORKER_SCRATCH_BUDGET_BYTES,
    WORKER_SCRATCH_ROOT_DEFAULT as WORKER_SCRATCH_ROOT_DEFAULT,
    WORKER_SCRATCH_ROOT_ENV as WORKER_SCRATCH_ROOT_ENV,
    WORKER_SCRATCH_ROOT_NAME as WORKER_SCRATCH_ROOT_NAME,
    _LAUNCHED_WORKERS as _LAUNCHED_WORKERS,
    _LAUNCHED_WORKERS_HANDOVER_ENV as _LAUNCHED_WORKERS_HANDOVER_ENV,
    _LAUNCHED_WORKERS_LOCK as _LAUNCHED_WORKERS_LOCK,
    _LAUNCHED_WORKERS_WAKE as _LAUNCHED_WORKERS_WAKE,
    _LAUNCHED_WORKER_REAPER as _LAUNCHED_WORKER_REAPER,
    _LAUNCHED_WORKER_RUNS as _LAUNCHED_WORKER_RUNS,
    _LAUNCH_FAILURE_STDERR_BYTES as _LAUNCH_FAILURE_STDERR_BYTES,
    _PLACEMENT_PROBE_ATTEMPTS as _PLACEMENT_PROBE_ATTEMPTS,
    _PLACEMENT_PROBE_TIMEOUT_SECONDS as _PLACEMENT_PROBE_TIMEOUT_SECONDS,
    _PR_SET_CHILD_SUBREAPER as _PR_SET_CHILD_SUBREAPER,
    _SUPERVISOR_LAUNCHER_SOURCE as _SUPERVISOR_LAUNCHER_SOURCE,
    _VANISHED_BIND_REFUSAL_PHRASES as _VANISHED_BIND_REFUSAL_PHRASES,
    _WORKER_DONE_MANIFEST_STATUSES as _WORKER_DONE_MANIFEST_STATUSES,
    _WORKER_GRACE_KILL_SECONDS as _WORKER_GRACE_KILL_SECONDS,
    _WORKER_MANIFEST_CEILING_SECONDS as _WORKER_MANIFEST_CEILING_SECONDS,
    _WORKER_MANIFEST_POLL_SECONDS as _WORKER_MANIFEST_POLL_SECONDS,
    _WORKER_MANIFEST_QUIET_SECONDS as _WORKER_MANIFEST_QUIET_SECONDS,
    _WorkerPid as _WorkerPid,
    _adopt_launched_workers_from_reexec as _adopt_launched_workers_from_reexec,
    _attempt_artifact_path as _attempt_artifact_path,
    _attempt_is_current as _attempt_is_current,
    _attempt_started_ns as _attempt_started_ns,
    _become_child_subreaper as _become_child_subreaper,
    _boundary_snapshot_roots as _boundary_snapshot_roots,
    _carried_crew_environment as _carried_crew_environment,
    _child_processes_of as _child_processes_of,
    _confirm_supervisor_survived as _confirm_supervisor_survived,
    _current_host_facts as _current_host_facts,
    _delivered_phase as _delivered_phase,
    _died_over_a_vanished_bind_source as _died_over_a_vanished_bind_source,
    _empty_stream_launch_failure as _empty_stream_launch_failure,
    _ensure_launched_worker_reaper as _ensure_launched_worker_reaper,
    _export_launched_workers_for_reexec as _export_launched_workers_for_reexec,
    _fleet_runtime_dir as _fleet_runtime_dir,
    _fleet_spawn_enabled as _fleet_spawn_enabled,
    _intermediate_failure_detail as _intermediate_failure_detail,
    _iso_stamp_to_ns as _iso_stamp_to_ns,
    _launch_failure_record as _launch_failure_record,
    _launched_prior_session as _launched_prior_session,
    _launched_worker_record as _launched_worker_record,
    _persisted_worker_environment as _persisted_worker_environment,
    _placed_record_identity as _placed_record_identity,
    _placement_hold_payload as _placement_hold_payload,
    _placement_job_alive as _placement_job_alive,
    _placement_job_state as _placement_job_state,
    _plan_composed_the_fence as _plan_composed_the_fence,
    _prepare_attempt_records as _prepare_attempt_records,
    _publish_stored_phase as _publish_stored_phase,
    _read_fleet_record as _read_fleet_record,
    _read_only_bind_sources as _read_only_bind_sources,
    _read_spawn_ack as _read_spawn_ack,
    _reap_launched_workers as _reap_launched_workers,
    _reap_the_launched_worker as _reap_the_launched_worker,
    _reap_worker_on_its_terminal_manifest as _reap_worker_on_its_terminal_manifest,
    _record_is_this_attempt as _record_is_this_attempt,
    _record_launch_abandoned_before_spawn as _record_launch_abandoned_before_spawn,
    _record_launch_failure as _record_launch_failure,
    _record_stop_before_spawn as _record_stop_before_spawn,
    _refusal_names_a_bind_source as _refusal_names_a_bind_source,
    _removable_scratch_target as _removable_scratch_target,
    _run_supervisor as _run_supervisor,
    _runs_inside_fleet_allocation as _runs_inside_fleet_allocation,
    _shim_names as _shim_names,
    _spawn as _spawn,
    _spawn_detached_supervisor as _spawn_detached_supervisor,
    _spawn_detached_worker as _spawn_detached_worker,
    _spawn_through_fleet as _spawn_through_fleet,
    _spawn_worker_retrying_a_vanished_bind as _spawn_worker_retrying_a_vanished_bind,
    _start_supervisor as _start_supervisor,
    _started_phase as _started_phase,
    _stop_grace_seconds as _stop_grace_seconds,
    _stop_is_requested as _stop_is_requested,
    _stream_record_facts as _stream_record_facts,
    _supervisor_argv as _supervisor_argv,
    _supervisor_exit_detail as _supervisor_exit_detail,
    _supervisor_exit_record as _supervisor_exit_record,
    _supervisor_launched_worker as _supervisor_launched_worker,
    _supervisor_manifest_baseline_ns as _supervisor_manifest_baseline_ns,
    _supervisor_manifest_path as _supervisor_manifest_path,
    _supervisor_spawn_worker as _supervisor_spawn_worker,
    _supervisor_spec as _supervisor_spec,
    _supervisor_tree_snapshot as _supervisor_tree_snapshot,
    _supervisor_write as _supervisor_write,
    _terminal_manifest_grace_seconds as _terminal_manifest_grace_seconds,
    _unresolved_backend_command as _unresolved_backend_command,
    _wait_status_from_returncode as _wait_status_from_returncode,
    _wait_status_record as _wait_status_record,
    _watch_request_slug as _watch_request_slug,
    _worker_default_signals as _worker_default_signals,
    _worker_git_shim_directory as _worker_git_shim_directory,
    _worker_host_line as _worker_host_line,
    _worker_manifest_done_status as _worker_manifest_done_status,
    _worker_manifest_is_whole as _worker_manifest_is_whole,
    _worker_process_environment as _worker_process_environment,
    _worker_reaper_loop as _worker_reaper_loop,
    _worker_runtime_environment as _worker_runtime_environment,
    _worker_runtime_plan as _worker_runtime_plan,
    _worker_shim_directory as _worker_shim_directory,
    _write_attempt_artifact as _write_attempt_artifact,
    _write_boundary_tree_snapshot as _write_boundary_tree_snapshot,
    _write_fleet_request as _write_fleet_request,
    apply_backend_placement as apply_backend_placement,
    assert_routable_backends_resolvable as assert_routable_backends_resolvable,
    ensure_worker_scratch as ensure_worker_scratch,
    harness_command_index as harness_command_index,
    launch_search_path as launch_search_path,
    placement_job_id as placement_job_id,
    preflight_launch_command as preflight_launch_command,
    remove_worker_scratch as remove_worker_scratch,
    require_worker_scratch_headroom as require_worker_scratch_headroom,
    resolve_backend_placement as resolve_backend_placement,
    resolve_launch_executable as resolve_launch_executable,
    supervised_launch as supervised_launch,
    tree_size_bytes as tree_size_bytes,
    worker_scratch_dir as worker_scratch_dir,
    worker_scratch_root as worker_scratch_root,
)

from .dispatch_peer import (  # noqa: E402
    _INOTIFY_EVENTS as _INOTIFY_EVENTS,
    _adjacent_live_peers as _adjacent_live_peers,
    _channel_root as _channel_root,
    _dotted_name as _dotted_name,
    _inotify_descriptor as _inotify_descriptor,
    _module_aliases as _module_aliases,
    _needs_help_for_question as _needs_help_for_question,
    _nodes_are_adjacent as _nodes_are_adjacent,
    _peer_command as _peer_command,
    _python_references as _python_references,
    _question_path as _question_path,
    _read_json as _read_json,
    _references_any_module as _references_any_module,
    _resolve_peer as _resolve_peer,
    _scoped_python_files as _scoped_python_files,
    _stamp_agent_display as _stamp_agent_display,
    _supervisor_command as _supervisor_command,
    _unwire_peer_channels as _unwire_peer_channels,
    _update_peer_index as _update_peer_index,
    _wait_seconds as _wait_seconds,
    _wire_peer_channels as _wire_peer_channels,
    peer_ask as peer_ask,
    peer_list as peer_list,
    peer_read as peer_read,
    peer_reply as peer_reply,
)

from .dispatch_picker import (  # noqa: E402
    PICKER_DISPATCH_TIMEOUT_SECONDS as PICKER_DISPATCH_TIMEOUT_SECONDS,
    _PICKER_PER_CALL_FIELDS as _PICKER_PER_CALL_FIELDS,
    _attach_shadow_picker_selection as _attach_shadow_picker_selection,
    _picker_budget_snapshot as _picker_budget_snapshot,
    _picker_fallback as _picker_fallback,
    _picker_input as _picker_input,
    _picker_ledger_rows as _picker_ledger_rows,
    _picker_refusal_reasons as _picker_refusal_reasons,
    _picker_verdict_inputs as _picker_verdict_inputs,
    _record_shadow_picker_selection as _record_shadow_picker_selection,
    _reportable_picker_selection as _reportable_picker_selection,
    _start_shadow_picker_selection as _start_shadow_picker_selection,
    _write_existing_pointer as _write_existing_pointer,
    build_picker_inputs as build_picker_inputs,
    dispatch_picker_selection as dispatch_picker_selection,
    resolve_dispatch_route as resolve_dispatch_route,
)

from .dispatch_plan import (  # noqa: E402
    DispatchPlan as DispatchPlan,
    ROLE_OPENNESS as ROLE_OPENNESS,
    SPEC_LEVEL_OPENNESS as SPEC_LEVEL_OPENNESS,
    UNKNOWN_OPENNESS as UNKNOWN_OPENNESS,
    _carry_declared_gate_documents as _carry_declared_gate_documents,
    _dispatch_lane_allowance as _dispatch_lane_allowance,
    _lane_allowance_unknown as _lane_allowance_unknown,
    _lane_worker_allowance as _lane_worker_allowance,
    _plan_impl_at_dispatch as _plan_impl_at_dispatch,
    _repairs_ledger_root as _repairs_ledger_root,
    _require_repairs_target as _require_repairs_target,
    _resolved_wave_id as _resolved_wave_id,
    _session_unreconciled_refusal as _session_unreconciled_refusal,
    open_endedness_score as open_endedness_score,
    plan_dispatch as plan_dispatch,
)

from .dispatch_sections import (  # noqa: E402
    DONE_WHEN_PLAN_TEXT_SPAN_WORDS as DONE_WHEN_PLAN_TEXT_SPAN_WORDS,
    _dispatch_section_routing as _dispatch_section_routing,
    _done_when_plan_overlap_warning as _done_when_plan_overlap_warning,
    _longest_contiguous_word_span as _longest_contiguous_word_span,
    _normalised_words as _normalised_words,
    _plan_section_text as _plan_section_text,
    _resolved_plan_section_text as _resolved_plan_section_text,
    _section_heading_matches as _section_heading_matches,
    _section_routing_evidence as _section_routing_evidence,
    _section_routing_failure as _section_routing_failure,
    project_mount_repository as project_mount_repository,
    resolve_project_repository as resolve_project_repository,
)

from .dispatch_sessions import (  # noqa: E402
    FENCE_WORKERS as FENCE_WORKERS,
    _apply_orientation_check as _apply_orientation_check,
    _backend_settings as _backend_settings,
    _capture_session_absence as _capture_session_absence,
    _carry_declared_placement as _carry_declared_placement,
    _carry_fence_unprotected as _carry_fence_unprotected,
    _current_harness_session as _current_harness_session,
    _dispatch_session_absence as _dispatch_session_absence,
    _harness_behind_the_fence as _harness_behind_the_fence,
    _inherited_worktree_reading as _inherited_worktree_reading,
    _lane_prompt as _lane_prompt,
    _names_the_fence as _names_the_fence,
    _prior_same_task_run as _prior_same_task_run,
    _prior_session_still_held as _prior_session_still_held,
    _record_node_id as _record_node_id,
    _record_plan as _record_plan,
    _recorded_manifest_path as _recorded_manifest_path,
    _recorded_task_node as _recorded_task_node,
    _restate_time_fence as _restate_time_fence,
    _reviewed_run_id as _reviewed_run_id,
    _run_stream_path as _run_stream_path,
    _shadow_mask_paths as _shadow_mask_paths,
    _session_too_large_to_continue as _session_too_large_to_continue,
    _stored_phase_survives as _stored_phase_survives,
    _task_identity as _task_identity,
    _task_session_resolution as _task_session_resolution,
    _terminal_phase_survives as _terminal_phase_survives,
    _worktree_git_read as _worktree_git_read,
    attach as attach,
    change_lane as change_lane,
    observe as observe,
    record_resumption as record_resumption,
    resume_plan as resume_plan,
    terminate as terminate,
)

from .dispatch_watch import (  # noqa: E402
    SESSION_HOST_DIRECTORY as SESSION_HOST_DIRECTORY,
    WATCHER_LOAD_BOUND_SECONDS as WATCHER_LOAD_BOUND_SECONDS,
    WATCH_ARMING_ENV as WATCH_ARMING_ENV,
    _FollowerAdmissionUnmet as _FollowerAdmissionUnmet,
    _PYTEST_TEMPORARY_ROOT as _PYTEST_TEMPORARY_ROOT,
    _SpawnedHandle as _SpawnedHandle,
    _WATCH_PRODUCER_SUPERVISOR as _WATCH_PRODUCER_SUPERVISOR,
    _ask_session_host_for_follower as _ask_session_host_for_follower,
    _current_and_ancestor_argvs as _current_and_ancestor_argvs,
    _declared_basetemp as _declared_basetemp,
    _ensure_watch_producer as _ensure_watch_producer,
    _open_request_fifo as _open_request_fifo,
    _refuse_arming_under_a_throwaway_home as _refuse_arming_under_a_throwaway_home,
    _released_follower_warning as _released_follower_warning,
    _running_under_pytest as _running_under_pytest,
    _session_host_fifo as _session_host_fifo,
    _session_host_fifo_path as _session_host_fifo_path,
    _session_host_owner as _session_host_owner,
    _session_host_record_path as _session_host_record_path,
    _session_host_runs_follower as _session_host_runs_follower,
    _session_host_runtime_root as _session_host_runtime_root,
    _session_host_waiting as _session_host_waiting,
    _start_watch_producer as _start_watch_producer,
    _stop_watch_producer_within as _stop_watch_producer_within,
    _temporary_home_root as _temporary_home_root,
    _unmet_follower_conditions as _unmet_follower_conditions,
    _watch_arming_intent as _watch_arming_intent,
    _watch_executable as _watch_executable,
    _watch_producer_argv as _watch_producer_argv,
    _watcher_delivery_admission as _watcher_delivery_admission,
    watch_arming_suppressed as watch_arming_suppressed,
)


# The supervisor entry point. This guard sits at the end of the module because
# the entry token and the supervisor's own machinery are defined with the rest
# of the launch path below it, and a module run as the supervisor must have
# them defined before it dispatches on its argv.
if __name__ == "__main__":
    raise SystemExit(_peer_command())
