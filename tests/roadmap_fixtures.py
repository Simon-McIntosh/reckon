"""The one plan-row builder the roadmap readers share.

A roadmap read is measured against inventory rows, and every test module that
needs one builds it here. One builder keeps the row shape in a single place: a
field rename is one edit rather than one per module, and a reader added later is
measured against the same rows the existing readers are.
"""

from __future__ import annotations

from typing import Any


def plan_row(
    slug: str,
    *,
    status: str = "active",
    impl: float = 0.0,
    depends_on: list[str] | None = None,
    sprint: str | None = None,
    effort: str = "M",
    effort_hours: float | None = None,
    effort_calibrated: bool | None = None,
    north_star: str | None = None,
    graph_handle: str | None = None,
    section_declarations: dict[str, str] | None = None,
    gates: list[dict[str, Any]] | None = None,
    followups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one inventory row with the fields a roadmap read consumes.

    ``section_declarations`` is omitted unless the caller passes it, because a
    row without persisted declarations is the legacy population the readiness
    ladder answers for rather than an error.
    """

    plan: dict[str, Any] = {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": status,
        "impl": impl,
        "depends_on": depends_on or [],
        "sprint": sprint,
        "effort": effort,
        "roi": "high",
        "blocking": [],
        "gates": gates
        if gates is not None
        else [{"id": "evidence", "verdict": "passed"}],
        "followups": followups
        if followups is not None
        else [
            {"id": "next", "status": "open", "prompt": "/reckon-build upstream-plan"}
        ],
    }
    if north_star is not None:
        plan["north_star"] = north_star
    if graph_handle is not None:
        plan["graph_handle"] = graph_handle
    if effort_hours is not None:
        plan["effort_hours"] = effort_hours
    if effort_calibrated is not None:
        plan["effort_calibrated"] = effort_calibrated
    if section_declarations is not None:
        plan["section_declarations"] = section_declarations
    return plan
