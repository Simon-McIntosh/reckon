"""Portable task-capability requests and deterministic runtime matching.

Persisted plans describe the ability and safeguards a task needs. Concrete
worker identities remain runtime data and are selected only when work is
dispatched.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
from typing import Any, Iterable, Mapping

CAPABILITY_SCHEMA_VERSION = "1.0"
CAPABILITY_CLASSES = ("routine", "general", "orchestrator")
REASONING_LEVELS = ("standard", "deep")
CONTEXT_LEVELS = ("standard", "extended")
AUTONOMY_LEVELS = ("guided", "autonomous")
VERIFICATION_LEVELS = ("standard", "strict")
RISK_LEVELS = ("low", "moderate", "elevated", "critical")

REQUIREMENT_LEVELS = {
    "reasoning": REASONING_LEVELS,
    "context": CONTEXT_LEVELS,
    "tool_autonomy": AUTONOMY_LEVELS,
    "verification": VERIFICATION_LEVELS,
    "risk": RISK_LEVELS,
}

# Compatibility input only. These identifiers are never emitted by normalised
# capability requests or used to select a concrete worker.
LEGACY_TIER_TO_CLASS = {
    "haiku": "routine",
    "sonnet": "general",
    "opus": "orchestrator",
}


def capability_request(
    capability_class: str = "general",
    *,
    requirements: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the canonical persisted capability request."""
    req = {
        key: str(value)
        for key, value in (requirements or {}).items()
        if value not in (None, "")
    }
    return {
        "version": CAPABILITY_SCHEMA_VERSION,
        "class": capability_class,
        "requirements": req,
    }


def from_legacy_tier(value: object) -> tuple[dict[str, Any] | None, str | None]:
    """Map a compatibility tier to a request plus an audit diagnostic."""
    raw = str(value or "").strip().lower()
    capability_class = LEGACY_TIER_TO_CLASS.get(raw)
    if capability_class is None:
        return None, None
    request = capability_request(capability_class)
    diagnostic = (
        f"legacy tier {raw!r} mapped to capability class "
        f"{capability_class!r}; persist capability explicitly to migrate"
    )
    return request, diagnostic


def map_legacy_capabilities(
    value: Mapping[str, Any],
    *,
    context: str,
) -> tuple[dict[str, Any], list[str]]:
    """Return a read-only normalised copy while retaining compatibility input."""
    mapped = copy.deepcopy(dict(value))
    warnings: list[str] = []
    if not mapped.get("capability") and mapped.get("tier"):
        request, diagnostic = from_legacy_tier(mapped["tier"])
        if request:
            mapped["capability"] = request
            warnings.append(f"{context}: {diagnostic}")
    return mapped, warnings


def validate_capability(value: Mapping[str, Any] | None) -> list[str]:
    """Return validation errors for a capability request without raising."""
    if value is None:
        return []
    errors: list[str] = []
    version = str(value.get("version") or "")
    if version != CAPABILITY_SCHEMA_VERSION:
        errors.append(
            f"capability.version: {version!r} must be {CAPABILITY_SCHEMA_VERSION!r}"
        )
    capability_class = str(value.get("class") or "")
    if capability_class not in CAPABILITY_CLASSES:
        errors.append(
            f"capability.class: {capability_class!r} not in "
            f"{list(CAPABILITY_CLASSES)!r}"
        )
    requirements = value.get("requirements") or {}
    if not isinstance(requirements, Mapping):
        errors.append("capability.requirements: must be an object")
        return errors
    unknown = sorted(set(requirements) - set(REQUIREMENT_LEVELS))
    if unknown:
        errors.append(f"capability.requirements: unknown keys {unknown!r}")
    for key, levels in REQUIREMENT_LEVELS.items():
        if key not in requirements or requirements[key] in (None, ""):
            continue
        actual = requirements[key]
        if actual not in levels:
            errors.append(
                f"capability.requirements.{key}: {actual!r} not in {list(levels)!r}"
            )
    return errors


def _rank(value: str, levels: tuple[str, ...]) -> int:
    try:
        return levels.index(value)
    except ValueError:
        return -1


def _effective_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = capability_request(
        str(value.get("class") or "general"),
        requirements=value.get("requirements") or {},
    )
    risk = request["requirements"].get("risk", "low")
    if risk in {"elevated", "critical"}:
        request["class"] = "orchestrator"
        request["requirements"]["verification"] = "strict"
    return request


