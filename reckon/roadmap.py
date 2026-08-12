"""Dependency-aware pending-work and roadmap analysis.

The analyzer is deliberately storage-neutral: callers pass the composed
inventory and sprint resources returned by discovery.  This keeps the same
semantics available to MCP, the CLI, audits, and tests without reimplementing
graph traversal in each surface.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from reckon._schema import LEGACY_EFFORT_HOURS, parse_plan_ref
from reckon.doccheck import _load_mounts, authorisation_staleness, derived_plan_age
from reckon.lifecycle import (
    COMPLETED_STATUSES,
    TERMINAL_STATUSES,
    effective_status,
    unpassed_gate_blockers,
)

_EFFORT_UNIT = "worker-hours"
_ROI_ORDER = {"high": 0, "mid": 1, "med": 1, "low": 2}
_AUTHORISED_STATUSES = frozenset({"pending", "active", "in-progress"})


def _item_slug(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("slug") or "")
    return ""


def _progress(plan: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(plan.get("impl", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _effort_hours(plan: dict[str, Any]) -> float:
    explicit = plan.get("effort_hours")
    if explicit is not None:
        try:
            return max(0.0, float(explicit))
        except (TypeError, ValueError):
            pass
    return LEGACY_EFFORT_HOURS.get(
        str(plan.get("effort") or "M").upper(),
        LEGACY_EFFORT_HOURS["M"],
    )


def _effort_calibrated(plan: dict[str, Any]) -> bool:
    if "effort_calibrated" in plan:
        return plan.get("effort_calibrated") is True
    return plan.get("effort_hours") is not None


def _remaining_effort_hours(plan: dict[str, Any]) -> float:
    return round(_effort_hours(plan) * (1.0 - _progress(plan)), 3)


def _uncalibrated_plans(
    plans: dict[str, dict[str, Any]], slugs: list[str] | set[str] | None = None
) -> list[str]:
    selected = sorted(slugs if slugs is not None else plans)
    return [
        slug
        for slug in selected
        if _remaining_effort_hours(plans[slug]) > 0
        and not _effort_calibrated(plans[slug])
    ]


def _north_star_rows(
    plans: dict[str, dict[str, Any]],
    declarations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Roll up the plans aligned with each declared durable direction."""

    rows: list[dict[str, Any]] = []
    for declaration in declarations:
        north_star_id = str(declaration.get("id") or "").strip()
        if not north_star_id:
            continue
        aligned = [
            plan
            for plan in plans.values()
            if str(plan.get("north_star") or "").strip() == north_star_id
        ]
        completed = sum(_status(plan) in COMPLETED_STATUSES for plan in aligned)
        row = dict(declaration)
        row.update(
            {
                "plans": len(aligned),
                "completed": completed,
                "lifecycle_completion_pct": (
                    round(100 * completed / len(aligned), 1) if aligned else 0.0
                ),
                "effort_unit": _EFFORT_UNIT,
                "remaining_effort_hours": round(
                    sum(_remaining_effort_hours(plan) for plan in aligned), 3
                ),
                "uncalibrated_plans": sorted(
                    str(plan.get("slug") or "")
                    for plan in aligned
                    if _remaining_effort_hours(plan) > 0
                    and not _effort_calibrated(plan)
                ),
                "uncalibrated_count": sum(
                    _remaining_effort_hours(plan) > 0 and not _effort_calibrated(plan)
                    for plan in aligned
                ),
            }
        )
        rows.append(row)
    return rows


def _status(plan: dict[str, Any]) -> str:
    return str(plan.get("workflow_status") or plan.get("status") or "draft")


def _dispatchability(plan: dict[str, Any]) -> tuple[bool, list[str]]:
    gates = [gate for gate in plan.get("gates") or [] if isinstance(gate, dict)]
    if not gates:
        return True, []

    missing: list[str] = []
    if not any(
        isinstance(followup, dict) and followup.get("status", "open") == "open"
        for followup in plan.get("followups") or []
    ):
        missing.append("open_followup")
    return not missing, missing


def _authorisation_age(
    project: str,
    plan: dict[str, Any],
) -> tuple[int | None, str]:
    recorded_age = derived_plan_age(
        plan.get("modified") or plan.get("last"),
        created_at=plan.get("created"),
    )
    if recorded_age[0] is not None:
        return recorded_age

    fallback_path = None
    docs_dir = _load_mounts().get(project)
    if docs_dir is not None:
        from reckon.resources import resolve_resource

        try:
            resource = resolve_resource(
                docs_dir,
                project,
                str(plan.get("slug") or ""),
                "plan",
            )
        except (OSError, ValueError):
            resource = None
        if resource is not None:
            fallback_path = resource.path
    return derived_plan_age(
        plan.get("modified") or plan.get("last"),
        fallback_path=fallback_path,
    )


