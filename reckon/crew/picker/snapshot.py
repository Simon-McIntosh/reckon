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

# Account refusals are eligibility data, independent of a budget reading.
REFUSED_BACKENDS = frozenset({"codex-spark"})
REFUSED_MODELS = frozenset({"gpt-5.3-codex-spark"})


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
    request: PickRequest, name: str, backend: dict[str, Any], repo: Path
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
        resolution=resolution, project=request.project, repo=repo
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


def candidates(
    request: PickRequest,
    config: dict[str, Any],
    repo: Path,
    *,
    records: list[dict[str, Any]] | None = None,
) -> list[Candidate]:
    """Read a fresh snapshot; never dispatch or change routing configuration."""
    now = datetime.now(UTC)
    rows = ledger.runs(request.project, root=repo) if records is None else records
    recorded = budget.latest_recorded(request.project, root=repo, config=config)
    windows = budget.recorded_windows(request.project, config, root=repo, records=rows)
    groups = budget.group_pace(config, windows=windows, records=rows, now=now)
    group_by_backend = {
        member: group for group in groups for member in group["members"]
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
        if name in REFUSED_BACKENDS or model in REFUSED_MODELS:
            reasons.append("account-refused-model")
        state = budget.state_for(name, backend, recorded=recorded.get(name), now=now)
        verdict = budget.decide(state, budget.policy(config), now=now)
        if verdict["held"]:
            reasons.append("budget-held: " + verdict["reason"])
        metered = not local and not ledger.is_unmetered_backend(name)
        if (
            metered
            and state.burn_multiple is not None
            and state.burn_multiple > pace_policy(config).pace_multiple
        ):
            reasons.append("burn-exceeds-pace-multiple")
        gate = _dispatch_lane_gate(backend)
        if gate["state"] in {"paused", "unreadable"}:
            reasons.append("lane-gate: " + gate["state"])
        reasons.extend(_fit(request, name, backend, repo))
        slots, congestion, allowance = (
            _lane(backend, request.session) if local else (None, None, {})
        )
        if local and allowance.get("held"):
            reasons.append("local-lane-no-worker-slots")
        # A known refusal must not spend a serving probe on the refused model.
        if reasons:
            availability = "not-probed"
        else:
            observation = resumption.probe_lane_availability(
                request.project, name, backend, root=repo
            )
            availability = observation["status"]
            if availability != "served":
                reasons.append("availability: " + availability)
        try:
            family = "local" if local else _backends.dialect_for(backend).name
        except _backends.BackendError:
            family = str(backend.get("launch") or name)
        group_allowance = (group_by_backend.get(name) or {}).get("allowance") or {}
        result.append(
            Candidate(
                backend=name,
                family=family,
                model=model,
                effort=backend.get("effort"),
                local=local,
                availability=availability,
                utilisation_pct=state.utilisation_pct,
                burn_multiple=state.burn_multiple,
                pace_allowance=group_allowance.get("effective_limit"),
                resets_at=state.resets_at,
                worker_slots=slots,
                congestion=congestion,
                outcomes=recent_outcomes(rows, request, name, model, now=now),
                reasons=reasons,
            )
        )
    return result
