"""Dependency-aware pending-work and roadmap analysis.

The analyzer is deliberately storage-neutral: callers pass the composed
inventory and sprint resources returned by discovery.  This keeps the same
semantics available to MCP, the CLI, audits, and tests without reimplementing
graph traversal in each surface.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from reckon._schema import parse_plan_ref
from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

_EFFORT_WEIGHT = {"S": 1.0, "M": 2.0, "L": 4.0, "XL": 8.0}
_ROI_ORDER = {"high": 0, "mid": 1, "med": 1, "low": 2}
_RUNNABLE_STATUSES = frozenset({"pending", "active", "in-progress"})


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


def _remaining_effort(plan: dict[str, Any]) -> float:
    weight = _EFFORT_WEIGHT.get(str(plan.get("effort") or "M").upper(), 2.0)
    return round(weight * (1.0 - _progress(plan)), 3)


def _status(plan: dict[str, Any]) -> str:
    return str(plan.get("workflow_status") or plan.get("status") or "draft")


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
    for position, sprint in enumerate(sprints):
        sprint_id = str(sprint.get("id") or "")
        if not sprint_id:
            continue
        order[sprint_id] = position
        for item in sprint.get("items") or []:
            slug = _item_slug(item)
            if slug:
                membership[slug].append(sprint_id)
    return dict(membership), order


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
        if not isinstance(raw, dict) or not raw.get("slug"):
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

    for slug, plan in plans.items():
        status = _status(plan)
        if status in TERMINAL_STATUSES:
            continue
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
        if status == "blocked" and not explicit_blockers and not dependency_blockers:
            explicit_blockers = [{"kind": "persisted", "id": "unrecorded"}]
        is_ready = (
            status in _RUNNABLE_STATUSES
            and not dependency_blockers
            and not explicit_blockers
            and slug not in cycle_members
        )
        row = {
            "slug": slug,
            "title": plan.get("title") or slug,
            "status": status,
            "effective_status": plan.get("effective_status") or status,
            "sprint": plan.get("sprint") or (membership.get(slug) or [None])[0],
            "roi": plan.get("roi") or "mid",
            "effort": plan.get("effort") or "M",
            "progress_pct": round(_progress(plan) * 100, 1),
            "remaining_effort": _remaining_effort(plan),
            "depends_on": dependency_rows.get(slug, []),
            "explicit_blockers": explicit_blockers,
            "unlocks": sorted(dependents.get(slug, set())),
            "ready": is_ready,
        }
        pending.append(row)
        (ready if is_ready else blocked).append(row)

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
            key=lambda path: sum(_remaining_effort(plans[item]) for item in path),
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
                sum(_remaining_effort(plans[item]) for item in path), 3
            )
    sorted_paths = sorted(path_candidates.items(), key=lambda item: (-item[1], item[0]))
    open_paths = [
        {"plans": list(path), "remaining_effort": effort}
        for path, effort in sorted_paths[: max(1, max_paths)]
    ]
    critical = open_paths[0] if open_paths else {"plans": [], "remaining_effort": 0.0}
    critical_members = set(critical["plans"])

    def priority(row: dict[str, Any]) -> tuple[Any, ...]:
        sprint_position = sprint_order.get(str(row.get("sprint") or ""), 10**6)
        return (
            0 if row["slug"] in critical_members else 1,
            sprint_position,
            _ROI_ORDER.get(str(row.get("roi") or "mid").lower(), 1),
            -len(row.get("unlocks") or []),
            row["remaining_effort"],
            row["slug"],
        )

    ready.sort(key=priority)
    pending.sort(key=lambda row: (not row["ready"], *priority(row)))
    blocked.sort(key=priority)
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
            }
        )

    plan_values = list(plans.values())
    completed_count = sum(_status(plan) in COMPLETED_STATUSES for plan in plan_values)
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
        "allocation": {
            "configured": bool(allocation),
            "scope": allocation,
            "note": (
                "Validate plan ownership against project scope and repository instructions before creation or relocation."
            ),
        },
        "sprints": sprint_rows,
        "pending_work": pending,
        "ready_now": ready,
        "blocked": blocked,
        "immediate_roadmap": immediate,
        "critical_path": critical,
        "open_paths": open_paths,
        "cycles": cycles,
        "wiring_findings": findings,
    }