def worker_satisfies(
    request: Mapping[str, Any],
    worker: Mapping[str, Any],
) -> bool:
    """Return whether one runtime-advertised worker meets every hard floor."""
    effective = _effective_request(request)
    offered = worker.get("capability") or {}
    if _rank(str(offered.get("class") or ""), CAPABILITY_CLASSES) < _rank(
        effective["class"], CAPABILITY_CLASSES
    ):
        return False
    available = offered.get("requirements") or {}
    for key, required in effective["requirements"].items():
        levels = REQUIREMENT_LEVELS.get(key)
        if levels is None:
            return False
        if _rank(str(available.get(key) or ""), levels) < _rank(required, levels):
            return False
    return True


@dataclass(frozen=True)
class MatchResult:
    """A runtime selection with explicit fallback and escalation signals."""

    worker: Mapping[str, Any] | None
    requested: Mapping[str, Any]
    policy: str
    fallback: str | None = None
    escalation_required: bool = False
    reasoning_adjustment: str | None = None


def _worker_key(worker: Mapping[str, Any]) -> tuple[int, float, str]:
    offered = worker.get("capability") or {}
    rank = _rank(str(offered.get("class") or ""), CAPABILITY_CLASSES)
    cost = worker.get("cost", float("inf"))
    try:
        cost_number = float(cost)
    except (TypeError, ValueError):
        cost_number = float("inf")
    return rank, cost_number, str(worker.get("id") or "")


def _eligible_workers(
    workers: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [worker for worker in workers if worker.get("general_purpose", True)]


def _same_family_workers(
    workers: Iterable[Mapping[str, Any]],
    orchestrator: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if orchestrator is None or not orchestrator.get("family"):
        return []
    return [
        worker
        for worker in workers
        if worker.get("family") == orchestrator.get("family")
    ]


def match_worker(
    request: Mapping[str, Any],
    workers: Iterable[Mapping[str, Any]],
    *,
    orchestrator: Mapping[str, Any] | None = None,
    policy: str = "least-sufficient",
) -> MatchResult:
    """Select a worker deterministically from runtime-advertised capabilities.

    ``one-below`` raises the requested class to the class immediately below the
    orchestrator. Elevated risk always retains the orchestrator class. When no
    advertised worker satisfies the request, the safest available worker is
    returned with ``escalation_required=True`` instead of silently weakening
    the contract.
    """
    effective = _effective_request(request)
    if policy not in {"least-sufficient", "one-below"}:
        raise ValueError(f"unknown capability policy {policy!r}")

    if policy == "one-below" and orchestrator is not None:
        orchestrator_capability = orchestrator.get("capability") or {}
        orchestrator_rank = _rank(
            str(orchestrator_capability.get("class") or ""), CAPABILITY_CLASSES
        )
        if orchestrator_rank >= 0:
            one_below_rank = max(0, orchestrator_rank - 1)
            request_rank = _rank(effective["class"], CAPABILITY_CLASSES)
            effective["class"] = CAPABILITY_CLASSES[max(one_below_rank, request_rank)]

    pool = _eligible_workers(workers)
    if not pool:
        return MatchResult(
            worker=None,
            requested=effective,
            policy=policy,
            fallback="inline-no-advertised-worker",
            escalation_required=True,
        )

    preferred_pool = _same_family_workers(pool, orchestrator)
    satisfying = sorted(
        (worker for worker in preferred_pool if worker_satisfies(effective, worker)),
        key=_worker_key,
    )
    if not satisfying:
        satisfying = sorted(
            (worker for worker in pool if worker_satisfies(effective, worker)),
            key=_worker_key,
        )
    if satisfying:
        chosen = satisfying[0]
        reasoning_adjustment = None
        if (
            policy == "one-below"
            and orchestrator is not None
            and chosen.get("id") == orchestrator.get("id")
        ):
            reasoning_adjustment = "decrease-one-supported-level"
        return MatchResult(
            worker=chosen,
            requested=effective,
            policy=policy,
            reasoning_adjustment=reasoning_adjustment,
        )

    strongest_rank = max(_worker_key(worker)[0] for worker in pool)
    strongest = [worker for worker in pool if _worker_key(worker)[0] == strongest_rank]
    safest = min(
        strongest,
        key=lambda worker: (_worker_key(worker)[1], _worker_key(worker)[2]),
    )
    return MatchResult(
        worker=safest,
        requested=effective,
        policy=policy,
        fallback="strongest-advertised-worker",
        escalation_required=True,
    )