def _blocking_row(plan: dict[str, Any], ref: str) -> dict[str, Any] | None:
    for row in plan.get("blocking") or []:
        if isinstance(row, dict) and row.get("ref") == ref:
            return row
    return None


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    slug: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "category": "dag",
        "code": code,
        "severity": severity,
        "message": message,
    }
    if slug:
        result["slug"] = slug
    if extra:
        result["extra"] = extra
    return result


def _canonical_cycle(nodes: list[str]) -> tuple[str, ...]:
    cycle = nodes[:-1] if len(nodes) > 1 and nodes[0] == nodes[-1] else nodes
    if not cycle:
        return ()
    rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
    return min(rotations)


def _dependency_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    found: set[tuple[str, ...]] = set()
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()

    def visit(node: str) -> None:
        if node in active_set:
            start = active.index(node)
            found.add(_canonical_cycle([*active[start:], node]))
            return
        if node in visited:
            return
        active.append(node)
        active_set.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        active.pop()
        active_set.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)
    return [list(cycle) + [cycle[0]] for cycle in sorted(found) if cycle]


def _sprint_membership(
    sprints: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    membership: dict[str, list[str]] = defaultdict(list)
    order: dict[str, int] = {}
    closed_statuses = {"done", "shipped", "archived"}
    for position, sprint in enumerate(sprints):
        sprint_id = str(sprint.get("id") or "")
        if not sprint_id:
            continue
        order[sprint_id] = position
        if str(sprint.get("status") or "").lower() in closed_statuses:
            continue
        for item in sprint.get("items") or []:
            slug = _item_slug(item)
            if slug:
                membership[slug].append(sprint_id)
    return dict(membership), order


def _sprint_of(
    slug: str, plan: dict[str, Any], membership: dict[str, list[str]]
) -> list[str]:
    """Return the sprint ids a plan belongs to, by membership or declaration."""
    assigned = list(membership.get(slug) or [])
    declared = str(plan.get("sprint") or "")
    if declared and declared not in assigned:
        assigned.append(declared)
    return assigned


def _downstream_sprints(
    all_plans: dict[str, dict[str, Any]],
    membership: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """Derive, per sprint, the sprints its plans unblock.

    A sprint close owes an answer to "what does this let us start?", and the
    dependency graph already knows: a plan depending on one of this sprint's
    plans is downstream work, and the sprint holding it is a sprint this one
    feeds. Deriving it means the answer cannot go stale the way a written list
    would, and a sprint that feeds nothing says so rather than staying silent.
    """
    edges: dict[str, list[dict[str, str]]] = defaultdict(list)
    for slug, plan in sorted(all_plans.items()):
        owning_project = str(plan.get("project") or "")
        for ref in plan.get("depends_on") or []:
            parsed = parse_plan_ref(ref)
            if parsed is None or parsed.is_external(owning_project):
                continue
            prerequisite = all_plans.get(parsed.slug)
            if prerequisite is None:
                continue
            upstream_sprints = _sprint_of(parsed.slug, prerequisite, membership)
            downstream_sprints = _sprint_of(slug, plan, membership)
            for upstream in upstream_sprints:
                for downstream in downstream_sprints:
                    if downstream == upstream:
                        continue
                    edges[upstream].append(
                        {"plan": slug, "sprint": downstream, "via": parsed.slug}
                    )
    return {
        sprint_id: {
            "feeds_sprints": sorted({row["sprint"] for row in rows}),
            "unblocks": sorted(
                rows, key=lambda row: (row["sprint"], row["plan"], row["via"])
            ),
        }
        for sprint_id, rows in edges.items()
    }


def _scope_slugs(
    plans: dict[str, dict[str, Any]],
    membership: dict[str, list[str]],
    sprint_id: str | None,
) -> set[str]:
    if not sprint_id:
        return set(plans)
    selected = {
        slug
        for slug, plan in plans.items()
        if sprint_id in membership.get(slug, []) or plan.get("sprint") == sprint_id
    }
    pending = list(selected)
    while pending:
        slug = pending.pop()
        if _status(plans[slug]) in TERMINAL_STATUSES:
            continue
        for ref in plans[slug].get("depends_on") or []:
            parsed = parse_plan_ref(ref)
            if (
                parsed is not None
                and not parsed.is_external(str(plans[slug].get("project") or ""))
                and parsed.slug in plans
                and parsed.slug not in selected
            ):
                selected.add(parsed.slug)
                pending.append(parsed.slug)
    return selected


def build_roadmap(
    project: str,
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    *,
    active_sprint_id: str | None = None,
    sprint_id: str | None = None,
    max_paths: int = 5,
    project_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return pending work, execution paths, progress, blockers, and wiring findings."""

    artifacts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_plans: dict[str, dict[str, Any]] = {}
    for raw in inventory:
        if not isinstance(raw, dict) or not raw.get("slug") or raw.get("archived"):
            continue
        item = dict(raw)
        item.setdefault("project", project)
        artifacts[str(item["slug"])].append(item)
        if item.get("type", "plan") == "plan":
            all_plans[str(item["slug"])] = item

    membership, sprint_order = _sprint_membership(sprints)
    selected_slugs = _scope_slugs(all_plans, membership, sprint_id)
    plans = {slug: all_plans[slug] for slug in selected_slugs}
    findings: list[dict[str, Any]] = []
    dependency_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    local_graph: dict[str, list[str]] = defaultdict(list)
    dependents: dict[str, set[str]] = defaultdict(set)
    north_stars = [
        dict(item)
        for item in (project_manifest or {}).get("north_stars", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    declared_north_stars = {str(item["id"]).strip() for item in north_stars}

    for slug, plan in plans.items():
        status = _status(plan)
        if status in TERMINAL_STATUSES:
            continue
        if declared_north_stars:
            north_star = str(plan.get("north_star") or "").strip()
            if north_star and north_star not in declared_north_stars:
                findings.append(
                    _finding(
                        "undeclared-north-star",
                        "error",
                        (
                            f"{slug}: north-star {north_star!r} is not declared "
                            f"by project {project!r}"
                        ),
                        slug=slug,
                        extra={"north_star": north_star},
                    )
                )
            elif not north_star and status in _AUTHORISED_STATUSES:
                findings.append(
                    _finding(
                        "unoriented-plan",
                        "info",
                        f"{slug}: live actionable work declares no north-star",
                        slug=slug,
                    )
                )
        if status == "blocked" and not (plan.get("blocking") or []):
            findings.append(
                _finding(
                    "orphaned-blocked-status",
                    "warn",
                    f"{slug}: persisted blocked status has no recorded blocker",
                    slug=slug,
                )
            )

        depends_on = list(plan.get("depends_on") or [])
        blocks = set(plan.get("blocks") or [])
        for ref in depends_on:
            parsed = parse_plan_ref(ref)
            if parsed is None:
                dependency_rows[slug].append(
                    {"ref": ref, "scope": "invalid", "found": False, "satisfied": False}
                )
                findings.append(
                    _finding(
                        "invalid-hard-dependency",
                        "error",
                        f"{slug}: dependency {ref!r} is not a valid plan reference",
                        slug=slug,
                        extra={"ref": ref},
                    )
                )
                continue

            external = parsed.is_external(project)
            if external:
                blocking = _blocking_row(plan, ref)
                satisfied = blocking is None
                row = {
                    "ref": ref,
                    "scope": "external",
                    "project": parsed.project,
                    "slug": parsed.slug,
                    "found": bool(blocking.get("found")) if blocking else True,
                    "status": blocking.get("status", "") if blocking else "satisfied",
                    "satisfied": satisfied,
                }
                dependency_rows[slug].append(row)
                if not satisfied and not row["found"]:
                    findings.append(
                        _finding(
                            "unresolved-external-dependency",
                            "error",
                            f"{slug}: external dependency {ref!r} does not resolve",
                            slug=slug,
                            extra={"ref": ref},
                        )
                    )
                continue

            target = all_plans.get(parsed.slug)
            if target is None:
                alternatives = [
                    item.get("type", "plan")
                    for item in artifacts.get(parsed.slug, [])
                    if item.get("type", "plan") != "plan"
                ]
                dependency_rows[slug].append(
                    {
                        "ref": ref,
                        "scope": "local",
                        "slug": parsed.slug,
                        "found": False,
                        "satisfied": False,
                        "artifact_types": alternatives,
                    }
                )
                if alternatives:
                    findings.append(
                        _finding(
                            "non-executable-hard-dependency",
                            "error",
                            (
                                f"{slug}: {ref!r} resolves only as "
                                f"{', '.join(sorted(set(alternatives)))}; use informs "
                                "for reference inputs"
                            ),
                            slug=slug,
                            extra={"ref": ref, "artifact_types": alternatives},
                        )
                    )
                else:
                    findings.append(
                        _finding(
                            "dangling-hard-dependency",
                            "error",
                            f"{slug}: dependency {ref!r} does not resolve to a live plan",
                            slug=slug,
                            extra={"ref": ref},
                        )
                    )
                continue

            target_status = _status(target)
            satisfied = target_status in COMPLETED_STATUSES
            dependency_rows[slug].append(
                {
                    "ref": ref,
                    "scope": "local",
                    "slug": parsed.slug,
                    "found": True,
                    "status": target_status,
                    "satisfied": satisfied,
                }
            )
            if not satisfied and parsed.slug in plans:
                local_graph[slug].append(parsed.slug)
                dependents[parsed.slug].add(slug)
            if target_status in TERMINAL_STATUSES - COMPLETED_STATUSES:
                findings.append(
                    _finding(
                        "inactive-hard-dependency",
                        "error",
                        f"{slug}: dependency {ref!r} is {target_status}, not complete",
                        slug=slug,
                        extra={"ref": ref, "status": target_status},
                    )
                )
            if ref in blocks or parsed.slug in blocks:
                findings.append(
                    _finding(
                        "contradictory-hard-relation",
                        "error",
                        f"{slug}: {ref!r} appears in both depends_on and blocks",
                        slug=slug,
                        extra={"ref": ref},
                    )
                )

            source_sprints = membership.get(slug) or (
                [plan.get("sprint")] if plan.get("sprint") else []
            )
            target_sprints = membership.get(parsed.slug) or (
                [target.get("sprint")] if target.get("sprint") else []
            )
            if source_sprints and target_sprints:
                source_position = min(
                    (sprint_order.get(str(value), 10**6) for value in source_sprints),
                    default=10**6,
                )
                target_position = min(
                    (sprint_order.get(str(value), 10**6) for value in target_sprints),
                    default=10**6,
                )
                if source_position < target_position:
                    findings.append(
                        _finding(
                            "sprint-order-inversion",
                            "error",
                            (
                                f"{slug}: scheduled before prerequisite {parsed.slug} "
                                "in the sprint sequence"
                            ),
                            slug=slug,
                            extra={"dependency": parsed.slug},
                        )
                    )

        if len(membership.get(slug, [])) > 1:
            findings.append(
                _finding(
                    "duplicate-sprint-membership",
                    "error",
                    f"{slug}: appears in multiple sprints: {', '.join(membership[slug])}",
                    slug=slug,
                    extra={"sprints": membership[slug]},
                )
            )
        declared_sprint = str(plan.get("sprint") or "")
        assigned_sprints = membership.get(slug, [])
        if declared_sprint and declared_sprint not in assigned_sprints:
            findings.append(
                _finding(
                    "plan-sprint-missing-item",
                    "warn",
                    f"{slug}: declares sprint {declared_sprint} but is absent from its items",
                    slug=slug,
                    extra={"sprint": declared_sprint},
                )
            )
        if (
            assigned_sprints
            and declared_sprint
            and declared_sprint not in assigned_sprints
        ):
            findings.append(
                _finding(
                    "plan-sprint-mismatch",
                    "warn",
                    f"{slug}: plan sprint and sprint membership disagree",
                    slug=slug,
                    extra={"declared": declared_sprint, "assigned": assigned_sprints},
                )
            )
        if (
            not declared_sprint
            and not assigned_sprints
            and status not in TERMINAL_STATUSES
        ):
            findings.append(
                _finding(
                    "unscheduled-open-plan",
                    "info",
                    f"{slug}: open work is not linked to a sprint",
                    slug=slug,
                )
            )

    cycles = _dependency_cycles(dict(local_graph))
    cycle_members = {slug for cycle in cycles for slug in cycle[:-1]}
    for cycle in cycles:
        findings.append(
            _finding(
                "dependency-cycle",
                "error",
                "dependency cycle: " + " -> ".join(cycle),
                slug=cycle[0],
                extra={"cycle": cycle},
            )
        )

    pending: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    unauthorised: list[dict[str, Any]] = []
    for slug, plan in plans.items():
        status = _status(plan)
        if status in TERMINAL_STATUSES:
            continue
        dependency_blockers = [
            row for row in dependency_rows.get(slug, []) if not row.get("satisfied")
        ]
        explicit_blockers = [
            row
            for row in plan.get("blocking") or []
            if isinstance(row, dict) and row.get("kind") == "explicit"
        ]
        gate_blockers = unpassed_gate_blockers(plan.get("gates") or [])
        if (
            status == "blocked"
            and not explicit_blockers
            and not dependency_blockers
            and not gate_blockers
        ):
            explicit_blockers = [{"kind": "persisted", "id": "unrecorded"}]
        dispatchable, missing_dispatchability = _dispatchability(plan)
        authorised = status in _AUTHORISED_STATUSES
        is_ready = (
            dispatchable
            and authorised
            and not dependency_blockers
            and not explicit_blockers
            and not gate_blockers
            and slug not in cycle_members
        )
        is_blocked = bool(
            dependency_blockers
            or explicit_blockers
            or gate_blockers
            or slug in cycle_members
        )
        readiness = "ready" if is_ready else "blocked" if is_blocked else "deferred"
        row = {
            "slug": slug,
            "title": plan.get("title") or slug,
            "status": status,
            "dispatchable": dispatchable,
            "missing_dispatchability": missing_dispatchability,
            "authorised": authorised,
            "effective_status": effective_status(
                status, [*dependency_blockers, *explicit_blockers, *gate_blockers]
            ),
            "sprint": plan.get("sprint") or (membership.get(slug) or [None])[0],
            "roi": plan.get("roi") or "mid",
            "effort": plan.get("effort") or "M",
            "effort_hours": _effort_hours(plan),
            "effort_calibrated": _effort_calibrated(plan),
            "progress_pct": round(_progress(plan) * 100, 1),
            "remaining_effort_hours": _remaining_effort_hours(plan),
            "depends_on": dependency_rows.get(slug, []),
            "explicit_blockers": explicit_blockers,
            "gate_blockers": gate_blockers,
            "unlocks": sorted(dependents.get(slug, set())),
            "ready": is_ready,
            "readiness": readiness,
        }
        pending.append(row)
        if status == "draft":
            age_days, age_source = _authorisation_age(project, plan)
            unauthorised.append(
                {
                    "slug": slug,
                    "title": plan.get("title") or slug,
                    "status": status,
                    "age_days": age_days,
                    "age_source": age_source,
                    "age_verdict": authorisation_staleness(
                        status=status,
                        age_days=age_days,
                    ),
                    "dispatchable": dispatchable,
                    "missing_dispatchability": missing_dispatchability,
                }
            )
        if is_ready:
            ready.append(row)
        elif is_blocked:
            blocked.append(row)
        else:
            deferred.append(row)

    def longest_path(node: str, visiting: frozenset[str] = frozenset()) -> list[str]:
        if node in visiting or node in cycle_members:
            return []
        candidates = [
            longest_path(dependency, visiting | {node})
            for dependency in local_graph.get(node, [])
            if dependency in plans
        ]
        best = max(
            candidates,
            key=lambda path: sum(_remaining_effort_hours(plans[item]) for item in path),
            default=[],
        )
        return [*best, node]

    path_candidates: dict[tuple[str, ...], float] = {}
    for slug, plan in plans.items():
        if _status(plan) in TERMINAL_STATUSES:
            continue
        path = longest_path(slug)
        if path:
            path_candidates[tuple(path)] = round(
                sum(_remaining_effort_hours(plans[item]) for item in path), 3
            )
    sorted_paths = sorted(path_candidates.items(), key=lambda item: (-item[1], item[0]))
    open_paths = []
    for path, length_hours in sorted_paths[: max(1, max_paths)]:
        path_plans = list(path)
        uncalibrated = _uncalibrated_plans(plans, path_plans)
        open_paths.append(
            {
                "plans": path_plans,
                "length_hours": length_hours,
                "effort_unit": _EFFORT_UNIT,
                "uncalibrated_plans": uncalibrated,
                "uncalibrated_count": len(uncalibrated),
            }
        )
    critical = (
        open_paths[0]
        if open_paths
        else {
            "plans": [],
            "length_hours": 0.0,
            "effort_unit": _EFFORT_UNIT,
            "uncalibrated_plans": [],
            "uncalibrated_count": 0,
        }
    )
    critical_members = set(critical["plans"])

    def priority(row: dict[str, Any]) -> tuple[Any, ...]:
        sprint_position = sprint_order.get(str(row.get("sprint") or ""), 10**6)
        return (
            0 if row["slug"] in critical_members else 1,
            sprint_position,
            _ROI_ORDER.get(str(row.get("roi") or "mid").lower(), 1),
            -len(row.get("unlocks") or []),
            row["remaining_effort_hours"],
            row["slug"],
        )

    ready.sort(key=priority)
    pending.sort(key=lambda row: (not row["ready"], *priority(row)))
    blocked.sort(key=priority)
    deferred.sort(key=priority)
    unauthorised.sort(
        key=lambda row: (
            row["age_days"] is None,
            -(row["age_days"] or 0),
            row["slug"],
        )
    )
    immediate = [
        {
            "order": position,
            "slug": row["slug"],
            "sprint": row["sprint"],
            "progress_pct": row["progress_pct"],
            "unlocks": row["unlocks"],
            "reason": (
                "critical path"
                if row["slug"] in critical_members
                else "ready with all hard prerequisites satisfied"
            ),
        }
        for position, row in enumerate(ready, start=1)
    ]

    downstream = _downstream_sprints(all_plans, membership)
    sprint_rows: list[dict[str, Any]] = []
    for position, sprint in enumerate(sprints, start=1):
        sprint_name = str(sprint.get("id") or "")
        sprint_status = str(sprint.get("status") or "planned")
        items = [_item_slug(item) for item in sprint.get("items") or []]
        items = [slug for slug in items if slug]
        item_plans = [all_plans[slug] for slug in items if slug in all_plans]
        completed = sum(_status(plan) in COMPLETED_STATUSES for plan in item_plans)
        progress = sum(_progress(plan) for plan in item_plans)
        sprint_rows.append(
            {
                "order": position,
                "id": sprint_name,
                "status": sprint_status,
                "items": len(items),
                "resolved_items": len(item_plans),
                "completed": completed,
                "lifecycle_completion_pct": round(100 * completed / len(items), 1)
                if items
                else (100.0 if sprint_status in COMPLETED_STATUSES else 0.0),
                "implementation_pct": round(100 * progress / len(item_plans), 1)
                if item_plans
                else (100.0 if sprint_status in COMPLETED_STATUSES else 0.0),
                "ready": sum(row.get("sprint") == sprint_name for row in ready),
                "blocked": sum(row.get("sprint") == sprint_name for row in blocked),
                "deferred": sum(row.get("sprint") == sprint_name for row in deferred),
                "feeds_sprints": downstream.get(sprint_name, {}).get(
                    "feeds_sprints", []
                ),
                "unblocks": downstream.get(sprint_name, {}).get("unblocks", []),
            }
        )

    plan_values = list(plans.values())
    completed_count = sum(_status(plan) in COMPLETED_STATUSES for plan in plan_values)
    uncalibrated = _uncalibrated_plans(plans)
    allocation = (project_manifest or {}).get("scope") or {}
    return {
        "project": project,
        "scope": {"sprint": sprint_id, "plans": len(plan_values)},
        "active_sprint_id": active_sprint_id,
        "completion": {
            "plans": len(plan_values),
            "completed": completed_count,
            "pending": len(pending),
            "lifecycle_completion_pct": round(
                100 * completed_count / len(plan_values), 1
            )
            if plan_values
            else 0.0,
            "implementation_pct": round(
                100 * sum(_progress(plan) for plan in plan_values) / len(plan_values),
                1,
            )
            if plan_values
            else 0.0,
        },
        "effort": {
            "unit": _EFFORT_UNIT,
            "remaining_hours": round(
                sum(_remaining_effort_hours(plan) for plan in plan_values), 3
            ),
            "uncalibrated_plans": uncalibrated,
            "uncalibrated_count": len(uncalibrated),
        },
        "allocation": {
            "configured": bool(allocation),
            "scope": allocation,
            "note": (
                "Validate plan ownership against project scope and repository instructions before creation or relocation."
            ),
        },
        "north_stars": _north_star_rows(plans, north_stars),
        "sprints": sprint_rows,
        "pending_work": pending,
        "ready_now": ready,
        "blocked": blocked,
        "deferred": deferred,
        "authorisation": {
            "authored_but_unauthorised": unauthorised,
            "count": len(unauthorised),
            "stale_count": sum(row["age_verdict"] == "stale" for row in unauthorised),
        },
        "immediate_roadmap": immediate,
        "critical_path": critical,
        "open_paths": open_paths,
        "cycles": cycles,
        "wiring_findings": findings,
    }
