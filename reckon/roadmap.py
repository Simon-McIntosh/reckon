"""Dependency-aware pending-work and roadmap analysis.

The analyzer is deliberately storage-neutral: callers pass the composed
inventory and sprint resources returned by discovery.  This keeps the same
semantics available to MCP, the CLI, audits, and tests without reimplementing
graph traversal in each surface.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from reckon._schema import (
    GRAPH_HANDLE_GRAMMAR,
    LEGACY_EFFORT_HOURS,
    is_graph_handle,
    parse_plan_ref,
    plan_section_anchors,
)
from reckon.doccheck import _load_mounts, authorisation_staleness, derived_plan_age
from reckon.lifecycle import (
    COMPLETED_STATUSES,
    TERMINAL_STATUSES,
    effective_status,
    unpassed_gate_blockers,
)
from reckon.mcp_views import compose_review, in_flight_by_plan, load_composed_review

_EFFORT_UNIT = "worker-hours"
_ROI_ORDER = {"high": 0, "mid": 1, "med": 1, "low": 2}
# A plan is authorised to run as soon as it exists and describes live work.
# ``draft`` belongs here: drafting is how a plan gets written, not a permission
# tier, and withholding readiness until someone re-labels it produces a queue of
# implementable work that reads as blocked. Only terminal or suspended states
# fall outside.
_AUTHORISED_STATUSES = frozenset({"draft", "pending", "active", "in-progress"})


class GraphTargetError(ValueError):
    """A graph ship target cannot resolve to one complete dependency closure."""

    def __init__(self, handle: str, detail: str) -> None:
        self.handle = handle
        super().__init__(f"graph handle {handle!r}: {detail}")


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


def _wall_clock_hours(plan: dict[str, Any]) -> float:
    """Elapsed hours for one plan at the parallelism it supports.

    Worker-hours measure labour; wall-clock measures how long the plan blocks
    the schedule. They differ whenever a plan can fan out, and only the second
    belongs in a path length. An unauthored value falls back to worker-hours,
    which is the honest serial assumption rather than an invented speed-up.
    """
    declared = plan.get("wall_clock_hours")
    if declared is not None:
        try:
            value = float(declared)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            # Parallelism cannot beat the serial total; clamp rather than trust.
            return min(max(0.0, value), _effort_hours(plan))
    return _effort_hours(plan)


def _remaining_effort_hours(plan: dict[str, Any]) -> float:
    return round(_effort_hours(plan) * (1.0 - _progress(plan)), 3)


def _remaining_wall_hours(plan: dict[str, Any]) -> float:
    return round(_wall_clock_hours(plan) * (1.0 - _progress(plan)), 3)


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


def _section_satisfied(plan: dict[str, Any], section: str) -> bool:
    """Return whether a target plan has completed one named section."""

    if _status(plan) in COMPLETED_STATUSES:
        return True
    section_gates = [
        gate
        for gate in plan.get("gates") or []
        if isinstance(gate, dict) and str(gate.get("section") or "") == section
    ]
    return bool(section_gates) and all(
        str(gate.get("verdict") or "").strip().lower() == "passed"
        for gate in section_gates
    )


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


def _decision_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return decisions with an explicit readiness state for roadmap consumers."""

    raw_decisions = plan.get("decisions") or []
    if isinstance(raw_decisions, dict):
        decisions = [
            {"key": key, **(value if isinstance(value, dict) else {})}
            for key, value in raw_decisions.items()
        ]
    else:
        decisions = [item for item in raw_decisions if isinstance(item, dict)]

    rows: list[dict[str, Any]] = []
    plan_slug = str(plan.get("slug") or "")
    for decision in decisions:
        key = str(decision.get("key") or "").strip()
        choice = str(decision.get("choice") or decision.get("chosen") or "").strip()
        rationale = str(decision.get("rationale") or "").strip()
        status = "locked" if choice else "deferred" if rationale else "open"
        rows.append(
            {
                "kind": "decision",
                "plan": plan_slug,
                "id": key,
                "question": str(decision.get("title") or key),
                "status": status,
                "choice": choice,
                "rationale": rationale,
            }
        )
    return rows


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


