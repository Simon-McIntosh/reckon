"""Steer a queued request while it is still waiting to launch.

A request that has not launched is not yet a run: its steering text, its
measure, its write scope, its time budget and its route are still the
coordinator's to change, and dropping it costs a queue entry rather than a
worker. These three operations own that window:

* :func:`edit` replaces the request's ``steering.md`` and its declared fields,
  and refuses a change that would leave the request failing the same contract
  checks a dispatch applies before any worker exists;
* :func:`reprioritise` records an explicit priority override on the request, so
  the ranking that consumes it reads the request rather than a coordinator's
  memory;
* :func:`drop` removes a waiting request through the same removal a discard
  performs, and refuses every phase that is not still waiting.

The launched side is deliberately not here: once a request has launched, the
worker holds the brief it read and the run's own verbs act on it. Every
operation therefore refuses a pointer whose phase is no longer waiting rather
than editing a run underneath a worker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew.node import CrewError, TaskNode, validate_node

#: The one file a waiting request's steering text lives in, inside the request's
#: own run directory. The queued path writes it at queue time and admission
#: reads it at launch, so an edit and a launch resolve the same file.
STEERING_FILENAME = "steering.md"

#: The phases in which a request is still waiting to launch, and so is still the
#: coordinator's to change or remove.
WAITING_PHASES: tuple[str, ...] = ("queued", "awaiting-upstream", "held")

#: The node fields an edit may replace beside the steering text.
EDITABLE_NODE_FIELDS: tuple[str, ...] = ("done_when", "write_paths", "time_budget")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _phase_of(record: Mapping[str, Any]) -> str:
    return str(record.get("phase") or "")


def _refuse_unless_waiting(record: Mapping[str, Any], verb: str) -> None:
    """Refuse a request that is no longer waiting, naming the phase it holds.

    A pointer past these phases may have a worker reading its brief right now,
    so an edit would change a document the run has already recorded a digest of
    and a drop would remove a live run's record. Both are refused here rather
    than at a later gate that would have to repeat the distinction.
    """
    phase = _phase_of(record)
    if phase in WAITING_PHASES:
        return
    run_id = str(record.get("run_id") or "")
    raise CrewError(
        f"cannot {verb} request {run_id!r}: it is {phase or 'in an unrecorded phase'}, "
        f"not one of {', '.join(WAITING_PHASES)}; only a request that has not "
        "launched can be changed or dropped"
    )


def _read_request(run_id: str, project: str | None) -> dict[str, Any]:
    from reckon.crew import runs as runs_module

    record = runs_module.read_pointer(run_id)
    if project and str(record.get("project") or "") != str(project):
        raise CrewError(
            f"run {run_id!r} belongs to project "
            f"{str(record.get('project') or '')!r}, not {project!r}"
        )
    return record


def steering_path(run_id: str) -> Path:
    """The steering file a waiting request's launch reads."""
    from reckon.crew import runs as runs_module

    return runs_module.run_dir(run_id) / STEERING_FILENAME


def _node_of(record: Mapping[str, Any]) -> TaskNode:
    """Rebuild the request's node, honouring the queued directory's defaults."""
    from reckon.crew.queue_admission import _node_from_pointer

    return _node_from_pointer(record)


