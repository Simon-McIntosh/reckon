"""Graph ship targets: one authored handle and the closure it resolves to.

A handle is authored on a single endpoint plan. Every member, metric and
readiness verdict is recomputed from the current plans and their dependency
edges, so nothing here is stored. The readiness and closure verdicts belong to
:mod:`reckon.roadmap` and are read from it at call time -- the roadmap imports
this module at its top, so a module-level import back would close a cycle. Each
function therefore reads the roadmap names it needs when it needs them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from reckon._plan_html import derive_fleet_impl_imputing_effort
from reckon._schema import GRAPH_HANDLE_GRAMMAR, is_graph_handle, parse_plan_ref
from reckon.lifecycle import COMPLETED_STATUSES


def _qualified_plan(project: str, slug: str) -> str:
    return f"{project}:{slug}"


def _claims_graph_handle(
    handle: str,
    projects: Mapping[str, Mapping[str, Any] | list[dict[str, Any]]],
) -> bool:
    for raw_state in projects.values():
        inventory = (
            raw_state
            if isinstance(raw_state, list)
            else raw_state.get("inventory") or []
        )
        if any(
            isinstance(plan, dict)
            and plan.get("type", "plan") == "plan"
            and plan.get("slug")
            and not plan.get("archived")
            and str(plan.get("graph_handle") or "").strip() == handle
            for plan in inventory
        ):
            return True
    return False


def resolve_ship_target(
    target: str,
    projects: Mapping[str, Mapping[str, Any] | list[dict[str, Any]]],
) -> dict[str, Any]:
    """Resolve a plan-or-graph ship token without making plan slugs ambiguous.

    ``graph:`` is the explicit graph form. A bare token resolves as a graph only
    when it matches the closed handle grammar and a live endpoint claims it;
    otherwise it remains a plan slug. Duplicate claims still fail through the
    canonical graph resolver.
    """

    token = str(target or "").strip()
    if token.startswith("graph:"):
        return resolve_graph_target(token.removeprefix("graph:").strip(), projects)
    if token.startswith("plan:"):
        token = token.removeprefix("plan:").strip()
        return {"target": f"plan:{token}", "kind": "plan", "slug": token}
    if is_graph_handle(token) and _claims_graph_handle(token, projects):
        return resolve_graph_target(token, projects)
    return {"target": f"plan:{token}", "kind": "plan", "slug": token}


def resolve_graph_target(
    handle: str,
    projects: Mapping[str, Mapping[str, Any] | list[dict[str, Any]]],
) -> dict[str, Any]:
    """Resolve one endpoint handle to its derived cross-project closure.

    ``projects`` is mounted project state keyed by project name. Each value is
    either an inventory list or a mapping carrying ``inventory`` and optional
    ``sprints``, ``active_sprint_id`` and ``project_manifest`` values. Only the
    endpoint handle is authored; every member and metric below is recomputed
    from the current plans and their dependency edges, and each member's
    readiness is the roadmap's own verdict for that plan rather than a second
    computation of the same fact.
    """

    from reckon.roadmap import (
        GraphTargetError,
        _judgment_member,
        _plan_decision_sections,
        _progress,
        _status,
        _whole_plan_dependency,
        build_roadmap,
        closure_blockers,
        unsettled_decisions,
    )

    target_handle = str(handle or "").strip()
    if not target_handle:
        raise GraphTargetError(target_handle, "a non-empty handle is required")
    if not is_graph_handle(target_handle):
        raise GraphTargetError(
            target_handle,
            f"must match {GRAPH_HANDLE_GRAMMAR}",
        )

    project_state: dict[str, dict[str, Any]] = {}
    plans: dict[tuple[str, str], dict[str, Any]] = {}
    endpoints: list[tuple[str, str]] = []
    for project, raw_state in projects.items():
        state = (
            {"inventory": raw_state} if isinstance(raw_state, list) else dict(raw_state)
        )
        inventory = [
            dict(item)
            for item in state.get("inventory") or []
            if isinstance(item, dict)
            and item.get("type", "plan") == "plan"
            and item.get("slug")
            and not item.get("archived")
        ]
        project_state[project] = {**state, "inventory": inventory}
        for plan in inventory:
            slug = str(plan["slug"])
            plan.setdefault("project", project)
            plans[(project, slug)] = plan
            if str(plan.get("graph_handle") or "").strip() == target_handle:
                endpoints.append((project, slug))

    if not endpoints:
        raise GraphTargetError(target_handle, "names no live plan")
    if len(endpoints) > 1:
        names = ", ".join(
            _qualified_plan(project, slug) for project, slug in sorted(endpoints)
        )
        raise GraphTargetError(target_handle, f"is carried by multiple plans: {names}")

    endpoint = endpoints[0]
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    plan_blocking_graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    pending = [endpoint]
    while pending:
        key = pending.pop()
        if key in graph:
            continue
        project, slug = key
        plan = plans[key]
        dependencies: list[tuple[str, str]] = []
        plan_blocking_dependencies: list[tuple[str, str]] = []
        for raw_ref in plan.get("depends_on") or []:
            parsed = parse_plan_ref(raw_ref)
            if parsed is None:
                raise GraphTargetError(
                    target_handle,
                    f"{_qualified_plan(project, slug)} has invalid dependency {raw_ref!r}",
                )
            dependency_project = (
                str(parsed.project) if parsed.is_external(project) else project
            )
            dependency = (dependency_project, parsed.slug)
            if dependency_project not in project_state:
                raise GraphTargetError(
                    target_handle,
                    f"dependency {raw_ref!r} reaches unmounted project {dependency_project!r}",
                )
            if dependency not in plans:
                raise GraphTargetError(
                    target_handle,
                    f"dependency {raw_ref!r} names no live plan",
                )
            dependencies.append(dependency)
            if not parsed.stage:
                plan_blocking_dependencies.append(dependency)
            pending.append(dependency)
        graph[key] = sorted(set(dependencies))
        plan_blocking_graph[key] = sorted(set(plan_blocking_dependencies))

    def longest_path(
        key: tuple[str, str], active: frozenset[tuple[str, str]] = frozenset()
    ) -> list[tuple[str, str]]:
        if key in active:
            cycle = " -> ".join(
                _qualified_plan(*item) for item in [*sorted(active), key]
            )
            raise GraphTargetError(target_handle, f"dependency cycle: {cycle}")
        candidates = [
            longest_path(dependency, active | {key})
            for dependency in plan_blocking_graph.get(key, [])
        ]
        if not candidates:
            return [key]
        depth = max(len(candidate) for candidate in candidates)
        best = min(candidate for candidate in candidates if len(candidate) == depth)
        return [*best, key]

    critical_keys = longest_path(endpoint)
    member_keys = sorted(graph)
    member_set = set(member_keys)
    shipped = sum(_status(plans[key]) in COMPLETED_STATUSES for key in member_keys)

    schedule_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for project in sorted({project for project, _slug in member_keys}):
        state = project_state[project]
        report = build_roadmap(
            project,
            state["inventory"],
            list(state.get("sprints") or []),
            active_sprint_id=state.get("active_sprint_id"),
            project_manifest=state.get("project_manifest") or {},
            docs_dir=state.get("docs_dir"),
        )
        for row in report["pending_work"]:
            schedule_rows[(project, str(row["slug"]))] = row

    decision_blocker_rows: list[dict[str, Any]] = []
    closure_blocker_rows: list[dict[str, Any]] = []
    ready: list[str] = []
    graph_judgment_members: list[dict[str, Any]] = []
    for key in member_keys:
        plan = plans[key]
        if _status(plan) in COMPLETED_STATUSES:
            continue
        graph_project, graph_slug = key
        graph_sections_map = _plan_decision_sections(
            plan, None, graph_project, graph_slug
        )
        decision_blocker_rows.extend(unsettled_decisions(plan, graph_sections_map))
        closure_blocker_rows.extend(closure_blockers(plan))
        dependencies_complete = True
        for dependency in plan_blocking_graph[key]:
            satisfied, judgment = _whole_plan_dependency(
                plans[dependency],
                project_state[dependency[0]].get("docs_dir"),
                dependency[0],
                dependency[1],
            )
            if satisfied:
                continue
            dependencies_complete = False
            if judgment is not None:
                graph_judgment_members.append(
                    _judgment_member(
                        graph_project,
                        graph_slug,
                        dependency[0],
                        dependency[1],
                        judgment,
                    )
                )
        # Readiness is the roadmap row's own verdict for this plan, so both
        # surfaces answer from one computation: authorisation (including a plan
        # released by its cleared hold), its blockers, gates, decisions and
        # dispatchability are decided once. The closure contributes only the
        # dependency debt the graph resolves across projects.
        member_row = schedule_rows.get(key)
        if member_row is not None and member_row.get("ready") and dependencies_complete:
            ready.append(_qualified_plan(*key))

    deferred_members = sorted(
        _qualified_plan(*key)
        for key in member_set
        if schedule_rows.get(key, {}).get("schedule_ready") is False
    )
    critical_refs = [_qualified_plan(*key) for key in critical_keys]
    total = len(member_keys)
    depth = len(critical_keys)
    return {
        "target": f"graph:{target_handle}",
        "handle": target_handle,
        "endpoint": {
            "project": endpoint[0],
            "slug": endpoint[1],
            "ref": _qualified_plan(*endpoint),
        },
        "members": [
            {
                "project": project,
                "slug": slug,
                "ref": _qualified_plan(project, slug),
                "status": _status(plans[(project, slug)]),
                "impl": _progress(plans[(project, slug)]),
            }
            for project, slug in member_keys
        ],
        "repositories": sorted({project for project, _slug in member_keys}),
        "completion": {"shipped": shipped, "total": total},
        "shipped_of_total": f"{shipped}/{total}",
        # The closure's progress, from the one roll-up function over the
        # endpoint's member plans. The counts above stay counts; this is the
        # figure every surface renders in their place.
        "progress": derive_fleet_impl_imputing_effort(
            plans[key] for key in member_keys
        ),
        "critical_path": {"plans": critical_refs, "depth": depth},
        "average_width": round(total / depth, 3) if depth else 0.0,
        "ready": sorted(ready),
        "decision_blockers": decision_blocker_rows,
        "closure_blockers": closure_blocker_rows,
        "judgment_required": {
            "required": bool(graph_judgment_members),
            "count": len(graph_judgment_members),
            "members": sorted(
                graph_judgment_members,
                key=lambda member: (
                    str(member.get("dependent") or ""),
                    str(member.get("target") or ""),
                    str(member.get("reason") or ""),
                ),
            ),
        },
        "ship_ready": not decision_blocker_rows and not closure_blocker_rows,
        "schedule_override": {
            "required": bool(deferred_members),
            "deferred": len(deferred_members),
            "members": deferred_members,
        },
    }


def _dependency_endpoints(
    project: str,
    plans: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enumerate terminal dependency closures from the current project graph."""

    from reckon.roadmap import _decision_rows, _progress, _status

    dependencies: dict[str, list[str]] = {}
    path_dependencies: dict[str, list[str]] = {}
    depended_on: set[str] = set()
    for slug, plan in plans.items():
        members: set[str] = set()
        path_members: set[str] = set()
        for raw_ref in plan.get("depends_on") or []:
            parsed = parse_plan_ref(raw_ref)
            if (
                parsed is None
                or parsed.is_external(project)
                or parsed.slug not in plans
                or parsed.slug == slug
            ):
                continue
            members.add(parsed.slug)
            depended_on.add(parsed.slug)
            if not parsed.stage:
                path_members.add(parsed.slug)
        dependencies[slug] = sorted(members)
        path_dependencies[slug] = sorted(path_members)

    depth_cache: dict[str, int] = {}

    def structural_depth(slug: str, active: frozenset[str] = frozenset()) -> int:
        if slug in depth_cache:
            return depth_cache[slug]
        if slug in active:
            return 0
        child_depths = [
            structural_depth(dependency, active | {slug})
            for dependency in path_dependencies.get(slug, [])
        ]
        depth_cache[slug] = 1 + max(child_depths, default=0)
        return depth_cache[slug]

    rows: list[dict[str, Any]] = []
    endpoint_slugs = [
        slug
        for slug, prerequisite_slugs in dependencies.items()
        if prerequisite_slugs and slug not in depended_on
    ]
    for endpoint_slug in endpoint_slugs:
        member_slugs: set[str] = set()
        pending = [endpoint_slug]
        while pending:
            slug = pending.pop()
            if slug in member_slugs:
                continue
            member_slugs.add(slug)
            pending.extend(dependencies.get(slug, []))

        member_rows = [
            {
                "project": str(plans[slug].get("project") or project),
                "slug": slug,
                "ref": _qualified_plan(
                    str(plans[slug].get("project") or project), slug
                ),
                "title": str(plans[slug].get("title") or slug),
                "status": _status(plans[slug]),
                "impl": _progress(plans[slug]),
                "depends_on": list(plans[slug].get("depends_on") or []),
                "open_decision_count": sum(
                    row["status"] == "open" for row in _decision_rows(plans[slug])
                ),
            }
            for slug in sorted(member_slugs)
        ]
        repository_counts: dict[str, int] = defaultdict(int)
        for member in member_rows:
            repository_counts[member["project"]] += 1

        shipped = sum(
            _status(plans[slug]) in COMPLETED_STATUSES for slug in member_slugs
        )
        held = sum(_status(plans[slug]) == "blocked" for slug in member_slugs)
        open_decision_count = sum(
            row["status"] == "open"
            for slug in member_slugs
            for row in _decision_rows(plans[slug])
        )
        total = len(member_slugs)
        depth = structural_depth(endpoint_slug)
        handle = str(plans[endpoint_slug].get("graph_handle") or "").strip() or None
        endpoint_project = str(plans[endpoint_slug].get("project") or project)
        rows.append(
            {
                "project": endpoint_project,
                "slug": endpoint_slug,
                "ref": _qualified_plan(endpoint_project, endpoint_slug),
                "title": str(plans[endpoint_slug].get("title") or endpoint_slug),
                "handle": handle,
                "members": member_rows,
                "repositories": [
                    {"repository": repository, "count": count}
                    for repository, count in sorted(repository_counts.items())
                ],
                "completion": {"shipped": shipped, "total": total},
                "shipped_fraction": round(shipped / total, 3) if total else 0.0,
                # The closure's progress: the same roll-up the fleet summary
                # serves, over this endpoint's own member plans. The roadmap
                # weights nothing itself — one function owns the figure so every
                # surface reading this endpoint answers with the plan list's
                # number rather than a shipped-over-total count.
                "progress": derive_fleet_impl_imputing_effort(
                    plans[slug] for slug in sorted(member_slugs)
                ),
                "held": held,
                "open_decision_count": open_decision_count,
                "structural_depth": depth,
                "average_width": round(total / depth, 3) if depth else 0.0,
            }
        )

    rows.sort(
        key=lambda row: (
            row["handle"] is None,
            -row["structural_depth"],
            row["slug"],
        )
    )
    return rows
