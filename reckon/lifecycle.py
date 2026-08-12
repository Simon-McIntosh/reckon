"""Derived lifecycle state for plans and sprint items."""

from __future__ import annotations

from typing import Any

COMPLETED_STATUSES = frozenset({"shipped", "done"})
LEGACY_BLOCKED_OPEN_STATUS = "active"
TERMINAL_STATUSES = frozenset(
    {
        *COMPLETED_STATUSES,
        "superseded",
        "abandoned",
        "archived",
        "historical",
        "reference",
    }
)


def unresolved_dependencies(deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return dependency rows that do not resolve to completed plans."""

    return [
        {
            "ref": dep.get("ref", ""),
            "found": bool(dep.get("found")),
            "status": dep.get("status", ""),
        }
        for dep in deps
        if isinstance(dep, dict)
        and (not dep.get("found") or dep.get("status") not in COMPLETED_STATUSES)
    ]


def unpassed_gate_blockers(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project gates without a passing verdict into blocker rows."""

    return [
        {
            "kind": "gate",
            "id": str(gate.get("id") or ""),
            "section": str(gate.get("section") or ""),
            "gated_sections": list(gate.get("gated_sections") or []),
            "status": str(gate.get("status") or ""),
            "measure": str(gate.get("measure") or ""),
            "verdict": str(gate.get("verdict") or ""),
            "evidence": str(gate.get("evidence") or ""),
        }
        for gate in gates
        if isinstance(gate, dict)
        and str(gate.get("verdict") or "").strip().lower() != "passed"
    ]


def effective_status(
    workflow_status: str | None,
    blocking: list[dict[str, Any]],
) -> str:
    """Project blockers over a plan's persisted workflow status.

    Terminal states remain terminal. Legacy persisted ``blocked`` uses
    ``active`` as its open-state compatibility fallback. Open work reads as
    blocked while any derived dependency or explicit blocker exists, then
    automatically returns to its persisted workflow state when those blockers
    clear.
    """

    status = str(workflow_status or "draft")
    if status == "blocked":
        status = LEGACY_BLOCKED_OPEN_STATUS
    if status in TERMINAL_STATUSES:
        return status
    return "blocked" if blocking else status