def _named_route(
    record: Mapping[str, Any], backend: str, model: str
) -> dict[str, str] | None:
    """Resolve an edited route through the checks a dispatch applies to ``--backend``.

    A route named here is a named route, not a hint: it passes the same
    override checks a dispatch's own lane and model do, so an edit cannot put a
    request on a lane or model the flight configuration does not serve. The
    backend's lane and model key are returned beside its name, so a later
    reader carries the catalogue pair without re-resolving a name that may
    since have moved.
    """
    from reckon import ledger
    from reckon.crew.queue_admission import _resolve_lane_config
    from reckon.crew_dispatch_commands import (
        _model_narrowed_backend,
        _refuse_a_combined_lane_model_backend,
    )

    if not backend and not model:
        return None
    config = _resolve_lane_config(record)
    _refuse_a_combined_lane_model_backend(backend)
    if model:
        try:
            backend_name, lane, model_key = _model_narrowed_backend(
                config, model, backend, local=bool(record.get("local"))
            )
        except Exception as exc:
            raise CrewError(str(getattr(exc, "message", exc))) from exc
        return {
            "backend": str(backend_name),
            "lane": str(lane),
            "model_key": str(model_key),
        }
    lane, model_key = ledger.resolve_name(backend)
    entry = (config.get("backends") or {}).get(backend)
    if entry is None and lane is None:
        defined = ", ".join(
            sorted(str(name) for name in (config.get("backends") or {}))
        )
        raise CrewError(
            f"backend {backend!r} is not defined by the flight configuration "
            f"(defined backends: {defined or 'none'})"
        )
    if isinstance(entry, Mapping):
        return {
            "backend": str(backend),
            "lane": str(entry.get("lane") or lane or ""),
            "model_key": str(entry.get("model_key") or model_key or ""),
        }
    return {
        "backend": str(backend),
        "lane": str(lane or ""),
        "model_key": str(model_key or ""),
    }


