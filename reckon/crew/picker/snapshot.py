"""Adapt existing fleet measurements and hard gates to picker candidates."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import _backends, budget, capability, ledger
from reckon._timestamps import parse_utc
from reckon.crew import lane_document, paid_lanes, recovery, resumption, routing
from reckon.crew.dispatch import (
    DispatchPlan,
    _dispatch_lane_gate,
    _lane_worker_allowance,
)
from reckon.crew.node import NodeValidation

from .types import Candidate, PickRequest


@dataclass
class BudgetCandidate(Candidate):
    """A candidate with an explanation when its account window is unknown.

    ``stale`` and ``budget_age_s`` describe the age of the reading behind the
    candidate's figures. A stale reading keeps its figures rather than nulling
    them, so the age and flag travel beside the numbers they qualify.
    """

    budget_reason: str | None = None
    stale: bool | None = None
    budget_age_s: float | None = None


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


# A serving verdict read from a lane's own endpoints document, translated into
# the picker's availability vocabulary. A lane the document shows down cannot
# serve, so its verdict is a hard exclusion; a verdict the reader cannot
# establish is unknown, never a claim the lane is down.
_SERVING_AVAILABILITY = {
    "serving": "served",
    "not-serving": "unavailable",
    "mismatch": "unavailable",
}


def _serving_observation(backend: dict[str, Any]) -> dict[str, Any] | None:
    """A backend's availability read from the document it publishes.

    Only a backend declaring an ``endpoints_document`` answers here. The
    verdict is read with the same reader ``reckon flight`` reports its serving
    column with, so a lane declared serving to that surface is served here.
    The document is a local file and is never read over the network.
    """
    if not backend.get("endpoints_document"):
        return None
    from reckon import flight

    reading = flight._probe_serving(backend)
    verdict = str(reading.get("serving") or "unknown")
    return {
        "status": _SERVING_AVAILABILITY.get(verdict, "unknown"),
        "detail": str(reading.get("serving_detail") or ""),
    }


def _cached_observation(
    project: str,
    backend_name: str,
    config: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """A cached serving observation still within its declared shelf life.

    An absent cache, and one whose age cannot be established or has passed the
    declared shelf life, return None. None of those is evidence a lane cannot
    serve, only that nothing has observed it recently — the caller reads that
    absence as unknown rather than as a refusal.
    """
    observation = resumption._read_lane_probe_cache(project, backend_name)
    if not observation:
        return None
    observed = resumption._parse_stamp(observation.get("observed_at"))
    if observed is None:
        return None
    shelf_minutes = float(
        budget.policy(config).get(
            "evidence_shelf_life_minutes", budget.DEFAULT_SHELF_LIFE_MINUTES
        )
    )
    if (now - observed).total_seconds() > shelf_minutes * 60.0:
        return None
    return observation


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
    # The competence verdict measures this backend's context window on every
    # path that reaches its context check and returns the measurement under
    # ``context``. Read it there rather than measuring the same window a second
    # time: the measurement reads the instruction chain, whose repository lookup
    # shells out to git, so a second call costs a second subprocess on the
    # pick's critical path under the dispatch bound.
    competence = routing._competence_verdict(
        resolution=resolution,
        project=request.project,
        repo=repo,
        verdict_inputs=verdict_inputs,
    )
    context = competence.get("context")
    if context is None:
        context = routing._context_fit_verdict(resolution=resolution, repo=repo)
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
    project: str,
    config: dict[str, Any],
    repo: Path,
    records: list[dict[str, Any]],
    *,
    cached_only: bool = False,
) -> dict[str, Any]:
    """Compose one dated live budget view using its existing state and pace readers."""
    if cached_only:
        # Budget preflight may refresh an undated refusal with a serving request.
        # Disable that refresh; candidate availability comes from the cache below.
        config = {
            **config,
            "backends": {
                name: {**backend, "budget_check": False}
                for name, backend in config.get("backends", {}).items()
            },
        }
    moment = datetime.now(UTC)
    windows = budget.recorded_windows(project, config, root=repo, records=records)
    document = paid_lanes.read_document()
    published = paid_lanes.document_windows(document, moment=moment)
    # Preflight prefers fresh published figures to recorded ones. Preserve an
    # older published figure only where no run has recorded that account, so
    # the candidate can report its age and stale reason instead of an absence.
    for account, reading in published.items():
        windows.setdefault(account, reading)
    # Every configured backend is budget-probed; nothing is filtered by name, so a
    # refusal can only come from a live serving observation, never a fixed list.
    probeable = list(config.get("backends", {}))
    # The budget view composes state_for and group_pace, including the account's
    # operative window. Consuming its verdict keeps every clock in one authority.
    report = budget.preflight(
        project,
        config,
        root=repo,
        backends=probeable,
        windows=windows,
        records=records,
        now=moment,
        document=document,
        **({"probe_runner": lambda _: {}} if cached_only else {}),
    )
    by_backend = {entry["backend"]: entry for entry in report["backends"]}
    shelf_seconds = budget.policy(config)["evidence_shelf_life_minutes"] * 60
    moment = parse_utc(report["checked_at"])
    for group in report["groups"]:
        allowance = group.get("allowance") or {}
        if allowance.get("state") != budget.OBSERVED:
            for name in group["members"]:
                by_backend[name]["state"]["detail"] = (
                    f"no recorded account-window reading for budget group {group['group']}"
                )
            continue
        observed = parse_utc(str(allowance.get("observed_at") or ""))
        if observed is None or moment is None:
            continue
        stale = (moment - observed).total_seconds() > shelf_seconds
        source = (
            "account-surface" if group.get("source") == "account-surface" else "ledger"
        )
        for name in group["members"]:
            state = by_backend[name]["state"]
            state.update(
                source=source,
                observed_at=observed.isoformat(),
                expired=stale,
                headroom="known",
                utilisation_pct=allowance["utilisation"] * 100,
                burn_multiple=allowance["burn_multiple"],
                resets_at=allowance["resets_at"],
                detail=(
                    "recorded account-window reading is stale"
                    if stale
                    else state.get("detail", "")
                ),
            )
    report["summary"] = budget.summary(report)
    return report


def candidates(
    request: PickRequest,
    config: dict[str, Any],
    repo: Path,
    *,
    records: list[dict[str, Any]] | None = None,
    availability_cache: dict[tuple[str, str | None], dict[str, Any]] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    verdict_inputs: dict[str, Any] | None = None,
    cached_only: bool = False,
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
        else budget_view(request.project, config, repo, rows, cached_only=cached_only)
    )
    # The node's estimate is independent of the candidate. Parsing its plan
    # once keeps a backend census from multiplying identical repository reads.
    shared = {
        **shared,
        "node_estimate": routing._estimated_hours(repo, request.project, request.node),
    }
    budget_by_backend = {row["backend"]: row for row in view["backends"]}
    group_by_backend = {
        member: group for group in view["groups"] for member in group["members"]
    }
    # A review never runs on a backend the flight configuration withdraws from
    # review routing, so the candidate is removed here rather than offered for
    # Jev to weigh: an exclusion is a rule about what cannot run, not a
    # pressure signal. Read through the recovery helper so the key name and its
    # parsing have one source of truth.
    review_excluded: set[str] = set()
    if request.node.role == "review":
        review_excluded = recovery._review_excluded_backends(config)
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
        if name in review_excluded:
            reasons.append("review-excluded-backend")
        # A routed CLI dispatch cannot execute an in-harness backend: nothing
        # spawns it, because only a coordinator attaching the task it already
        # runs can bind the harness, so the picker removes it rather than
        # offering it for Jev to weigh. The launch is read from the resolved
        # backend, so a role overlay that changes the launch is honoured.
        if backend.get("launch") == recovery.IN_HARNESS_LAUNCH:
            reasons.append("in-harness-backend")
        verdict = budget_by_backend.get(name) or {
            "held": False,
            "state": budget.BudgetState(name).as_dict(),
        }
        state = verdict["state"]
        group = group_by_backend.get(name)
        group_allowance = (group or {}).get("allowance") or {}
        # A reading is carried whether or not it has passed its shelf life: an
        # old figure with its age and stale flag lets Jev weigh it, where a null
        # figure hides the account exactly when it has been idle. Only a reading
        # that is genuinely absent leaves the figures null, with its
        # budget_reason naming why.
        reading_known = state.get("headroom") == "known" and (
            group is None
            or group_allowance.get("state") == budget.OBSERVED
            or group_allowance.get("effective_limit") is not None
        )
        budget_facts = state if reading_known else {}
        stale = bool(state.get("expired")) if reading_known else None
        # The ceiling gate acts on a fresh reading alone: an old figure is
        # offered for Jev to weigh, never turned into a hard refusal.
        fresh = reading_known and not state.get("expired")
        utilisation = state.get("utilisation_pct") if fresh else None
        ceiling = budget.policy(config)["utilisation_ceiling_pct"]
        if utilisation is not None and utilisation >= ceiling:
            reasons.append(f"budget-ceiling: {utilisation:g}% at or above {ceiling:g}%")
        gate = _dispatch_lane_gate(backend)
        if gate["state"] in {"paused", "unreadable"}:
            reasons.append("lane-gate: " + gate["state"])
        slots, congestion, _ = (
            _lane(backend, request.session) if local else (None, None, {})
        )
        # Already-excluded candidates need no repository census or serving probe.
        if not reasons:
            reasons.extend(_fit(request, name, backend, repo, verdict_inputs=shared))
        if reasons:
            availability = "not-probed"
        else:
            cache_key = (name, model)
            observation = (availability_cache or {}).get(cache_key)
            if observation is None:
                # A lane publishing what it serves answers from that document,
                # which is authoritative for it and costs no request.
                observation = _serving_observation(backend)
            if observation is None and cached_only:
                observation = _cached_observation(
                    request.project, name, config, now=now
                )
            if observation is None:
                if cached_only:
                    # A cached pick issues no request. An absent or expired
                    # observation is unknown, never a claim the lane is down,
                    # so the candidate stays offered for Jev to weigh.
                    observation = {"status": "unknown"}
                else:
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
            availability = str(observation.get("status") or "unknown")
            if availability in {"refused", "unavailable", "logged-out"}:
                reasons.append("availability: " + availability)
        try:
            family = "local" if local else _backends.dialect_for(backend).name
        except _backends.BackendError:
            family = str(backend.get("launch") or name)
        reset = parse_utc(str(budget_facts.get("resets_at") or ""))
        days_to_reset = (
            max(0.0, (reset - now).total_seconds() / 86400) if reset else None
        )
        observed = parse_utc(str(state.get("observed_at") or ""))
        budget_age_s = (
            max(0.0, (now - observed).total_seconds())
            if reading_known and observed is not None
            else None
        )
        budget_reason = None
        if group is not None and not budget_facts:
            budget_reason = str(state.get("detail") or "") or (
                "no recorded account-window reading for the candidate's budget group"
            )
        result.append(
            BudgetCandidate(
                backend=name,
                family=family,
                model=model,
                effort=backend.get("effort"),
                local=local,
                availability=availability,
                utilisation_pct=budget_facts.get("utilisation_pct"),
                burn_multiple=budget_facts.get("burn_multiple"),
                pace_allowance=(
                    group_allowance.get("effective_limit") if budget_facts else None
                ),
                resets_at=budget_facts.get("resets_at"),
                days_to_reset=days_to_reset,
                worker_slots=slots,
                congestion=congestion,
                outcomes=recent_outcomes(rows, request, name, model, now=now),
                reasons=reasons,
                budget_reason=budget_reason,
                stale=stale,
                budget_age_s=budget_age_s,
            )
        )
    return result
