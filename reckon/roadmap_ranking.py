"""The ranking one roadmap read applies to its ready work.

The roadmap's payload window is finite, so the arithmetic that decides which
ready row leads lives here rather than beside the payload it orders. Two
populations share one key: a plan row, and the ranked rows of the ready
sections inside it. A key term a row cannot carry is neutral for that row, so
the plan rows keep the ordering they had and the section rows gain the
section-scoped signals they are for.

Nothing here reads a project; every function takes the values the caller
already resolved, which is what keeps this module a function of its arguments
rather than a second analyser.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from itertools import pairwise
from typing import Any

# ROI is an author's coarse ordering, so it separates rows that agree on
# everything the ranking considers more material.
_ROI_ORDER = {"high": 0, "mid": 1, "med": 1, "low": 2}

_EFFORT_UNIT = "worker-hours"

# The signal names a ranked ready row reports in its ``reason``. One vocabulary
# for plan rows and section rows alike: a row says which signal placed it.
CRITICAL_PATH_SIGNAL = "critical path"
SPRINT_SIGNAL = "sprint"
ROI_SIGNAL = "roi"
REACH_SIGNAL = "reach"
STUCK_SIGNAL = "stuck"
EFFORT_SIGNAL = "effort"
HOLD_SIGNAL = "after hold"

# The signal a row reports when no term of the shared key separates it from the
# row it is ranked above: it is ready, and nothing more specific placed it.
READY_SIGNAL = "ready"

# The signal each term of the shared key reports, in key order. A smaller value
# leads, so the signal that placed a row is the first term on which its key is
# smaller than the key of the row below it.
_KEY_SIGNALS = (
    HOLD_SIGNAL,
    CRITICAL_PATH_SIGNAL,
    SPRINT_SIGNAL,
    ROI_SIGNAL,
    REACH_SIGNAL,
    STUCK_SIGNAL,
    EFFORT_SIGNAL,
    READY_SIGNAL,
)

# A section is stuck when it has been attempted twice without the attempts
# settling, or when a run on it has been blocked for over a day. Both readings
# say the same thing to a reader: this row is not merely ready, it is the row
# that keeps coming back, so it leads the rows it is otherwise equal to.
STUCK_ATTEMPT_THRESHOLD = 2
STUCK_BLOCKED_RUN_SECONDS = 86400.0

# A row that names no sprint is ranked after every row that names one.
_NO_SPRINT_PLACE = 10**6


def _path_plan_slugs(path: Iterable[str]) -> list[str]:
    """The plan slugs one path passes through, in path order and once each.

    A path node is a plan slug or a section node; several of a plan's sections
    on one path name that plan once, because the field answers "which plans does
    this path cross", not "how many of its sections does it touch".
    """

    slugs: list[str] = []
    seen: set[str] = set()
    for node in path:
        slug = node.partition("#")[0]
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def _section_node(plan_slug: str, section: str) -> str:
    """The graph identity of one section: its plan, then its own id.

    The ``<plan>#<section>`` spelling is the ref grammar the declaration already
    uses, so a node in the path prints as the anchor a reader may click. No plan
    slug carries a ``#``, so the two identities never collide.
    """

    return f"{plan_slug}#{section}"


def _section_effort_hours(authored: Mapping[str, Any], section: str) -> float:
    """The authored effort of one section, or zero when it records none.

    A section's hours live on its own authored record rather than on the plan,
    because the same plan's sections differ in size and the path is priced by
    the section the work actually sits in.
    """

    for record in authored.get("sections") or []:
        if not isinstance(record, Mapping):
            continue
        if str(record.get("id") or "") != section:
            continue
        try:
            return max(0.0, float(record.get("effort_hours") or 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _lift_section_edges(
    rows: Iterable[Mapping[str, Any]],
    waiting_slug: str,
    *,
    plans: Mapping[str, dict[str, Any]],
    local_graph: dict[str, list[str]],
    dependents: dict[str, set[str]],
    section_effort: dict[str, float],
    edge_meta: dict[tuple[str, str], dict[str, str]],
    authored: Callable[[str], Mapping[str, Any]],
    waiting_authored: Mapping[str, Any],
) -> None:
    """Give the resolved section waits node identity in the roadmap's graph.

    The section-scoped declaration resolves these edges; this only names their
    ends. A resolved, unsatisfied wait becomes an edge between two section
    nodes, so the critical path and the unlock set run through the sections that
    carry the work rather than stopping at the plans around them.

    The wait is recorded at plan granularity in ``dependents`` alone. The plan
    keeps no graph edge of its own, so it stays in the ready set and out of the
    cycle walk — a plan-level veto is the very behaviour the section-scoped
    declaration exists to replace.
    """

    for row in rows:
        target_section = str(row.get("stage") or "")
        waiting_section = str(row.get("source_section") or "")
        target_slug = str(row.get("slug") or "")
        if (
            row.get("scope") != "local"
            or not row.get("found")
            or row.get("satisfied")
            or not target_section
            or not waiting_section
            or target_slug not in plans
            or waiting_slug not in plans
        ):
            continue
        target_node = _section_node(target_slug, target_section)
        waiting_node = _section_node(waiting_slug, waiting_section)
        section_effort.setdefault(
            target_node, _section_effort_hours(authored(target_slug), target_section)
        )
        section_effort.setdefault(
            waiting_node, _section_effort_hours(waiting_authored, waiting_section)
        )
        local_graph[waiting_node].append(target_node)
        dependents[target_node].add(waiting_node)
        if target_slug != waiting_slug:
            # One plan's own section may wait on another of its sections. That
            # edge sequences work inside the plan, and it must not report the
            # plan as an unlock of itself, where the field asks which other
            # plan's work this one releases.
            dependents[target_slug].add(waiting_slug)
        edge_meta[(waiting_node, target_node)] = {
            "ref": str(row.get("ref") or target_node),
            "section": target_section,
            "gate": "",
        }


def section_is_stuck(attempts: int, blocked_run_seconds: float | None) -> bool:
    """Whether a section is one a reader has already been through.

    ``attempts`` counts the executable runs the section has been given, so two
    of them say the section did not settle on its first try.
    ``blocked_run_seconds`` is the age of a run on the section that reports
    itself blocked, and a day of that says the same thing from the other
    direction: not unsolved in passing, but stuck. Either reading is enough; a
    section with neither is not stuck.
    """

    if attempts >= STUCK_ATTEMPT_THRESHOLD:
        return True
    return (
        blocked_run_seconds is not None
        and blocked_run_seconds > STUCK_BLOCKED_RUN_SECONDS
    )


def _priority_key(
    *,
    after_hold: bool,
    on_critical_path: bool,
    sprint_position: int,
    roi: str,
    reach: int,
    stuck: bool,
    remaining_effort_hours: float,
    identity: str,
) -> tuple[Any, ...]:
    """The one ordering a ready row is ranked by, plan row or section row.

    Critical-path membership leads, then sprint position, then ROI, then reach
    — the count of work the row releases — then the stuck signal, which leads
    among rows otherwise equal: a row a reader has already been through twice
    is the one to look at first. The row's own effort and identity are the
    last, stable tie-breakers.
    """

    return (
        # Soft-sequenced work yields the front of a tie: a row that has
        # deliberately deferred to something unshipped should not be the first
        # thing a reader starts, though it stays ready and ranked ahead of
        # nothing — the edge never holds it.
        1 if after_hold else 0,
        0 if on_critical_path else 1,
        sprint_position,
        _ROI_ORDER.get(str(roi or "mid").lower(), 1),
        -reach,
        0 if stuck else 1,
        remaining_effort_hours,
        identity,
    )


def plan_ranking_key(
    row: Mapping[str, Any],
    *,
    critical_members: set[str] | frozenset[str],
    sprint_order: Mapping[str, int],
) -> tuple[Any, ...]:
    """The key one plan row is ranked by."""

    return _priority_key(
        after_hold=bool(row.get("after_hold")),
        on_critical_path=row["slug"] in critical_members,
        sprint_position=sprint_order.get(
            str(row.get("sprint") or ""), _NO_SPRINT_PLACE
        ),
        roi=str(row.get("roi") or "mid"),
        reach=len(row.get("unlocks") or []),
        stuck=bool(row.get("stuck")),
        remaining_effort_hours=row["remaining_effort_hours"],
        identity=str(row["slug"]),
    )


def section_ranking_key(
    row: Mapping[str, Any],
    *,
    critical_sections: set[str] | frozenset[str],
    sprint_order: Mapping[str, int],
) -> tuple[Any, ...]:
    """The key one ready section row is ranked by."""

    return _priority_key(
        after_hold=bool(row.get("after_hold")),
        on_critical_path=str(row.get("ref") or "") in critical_sections,
        sprint_position=sprint_order.get(
            str(row.get("sprint") or ""), _NO_SPRINT_PLACE
        ),
        roi=str(row.get("roi") or "mid"),
        reach=len(row.get("unlocks") or []),
        stuck=bool(row.get("stuck")),
        remaining_effort_hours=row["remaining_effort_hours"],
        identity=str(row.get("ref") or row.get("plan") or ""),
    )


def leading_signal(winner: tuple[Any, ...], loser: tuple[Any, ...]) -> str:
    """The term of the shared key on which one ranked row leads another.

    A row's placing signal is the term that puts it above the row below it, so
    the answer is the first term on which its key is smaller. Naming the key's
    own terms is what stops a row reporting reach when ROI, sprint position or
    another term is what actually placed it; two rows equal on every term are
    separated by identity alone, which places nothing and reads as ready.
    """

    for index, signal in enumerate(_KEY_SIGNALS):
        if winner[index] < loser[index]:
            return signal
    return READY_SIGNAL


def section_row_floor(key: tuple[Any, ...]) -> tuple[Any, ...]:
    """The least favourable section row key one section row's own key admits.

    The last section row of a read has nothing below it to be placed above, so
    it is measured against this floor. The terms that separate two section rows
    from one another — reach, sprint position and ROI — carry the row's own value
    in the floor, so only the terms a section carries outright can move it: off
    the critical path, not stuck, and costing nothing.
    """

    return (key[0], 1, key[2], key[3], key[4], 1, 0.0, key[7])


def rank_open_paths(
    plans: Mapping[str, dict[str, Any]],
    *,
    section_effort: Mapping[str, float],
    local_graph: Mapping[str, list[str]],
    edge_meta: Mapping[tuple[str, str], Mapping[str, str]],
    cycle_members: set[str] | frozenset[str],
    closable_slugs: set[str],
    max_paths: int,
    remaining_wall_hours: Callable[[Mapping[str, Any]], float],
    remaining_effort_hours: Callable[[Mapping[str, Any]], float],
    uncalibrated_plans: Callable[..., list[str]],
    terminal_statuses: frozenset[str],
    status_of: Callable[[Mapping[str, Any]], str],
) -> tuple[list[dict[str, Any]], dict[str, Any], set[str]]:
    """Rank the open execution paths and return the critical one beside them.

    The path a reader starts on is the longest walk the graph supports, priced
    by the hours of the nodes it runs through. A sectioned plan's work is its
    sections, so a plan competing as one node beside its own sections would
    price that work twice and hide the sections the path runs through.
    """

    def node_hours(node: str) -> float:
        """Elapsed hours one path node costs: a plan's, or a section's own."""

        if node in plans:
            return remaining_wall_hours(plans[node])
        return section_effort.get(node, 0.0)

    def node_effort(node: str) -> float:
        if node in plans:
            return remaining_effort_hours(plans[node])
        return section_effort.get(node, 0.0)

    def longest_path(node: str, visiting: frozenset[str]) -> list[str]:
        if node in visiting or node in cycle_members:
            return []
        candidates = [
            longest_path(dependency, visiting | {node})
            for dependency in local_graph.get(node, [])
            if (dependency in plans or dependency in section_effort)
            and dependency not in closable_slugs
        ]
        best = max(
            candidates,
            key=lambda path: sum(node_hours(item) for item in path),
            default=[],
        )
        return [*best, node]

    def node_is_live(node: str) -> bool:
        """Whether a node names work a path may still run through."""

        plan_slug = node.partition("#")[0]
        plan = plans.get(plan_slug)
        return plan is not None and status_of(plan) not in terminal_statuses

    # A sectioned plan's work is its sections. Letting the plan compete as one
    # node beside them would price that work twice and hide the sections the
    # path actually runs through.
    sectioned_plans = {node.partition("#")[0] for node in section_effort}

    path_candidates: dict[tuple[str, ...], float] = {}
    for slug, plan in plans.items():
        if status_of(plan) in terminal_statuses:
            continue
        if slug in closable_slugs or slug in sectioned_plans:
            continue
        path = longest_path(slug, frozenset())
        if path:
            path_candidates[tuple(path)] = round(
                sum(node_hours(item) for item in path), 3
            )
    for node in sorted(section_effort):
        if not node_is_live(node) or node.partition("#")[0] in closable_slugs:
            continue
        path = longest_path(node, frozenset())
        if path:
            path_candidates[tuple(path)] = round(
                sum(node_hours(item) for item in path), 3
            )
    sorted_paths = sorted(path_candidates.items(), key=lambda item: (-item[1], item[0]))
    open_paths: list[dict[str, Any]] = []
    for path, length_hours in sorted_paths[: max(1, max_paths)]:
        path_plans = _path_plan_slugs(path)
        path_sections = [node for node in path if "#" in node]
        uncalibrated = uncalibrated_plans(plans, path_plans)
        entry: dict[str, Any] = {
            "plans": path_plans,
            # The sections the path runs through, beside the plans it names.
            # The key is always present, empty when the path crosses no
            # section, so a reader relies on its shape rather than probing.
            "sections": path_sections,
            # Elapsed time to walk the path, each plan fanning out as far as
            # it supports; worker-hours is the labour the same path costs and
            # is reported beside it, never in place of it.
            "length_hours": length_hours,
            "length_unit": "elapsed-hours",
            "worker_hours": round(sum(node_effort(item) for item in path), 3),
            "effort_unit": _EFFORT_UNIT,
            "uncalibrated_plans": uncalibrated,
            "uncalibrated_count": len(uncalibrated),
            # Each step of the path beside its edge: an edge that waits on one
            # section of its target names that section, so a plan held by a
            # single section shows the section rather than the plan.
            "edges": [
                {
                    "from": dependent.partition("#")[0],
                    "to": dependency.partition("#")[0],
                    "ref": meta.get("ref", dependency.partition("#")[0]),
                    "section": meta.get("section", "") or dependency.partition("#")[2],
                    "gate": meta.get("gate", ""),
                }
                for dependency, dependent in pairwise(path)
                for meta in [
                    edge_meta.get((dependent, dependency))
                    or edge_meta.get(
                        (
                            dependent.partition("#")[0],
                            dependency.partition("#")[0],
                        ),
                        {},
                    )
                ]
            ],
        }
        open_paths.append(entry)
    # The critical path carries the shape every reader has always seen plus the
    # sections the path runs through, so a summary that never leaves the plan
    # level reads exactly as it did.
    critical = (
        {key: value for key, value in open_paths[0].items() if key != "edges"}
        if open_paths
        else {
            "plans": [],
            "sections": [],
            "length_hours": 0.0,
            "length_unit": "elapsed-hours",
            "worker_hours": 0.0,
            "effort_unit": _EFFORT_UNIT,
            "uncalibrated_plans": [],
            "uncalibrated_count": 0,
        }
    )
    return open_paths, critical, set(critical["plans"])