def _contract_findings(
    node: TaskNode, record: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Judge the edited node against the checks a dispatch applies before a launch.

    The dispatch's own ``validate_node`` is the contract, so an edit cannot
    leave a request that the admission pass would refuse at launch. The
    decisions the original dispatch recorded as locked travel with the request,
    so an edit is judged under the same locks the launch will be.
    """
    from reckon.crew.queue_admission import _resolve_lane_config
    from reckon.crew.routing import resolved_time_ceiling

    options = record.get("dispatch_options") or {}
    node.manifest_path = str((record.get("node") or {}).get("manifest_path") or "")
    verdict = validate_node(
        node,
        locked_decisions=tuple(options.get("locked_decisions") or ()),
        budget_ceiling=resolved_time_ceiling(_resolve_lane_config(record)),
    )
    return list(verdict.findings)


def edit(
    run_id: str,
    *,
    steering_path_source: str = "",
    done_when: str | None = None,
    write_paths: list[str] | None = None,
    time_budget: str | None = None,
    backend: str = "",
    model: str = "",
    project: str | None = None,
) -> dict[str, Any]:
    """
    Replace a waiting request's steering text, measure, scope, budget or route.

    ``steering_path_source`` names a file whose bytes replace the request's
    ``steering.md``; the other arguments replace the node fields they name. At
    least one change is required. The edited node is judged with the dispatch's
    own contract check before anything is written, so a refused edit leaves the
    request exactly as it was.
    """
    from reckon.crew import runs as runs_module

    changes = {
        "steering": bool(steering_path_source),
        "done_when": done_when is not None,
        "write_paths": write_paths is not None,
        "time_budget": time_budget is not None,
        "route": bool(backend or model),
    }
    if not any(changes.values()):
        raise CrewError(
            "a queue edit names at least one of a steering file, done-when, "
            "write path, time budget, backend or model"
        )

    steering_text: str | None = None
    if changes["steering"]:
        source = Path(steering_path_source).expanduser()
        try:
            steering_text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise CrewError(
                f"steering file {steering_path_source!r} could not be read: {exc}"
            ) from exc
        if not steering_text.strip():
            raise CrewError(
                f"steering file {steering_path_source!r} is empty; a request's "
                "steering text is its authority and cannot be blanked"
            )

    record = _read_request(run_id, project)
    _refuse_unless_waiting(record, "edit")

    node = _node_of(record)
    if done_when is not None:
        node.done_when = done_when
    if write_paths is not None:
        node.write_paths = list(write_paths)
    if time_budget is not None:
        node.time_budget = time_budget
    if steering_text is not None:
        node.brief = steering_text
    route = _named_route(record, backend, model)

    findings = _contract_findings(node, record)
    if findings:
        detail = "; ".join(
            f"{finding.get('property')}: {finding.get('detail')}"
            for finding in findings
        )
        raise CrewError(
            f"the edited request fails its contract checks and was not stored — {detail}"
        )

    digest = (
        hashlib.sha256(steering_text.encode("utf-8")).hexdigest()
        if steering_text is not None
        else ""
    )

    with runs_module._pointer_lock(run_id):
        current = runs_module.read_pointer(run_id)
        # The request may have launched between the read and this lock; an edit
        # that slipped in afterwards would change a brief a worker is reading.
        _refuse_unless_waiting(current, "edit")
        node_block = dict(current.get("node") or {})
        if done_when is not None:
            node_block["done_when"] = done_when
        if write_paths is not None:
            node_block["write_paths"] = list(write_paths)
        if time_budget is not None:
            node_block["time_budget"] = time_budget
        target = steering_path(run_id)
        if steering_text is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(steering_text, encoding="utf-8")
            node_block["brief_path"] = str(target)
            node_block["brief_sha256"] = digest
        current["node"] = node_block
        options = dict(current.get("dispatch_options") or {})
        if route is not None:
            current["backend"] = route["backend"]
            current["requested_backend"] = route["backend"]
            current["lane"] = route["lane"]
            current["model_key"] = route["model_key"]
            options["route"] = "deterministic"
            current["named_route"] = {
                **route,
                "set_at": _now(),
                "set_by": "queue-edit",
            }
        current["dispatch_options"] = options
        current["edited_at"] = _now()
        runs_module._write_json(runs_module.pointer_path(run_id), current)

    return {
        "run_id": run_id,
        "phase": _phase_of(current),
        "changed": [name for name, changed in changes.items() if changed],
        "steering_path": str(target) if steering_text is not None else "",
        "steering_sha256": digest,
        "route": route or {},
        "done_when": str(node_block.get("done_when") or ""),
        "write_paths": list(node_block.get("write_paths") or ()),
        "time_budget": str(node_block.get("time_budget") or ""),
    }


def reprioritise(
    run_id: str, priority: int, *, reason: str = "", project: str | None = None
) -> dict[str, Any]:
    """Record an explicit priority override on a waiting request.

    The override is stored on the request itself, not applied as a one-off
    reorder, so the ranking that reads the queue sees the coordinator's intent
    on every later pass. The roadmap key the request otherwise ranks by is left
    untouched beside it, so a reader can tell an overridden request from one
    sorted by its section.
    """
    from reckon.crew import runs as runs_module

    record = _read_request(run_id, project)
    _refuse_unless_waiting(record, "reprioritise")

    with runs_module._pointer_lock(run_id):
        current = runs_module.read_pointer(run_id)
        _refuse_unless_waiting(current, "reprioritise")
        current["priority_override"] = int(priority)
        current["priority_override_set_at"] = _now()
        current["priority_override_reason"] = reason
        runs_module._write_json(runs_module.pointer_path(run_id), current)

    return {
        "run_id": run_id,
        "phase": _phase_of(current),
        "priority_override": int(priority),
        "reason": reason,
    }


def drop(
    run_id: str, *, reason: str = "", project: str | None = None
) -> dict[str, Any]:
    """Remove a waiting request without promoting it.

    The removal is the discard's own path, so the departure is recorded where a
    later reader looks for it and any worktree a launch left behind is released
    by the same audit. The phase check is what makes this the queue's verb: a
    pointer past the waiting phases is a run, and dropping one is a different
    decision than withdrawing a request nobody started.

    The phase is checked again by the removal itself, against the record it
    re-reads under the pointer's lock, so a request that launches between this
    call's first read and its discard is refused rather than removed.
    """
    from reckon import crew as crew_module

    record = _read_request(run_id, project)
    _refuse_unless_waiting(record, "drop")
    phase = _phase_of(record)
    result = crew_module.discard(run_id, allowed_phases=WAITING_PHASES)
    return {"run_id": run_id, "dropped_phase": phase, "reason": reason, **result}


def _selected_requests(
    run_id: str | None,
    plan: str | None,
    session: str | None,
    project: str | None,
    *,
    release: bool,
) -> list[str]:
    from reckon.crew import runs

    if sum(bool(value) for value in (run_id, plan, session)) != 1:
        raise CrewError("name exactly one of --run, --plan or --session")
    if run_id:
        _read_request(run_id, project)
        return [run_id]
    selected = [
        str(pointer["run_id"])
        for pointer in runs.list_live(project=project)
        if (
            not plan
            or str(
                pointer.get("plan")
                or (pointer.get("node") or {}).get("plan")
                or (pointer.get("dispatch_options") or {}).get("plan_slug")
                or ""
            )
            == plan
        )
        and (not session or str(pointer.get("session") or "") == session)
        and _phase_of(pointer) in (("held",) if release else WAITING_PHASES)
    ]
    if not selected:
        raise CrewError(f"no waiting requests found for {plan or session!r}")
    return selected


def _change_hold(run_id: str, *, release: bool, reason: str) -> dict[str, Any]:
    from reckon._timestamps import parse_utc
    from reckon.crew import runs

    with runs._pointer_lock(run_id):
        current = runs.read_pointer(run_id)
        phase = _phase_of(current)
        if release:
            if phase != "held":
                raise CrewError(
                    f"cannot release request {run_id!r}: it is {phase}, not held"
                )
            hold_record = dict(current.get("queue_hold") or {})
            prior = str(hold_record.get("prior_phase") or "")
            if prior not in ("queued", "awaiting-upstream"):
                raise CrewError(f"request {run_id!r} has no readable prior phase")
            started = parse_utc(hold_record.get("started_at"))
            queued_at = parse_utc(current.get("queued_at"))
            ended = datetime.now(UTC)
            if started is not None and queued_at is not None:
                current["queued_at"] = (queued_at + (ended - started)).isoformat()
            current["phase"] = prior
            current.pop("queue_hold", None)
            current["queue_released_at"] = ended.isoformat()
        else:
            if phase == "held":
                return {"run_id": run_id, "phase": phase, "unchanged": True}
            if phase not in ("queued", "awaiting-upstream"):
                raise CrewError(
                    f"cannot hold request {run_id!r}: it is {phase or 'unrecorded'}; "
                    "use crew stop for a launched run"
                )
            current["queue_hold"] = {
                "prior_phase": phase,
                "started_at": _now(),
                "reason": reason,
            }
            current["phase"] = "held"
        runs._write_json(runs.pointer_path(run_id), current)
    return {"run_id": run_id, "phase": current["phase"], "reason": reason}


def hold(
    run_id: str | None = None,
    *,
    plan: str | None = None,
    session: str | None = None,
    reason: str = "",
    project: str | None = None,
) -> dict[str, Any]:
    """Pause waiting requests without losing their brief, waits or priority."""
    from reckon.crew.queue_admission import _admit_lock

    with _admit_lock():
        ids = _selected_requests(run_id, plan, session, project, release=False)
        changed = [
            _change_hold(identifier, release=False, reason=reason) for identifier in ids
        ]
    return {"held": changed, "count": len(changed)}


def release(
    run_id: str | None = None,
    *,
    plan: str | None = None,
    session: str | None = None,
    reason: str = "",
    project: str | None = None,
) -> dict[str, Any]:
    """Restore held requests to their previous phase and paused starvation age."""
    from reckon.crew.queue_admission import _admit_lock

    with _admit_lock():
        ids = _selected_requests(run_id, plan, session, project, release=True)
        changed = [
            _change_hold(identifier, release=True, reason=reason) for identifier in ids
        ]
    return {"released": changed, "count": len(changed)}