def _review_health(
    project: str,
    review: dict[str, Any] | None,
    local_graph: Mapping[str, list[str]],
) -> list[dict[str, Any]]:
    """Turn composed review health discriminators into roadmap advisories."""

    if not review:
        return []
    priority = review.get("priority") or []
    rank_by_slug: dict[str, int] = {}
    for row in priority:
        parsed = parse_plan_ref(row.get("ref"))
        if parsed is None or parsed.is_external(project):
            continue
        rank_by_slug[parsed.slug] = int(row.get("rank", 10**6))

    health: list[dict[str, Any]] = []
    for successor, dependencies in local_graph.items():
        successor_rank = rank_by_slug.get(successor)
        if successor_rank is None:
            continue
        for dependency in dependencies:
            dependency_rank = rank_by_slug.get(dependency)
            if dependency_rank is None or successor_rank >= dependency_rank:
                continue
            inversion = {
                "plan": successor,
                "rank": successor_rank,
                "dependency": dependency,
                "dependency_rank": dependency_rank,
            }
            health.append(
                _finding(
                    "priority-order-inversion",
                    "warn",
                    (
                        f"{successor}: review rank {successor_rank} places it ahead "
                        f"of unshipped prerequisite {dependency} at rank {dependency_rank}"
                    ),
                    slug=successor,
                    extra=inversion,
                )
            )

    reviewed_at = str(review.get("reviewed_at") or "")
    moved_subjects = {
        f"{subject.get('kind', 'subject')}:{subject.get('id', '')}"
        for row in review.get("findings") or []
        if isinstance(row, dict) and row.get("stale") is True
        for subject in [row.get("subject") or {}]
    }
    moved_subjects.update(
        f"plan:{parsed.slug}"
        for row in priority
        if isinstance(row, dict) and row.get("stale") is True
        for parsed in [parse_plan_ref(row.get("ref"))]
        if parsed is not None and not parsed.is_external(project)
    )
    if moved_subjects:
        subjects = sorted(moved_subjects)
        health.append(
            _finding(
                "review-stale",
                "warn",
                (
                    f"reviewed {reviewed_at or 'at an unknown time'}; subjects moved "
                    f"after review: {', '.join(subjects)}"
                ),
                extra={"reviewed_at": reviewed_at, "subjects": subjects},
            )
        )

    return health


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


def _open_sprints(
    sprints: list[dict[str, Any]], plans: dict[str, dict[str, Any]]
) -> list[str]:
    """Return ordered sprints that still contain executable work."""

    result: list[str] = []
    for sprint in sprints:
        sprint_id = str(sprint.get("id") or "")
        status = str(sprint.get("status") or "").lower()
        if not sprint_id or status in TERMINAL_STATUSES:
            continue
        item_plans = [
            plans[slug]
            for item in sprint.get("items") or []
            if (slug := _item_slug(item)) in plans
        ]
        if any(_status(plan) not in TERMINAL_STATUSES for plan in item_plans):
            result.append(sprint_id)
    return result


