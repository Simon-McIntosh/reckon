"""Adapt existing fleet measurements and hard gates to picker candidates."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import _backends, budget, capability, ledger
from reckon._timestamps import parse_utc
from reckon.crew import lane_document, resumption, routing
from reckon.crew.dispatch import (
    DispatchPlan,
    _dispatch_lane_gate,
    _lane_worker_allowance,
)
from reckon.crew.node import NodeValidation
from reckon.crew.pace import policy as pace_policy

from .types import Candidate, PickRequest


def recent_outcomes(
    records: list[dict[str, Any]],
    request: PickRequest,
    backend: str,
    model: str | None,
    *,
    now: datetime,
) -> dict[str, int]:
    cutoff = now - timedelta(days=14)
    counts: Counter[str] = Counter()
    for row in records:
        stamp = parse_utc(
            str(row.get("completed_at") or row.get("dispatched_at") or "")
        )
        if stamp is None or not cutoff <= stamp <= now:
            continue
        if (
            row.get("backend") != backend
            or (row.get("agent") or {}).get("model") != model
        ):
            continue
        if (
            row.get("role") != request.node.role
            or row.get("spec_level") != request.node.spec_level
        ):
            continue
        counts[str(row.get("gate") or "unknown")] += 1
    return {key: counts[key] for key in ("passed", "failed", "not-run", "unknown")}


def _lane(backend: dict[str, Any], session: str) -> tuple[Any, Any, dict[str, Any]]:
    path = backend.get("lane_document")
    try:
        document = json.loads(Path(path).expanduser().read_text()) if path else None
    except (OSError, ValueError):
        document = None
    reading = lane_document.read_lane_document(document)
    # The shared allowance helper owns session/new-session/global precedence.
    admission = lane_document.read_lane_admission(document)
    allowance = _lane_worker_allowance(document, session=session)
    slots = allowance.get("allowance")
    if reading["stale"]:
        slots = None
    congestion = {
        key: reading[key] for key in ("running", "waiting", "admission_verdict")
    }
    congestion["stale"] = reading["stale"]
    congestion["slots_state"] = admission["state"]
    return slots, congestion, allowance


def _fit(
    request: PickRequest,
    name: str,
    backend: dict[str, Any],
    repo: Path,
    *,
    verdict_inputs: dict[str, Any] | None = None,
) -> list[str]:
    execution = capability.assess_execution_fit(
        request.node.done_when,
        role=request.node.role,
        execution_capable=backend.get("execution_capable"),
    )
    resolution = DispatchPlan(
        run_id="",
        backend=name,
        launch=backend.get("launch", ""),
        backend_settings=backend,
        node=request.node,
        budget_ceiling="",
        validation=NodeValidation(ok=True),
        execution_fit=execution,
    )
    reasons = (
        [] if execution.allowed else ["execution-fit: " + execution.refusal_detail()]
    )
    context = routing._context_fit_verdict(resolution=resolution, repo=repo)
    competence = routing._competence_verdict(
        resolution=resolution,
        project=request.project,
        repo=repo,
        verdict_inputs=verdict_inputs,
    )
    if context and not context["allowed"]:
        reasons.append("context-fit: " + context["reason"])
    if context and request.estimated_context > context["window_tokens"]:
        reasons.append(
            f"context-fit: requested {request.estimated_context} tokens exceeds {context['window_tokens']}"
        )
    if not competence["allowed"]:
        reasons.append("competence: " + competence["reason"])
    return reasons


def budget_view(
    project: str, config: dict[str, Any], repo: Path, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compose one dated live budget view using its existing state and pace readers."""
    windows = budget.recorded_windows(project, config, root=repo, records=records)
    # Every configured backend is budget-probed; nothing is filtered by name, so a
    # refusal can only come from a live serving observation, never a fixed list.
    probeable = list(config.get("backends", {}))
    # The budget view composes state_for and group_pace, including the account's
    # operative window. Consuming its verdict keeps every clock in one authority.
    return budget.preflight(
        project,
        config,
        root=repo,
        backends=probeable,
        windows=windows,
        records=records,
        now=datetime.now(UTC),
    )


