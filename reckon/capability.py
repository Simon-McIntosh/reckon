"""Portable task-capability requests and explicit runtime validation.

Persisted plans describe the ability and safeguards a task needs. Concrete
worker identities remain runtime data, are chosen by the current prompt or
coordinator, and are validated only when work is dispatched.
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
    """Validation of one explicitly selected runtime worker."""

    worker: Mapping[str, Any] | None
    requested: Mapping[str, Any]
    policy: str = "explicit"
    fallback: str | None = None
    escalation_required: bool = False


def _eligible_workers(
    workers: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [worker for worker in workers if worker.get("general_purpose", True)]


def match_worker(
    request: Mapping[str, Any],
    workers: Iterable[Mapping[str, Any]],
    *,
    selected_worker_id: str,
) -> MatchResult:
    """Validate the exact runtime worker chosen by the prompt or coordinator."""
    effective = _effective_request(request)
    pool = _eligible_workers(workers)
    selected = next(
        (worker for worker in pool if worker.get("id") == selected_worker_id),
        None,
    )
    if selected is None:
        return MatchResult(
            worker=None,
            requested=effective,
            fallback="selected-worker-unavailable",
            escalation_required=True,
        )
    if worker_satisfies(effective, selected):
        return MatchResult(
            worker=selected,
            requested=effective,
        )
    return MatchResult(
        worker=selected,
        requested=effective,
        fallback="selected-worker-insufficient",
        escalation_required=True,
    )