def _schedule_horizon(project_manifest: dict[str, Any] | None) -> int | None:
    """Read the declared number of open sprints allowed in the schedule window."""

    value = (project_manifest or {}).get("schedule_horizon_sprints")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


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
    from the current plans and their dependency edges.
    """

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
            {"inventory": raw_state}
            if isinstance(raw_state, list)
            else dict(raw_state)
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
                str(parsed.project)
                if parsed.is_external(project)
                else project
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
        )
        for row in report["pending_work"]:
            schedule_rows[(project, str(row["slug"]))] = row

    decision_blockers: list[dict[str, Any]] = []
    ready: list[str] = []
    for key in member_keys:
        plan = plans[key]
        if _status(plan) in COMPLETED_STATUSES:
            continue
        decisions = _decision_rows(plan)
        open_decisions = [row for row in decisions if row["status"] == "open"]
        decision_blockers.extend(open_decisions)
        dependencies_complete = all(
            _status(plans[dependency]) in COMPLETED_STATUSES
            for dependency in plan_blocking_graph[key]
        )
        explicit_blockers = [
            row
            for row in plan.get("blocking") or []
            if isinstance(row, dict) and row.get("kind") == "explicit"
        ]
        if (
            dependencies_complete
            and not explicit_blockers
            and not unpassed_gate_blockers(plan.get("gates") or [])
            and not open_decisions
            and _status(plan) in _AUTHORISED_STATUSES
            and _dispatchability(plan)[0]
        ):
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
        "critical_path": {"plans": critical_refs, "depth": depth},
        "average_width": round(total / depth, 3) if depth else 0.0,
        "ready": sorted(ready),
        "decision_blockers": decision_blockers,
        "ship_ready": not decision_blockers,
        "schedule_override": {
            "required": bool(deferred_members),
            "deferred": len(deferred_members),
            "members": deferred_members,
        },
    }


def build_roadmap(
    project: str,
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    *,
    active_sprint_id: str | None = None,
    sprint_id: str | None = None,
    max_paths: int = 5,
    project_manifest: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
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
    open_sprints = _open_sprints(sprints, all_plans)
    schedule_horizon = _schedule_horizon(project_manifest)
    schedule_ready_sprints = (
        open_sprints[:schedule_horizon]
        if schedule_horizon is not None
        else open_sprints
    )
    schedule_boundary = schedule_ready_sprints[-1] if schedule_ready_sprints else None
    selected_slugs = _scope_slugs(all_plans, membership, sprint_id)
    plans = {slug: all_plans[slug] for slug in selected_slugs}
    live_runs = in_flight_by_plan(project)
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
                if parsed.stage:
                    row["stage"] = parsed.stage
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
            target_sections = plan_section_anchors(target)
            section_found = not parsed.stage or parsed.stage in target_sections
            if parsed.stage:
                satisfied = section_found and _section_satisfied(target, parsed.stage)
            else:
                satisfied = target_status in COMPLETED_STATUSES
            dependency_row = {
                "ref": ref,
                "scope": "local",
                "slug": parsed.slug,
                "found": True,
                "status": target_status,
                "satisfied": satisfied,
            }
            if parsed.stage:
                dependency_row.update(
                    {"stage": parsed.stage, "section_found": section_found}
                )
            dependency_rows[slug].append(dependency_row)
            if parsed.stage and not section_found:
                findings.append(
                    _finding(
                        "missing-dependency-section",
                        "error",
                        (
                            f"{slug}: dependency {ref!r} names no section "
                            f"{parsed.stage!r} on {parsed.slug}"
                        ),
                        slug=slug,
                        extra={
                            "ref": ref,
                            "section": parsed.stage,
                            "target": parsed.slug,
                        },
                    )
                )
            if not satisfied and not parsed.stage and parsed.slug in plans:
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
    schedule_deferred: list[dict[str, Any]] = []
    decision_blockers_report: list[dict[str, Any]] = []
    deferred_decisions_report: list[dict[str, Any]] = []
    unauthorised: list[dict[str, Any]] = []
    for slug, plan in plans.items():
        status = _status(plan)
        if status in TERMINAL_STATUSES:
            continue
        dependency_blockers = [
            row for row in dependency_rows.get(slug, []) if not row.get("satisfied")
        ]
        plan_dependency_blockers = [
            row for row in dependency_blockers if not row.get("stage")
        ]
        section_dependency_blockers = [
            row for row in dependency_blockers if row.get("stage")
        ]
        explicit_blockers = [
            row
            for row in plan.get("blocking") or []
            if isinstance(row, dict) and row.get("kind") == "explicit"
        ]
        gate_blockers = unpassed_gate_blockers(plan.get("gates") or [])
        decisions = _decision_rows(plan)
        decision_blockers = [
            decision for decision in decisions if decision["status"] == "open"
        ]
        deferred_decisions = [
            decision for decision in decisions if decision["status"] == "deferred"
        ]
        if (
            status == "blocked"
            and not explicit_blockers
            and not plan_dependency_blockers
            and not gate_blockers
            and not decision_blockers
        ):
            explicit_blockers = [{"kind": "persisted", "id": "unrecorded"}]
        dispatchable, missing_dispatchability = _dispatchability(plan)
        authorised = status in _AUTHORISED_STATUSES
        is_ready = (
            dispatchable
            and authorised
            and not plan_dependency_blockers
            and not explicit_blockers
            and not gate_blockers
            and not decision_blockers
            and slug not in cycle_members
        )
        is_blocked = bool(
            plan_dependency_blockers
            or explicit_blockers
            or gate_blockers
            or decision_blockers
            or slug in cycle_members
        )
        readiness = "ready" if is_ready else "blocked" if is_blocked else "deferred"
        plan_sprint = plan.get("sprint") or (membership.get(slug) or [None])[0]
        is_schedule_deferred = bool(
            schedule_horizon is not None
            and plan_sprint in open_sprints
            and plan_sprint not in schedule_ready_sprints
        )
        schedule_reason = None
        if is_schedule_deferred:
            schedule_reason = (
                f"held behind {schedule_boundary}; the schedule window starts at "
                f"{open_sprints[0]} and spans {schedule_horizon} sprints holding open work"
            )
        row = {
            "slug": slug,
            "title": plan.get("title") or slug,
            "status": status,
            "dispatchable": dispatchable,
            "missing_dispatchability": missing_dispatchability,
            "authorised": authorised,
            "effective_status": effective_status(
                status,
                [
                    *plan_dependency_blockers,
                    *explicit_blockers,
                    *gate_blockers,
                    *decision_blockers,
                ],
            ),
            "sprint": plan_sprint,
            "roi": plan.get("roi") or "mid",
            "effort": plan.get("effort") or "M",
            "effort_hours": _effort_hours(plan),
            "wall_clock_hours": _wall_clock_hours(plan),
            "effort_calibrated": _effort_calibrated(plan),
            "progress_pct": round(_progress(plan) * 100, 1),
            "remaining_effort_hours": _remaining_effort_hours(plan),
            "remaining_wall_hours": _remaining_wall_hours(plan),
            "depends_on": dependency_rows.get(slug, []),
            "explicit_blockers": explicit_blockers,
            "gate_blockers": gate_blockers,
            "decision_blockers": decision_blockers,
            "deferred_decisions": deferred_decisions,
            "decisions": decisions,
            "unlocks": sorted(dependents.get(slug, set())),
            "ready": is_ready,
            "readiness": readiness,
            "dependency_ready": is_ready,
            "dependency_readiness": readiness,
            "schedule_ready": not is_schedule_deferred,
            "schedule_readiness": "deferred" if is_schedule_deferred else "ready",
            "schedule_deferred_reason": schedule_reason,
            "schedule_behind_sprint": schedule_boundary
            if is_schedule_deferred
            else None,
        }
        if section_dependency_blockers:
            section_blockers = {
                str(blocker["stage"]): [
                    row
                    for row in section_dependency_blockers
                    if row.get("stage") == blocker["stage"]
                ]
                for blocker in section_dependency_blockers
            }
            sections = sorted(plan_section_anchors(plan) | section_blockers.keys())
            row["section_readiness"] = [
                {
                    "section": section,
                    "ready": section not in section_blockers,
                    "blockers": section_blockers.get(section, []),
                }
                for section in sections
            ]
            row["ready_sections"] = [
                section for section in sections if section not in section_blockers
            ]
            row["blocked_sections"] = [
                section for section in sections if section in section_blockers
            ]
        if slug in live_runs:
            row["in_flight"] = live_runs[slug]
        pending.append(row)
        if not authorised and status not in TERMINAL_STATUSES:
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
        if is_schedule_deferred:
            schedule_deferred.append(row)
        decision_blockers_report.extend(decision_blockers)
        deferred_decisions_report.extend(deferred_decisions)

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
            key=lambda path: sum(_remaining_wall_hours(plans[item]) for item in path),
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
                sum(_remaining_wall_hours(plans[item]) for item in path), 3
            )
    sorted_paths = sorted(path_candidates.items(), key=lambda item: (-item[1], item[0]))
    open_paths = []
    for path, length_hours in sorted_paths[: max(1, max_paths)]:
        path_plans = list(path)
        uncalibrated = _uncalibrated_plans(plans, path_plans)
        open_paths.append(
            {
                "plans": path_plans,
                # Elapsed time to walk the path, each plan fanning out as far
                # as it supports; worker-hours is the labour the same path
                # costs and is reported beside it, never in place of it.
                "length_hours": length_hours,
                "length_unit": "elapsed-hours",
                "worker_hours": round(
                    sum(_remaining_effort_hours(plans[item]) for item in path), 3
                ),
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
            "length_unit": "elapsed-hours",
            "worker_hours": 0.0,
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
    schedule_deferred.sort(key=priority)
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
            "dependency_ready": row["dependency_ready"],
            "dependency_readiness": row["dependency_readiness"],
            "schedule_ready": row["schedule_ready"],
            "schedule_readiness": row["schedule_readiness"],
            "schedule_deferred_reason": row["schedule_deferred_reason"],
            "schedule_behind_sprint": row["schedule_behind_sprint"],
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
                "schedule_ready": sum(
                    row.get("sprint") == sprint_name and row["schedule_ready"]
                    for row in pending
                ),
                "schedule_deferred": sum(
                    row.get("sprint") == sprint_name for row in schedule_deferred
                ),
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
    if review is None:
        docs_dir = _load_mounts().get(project)
        review_block = None
        if docs_dir is not None:
            review_block, _review_version = load_composed_review(
                docs_dir, project, list(all_plans.values()), sprints, project_manifest
            )
    elif review:
        review_block = compose_review(
            review, list(all_plans.values()), sprints, project, project_manifest
        )
    else:
        review_block = None
    review_findings = _review_health(project, review_block, local_graph)
    if review_block is not None:
        review_block = dict(review_block)
        review_block["findings"] = [
            dict(row)
            for row in review_block.get("findings") or []
            if isinstance(row, dict) and not row.get("resolved_at")
        ]
    findings.extend(review_findings)
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
            "remaining_wall_hours": round(
                sum(_remaining_wall_hours(plan) for plan in plan_values), 3
            ),
            "wall_clock_unit": "elapsed-hours",
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
        "review": review_block,
        "north_stars": _north_star_rows(plans, north_stars),
        "sprints": sprint_rows,
        "pending_work": pending,
        "ready_now": ready,
        "blocked": blocked,
        "deferred": deferred,
        "schedule": {
            "configured": schedule_horizon is not None,
            "configuration_key": "schedule_horizon_sprints",
            "window_sprints": schedule_horizon,
            "horizon_depth": len(open_sprints),
            "open_sprints": open_sprints,
            "earliest_open_sprint": open_sprints[0] if open_sprints else None,
            "ready_sprints": schedule_ready_sprints,
            "ready": sum(row["schedule_ready"] for row in pending),
            "deferred": len(schedule_deferred),
        },
        "schedule_deferred": schedule_deferred,
        "decision_blockers": decision_blockers_report,
        "deferred_decisions": deferred_decisions_report,
        "decision_readiness": {
            "ready": not decision_blockers_report,
            "open": len(decision_blockers_report),
            "deferred": len(deferred_decisions_report),
        },
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