def candidates(
    request: PickRequest,
    config: dict[str, Any],
    repo: Path,
    *,
    records: list[dict[str, Any]] | None = None,
    availability_cache: dict[tuple[str, str | None], dict[str, Any]] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    verdict_inputs: dict[str, Any] | None = None,
) -> list[Candidate]:
    """Read a fresh snapshot; never dispatch or change routing configuration."""
    now = datetime.now(UTC)
    rows = ledger.runs(request.project, root=repo) if records is None else records
    shared = (
        verdict_inputs
        if verdict_inputs is not None
        else routing.shared_verdict_inputs(request.project, repo)
    )
    view = (
        budget_snapshot
        if budget_snapshot is not None
        else budget_view(request.project, config, repo, rows)
    )
    budget_by_backend = {row["backend"]: row for row in view["backends"]}
    group_by_backend = {
        member: group for group in view["groups"] for member in group["members"]
    }
    result = []
    for name in config.get("backends", {}):
        local = name == config.get("local_backend")
        _, backend = routing.resolve_role_override(
            config,
            request.node.role,
            request.node.spec_level,
            name,
            capability_class=str(request.capability.get("class") or ""),
        )
        model = backend.get("model")
        reasons = []
        verdict = budget_by_backend.get(name) or {
            "held": False,
            "state": budget.BudgetState(name).as_dict(),
        }
        state = verdict["state"]
        if verdict["held"]:
            reasons.append("budget-held: " + verdict["reason"])
        metered = not local and not ledger.is_unmetered_backend(name)
        if (
            metered
            and state.get("burn_multiple") is not None
            and state["burn_multiple"] > pace_policy(config).pace_multiple
        ):
            reasons.append("burn-exceeds-pace-multiple")
        gate = _dispatch_lane_gate(backend)
        if gate["state"] in {"paused", "unreadable"}:
            reasons.append("lane-gate: " + gate["state"])
        slots, congestion, allowance = (
            _lane(backend, request.session) if local else (None, None, {})
        )
        if local and allowance.get("held"):
            reasons.append("local-lane-no-worker-slots")
        # Already-excluded candidates need no repository census or serving probe.
        if not reasons:
            reasons.extend(_fit(request, name, backend, repo, verdict_inputs=shared))
        if reasons:
            availability = "not-probed"
        else:
            cache_key = (name, model)
            observation = (availability_cache or {}).get(cache_key)
            if observation is None:
                serving_backend = {
                    **config["backends"][name],
                    "model": model,
                    "effort": backend.get("effort"),
                }
                observation = resumption.probe_lane_availability(
                    request.project, name, serving_backend, root=repo
                )
                if availability_cache is not None:
                    availability_cache[cache_key] = observation
            availability = observation["status"]
            if availability != "served":
                reasons.append("availability: " + availability)
        try:
            family = "local" if local else _backends.dialect_for(backend).name
        except _backends.BackendError:
            family = str(backend.get("launch") or name)
        group_allowance = (group_by_backend.get(name) or {}).get("allowance") or {}
        budget_facts = (
            state
            if state.get("headroom") == "known" and not state.get("expired")
            else {}
        )
        result.append(
            Candidate(
                backend=name,
                family=family,
                model=model,
                effort=backend.get("effort"),
                local=local,
                availability=availability,
                utilisation_pct=budget_facts.get("utilisation_pct"),
                burn_multiple=budget_facts.get("burn_multiple"),
                pace_allowance=group_allowance.get("effective_limit"),
                resets_at=budget_facts.get("resets_at"),
                worker_slots=slots,
                congestion=congestion,
                outcomes=recent_outcomes(rows, request, name, model, now=now),
                reasons=reasons,
            )
        )
    return result
