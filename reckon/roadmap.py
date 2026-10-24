"""Dependency-aware pending-work and roadmap analysis.

The analyzer is deliberately storage-neutral: callers pass the composed
inventory and sprint resources returned by discovery.  This keeps the same
semantics available to MCP, the CLI, audits, and tests without reimplementing
graph traversal in each surface.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon._plan_html import (
    OPEN_SECTION_DECLARATIONS,
    SECTION_DECLARATION_DEFERRED,
    SECTION_DECLARATION_DONE,
    SECTION_NUMBER_PATTERN,
    _read_plan_text,
    authored_section_headings,
    open_sections,
    read_state_and_text_file,
    section_record_id,
)
from reckon._schema import (
    GATE_TRANSITIONS,
    LEGACY_EFFORT_HOURS,
    decision_section_refusals,
    decision_sections,
    declared_section_identities,
    is_implementable_section,
    is_section_identity,
    parse_plan_ref,
    pending_transition_gates,
    plan_executable_remainder,
    resolve_plan_ref,
    section_dependency_refusals,
    section_depends_on,
    standalone_reason,
)
from reckon._store import sprint_item_slug
from reckon._timestamps import parse_utc
from reckon.doccheck import (
    _load_mounts,
    authorisation_staleness,
    derived_plan_age,
    gate_declared_refs,
    gate_evidence_slugs_from_gates,
    unwired_plan_finding,
)
from reckon.file_memo import memoized
from reckon.followup_pointers import classify_followup, find_invocation

# The graph-target family lives in its own module. ``_dependency_endpoints``
# and ``_qualified_plan`` are read by code here; the resolvers are re-exported
# so a caller that already names ``reckon.roadmap`` keeps resolving them.
from reckon.graph_target import _dependency_endpoints, _qualified_plan
from reckon.graph_target import (
    resolve_graph_target as resolve_graph_target,  # noqa: PLC0414
)
from reckon.graph_target import (
    resolve_ship_target as resolve_ship_target,  # noqa: PLC0414
)
from reckon.lifecycle import (
    COMPLETED_STATUSES,
    TERMINAL_STATUSES,
    effective_status,
    unpassed_gate_blockers,
)
from reckon.live_run_classification import (
    BLOCKED_RUN_CLASSIFICATION,
    DECISION_BLOCKER,
    GATE_BLOCKER,
    SECTION_DEPENDENCY_BLOCKER,
    held_path_blockers,
    held_path_rows,
    recorded_live_run_classifications,
    run_blocker_rows,
    run_classification,
    run_classification_record,
    section_run_rows,
)
from reckon.mcp_views import (
    compose_review,
    load_composed_review,
    partition_live_runs,
    section_attempts_by_plan,
)
from reckon.project_state import (
    ProjectStateError,
    focus_sprint_id,
    live_sprint_ids,
    read_resource,
)
from reckon.resources import (
    read_plan_record,
    read_sprint_record,
    resolve_resource,
    resource_scan_scope,
)
from reckon.roadmap_ranking import (
    _EFFORT_UNIT,
    _lift_section_edges,
    _section_effort_hours,
    leading_signal,
    plan_ranking_key,
    rank_open_paths,
    section_is_stuck,
    section_ranking_key,
    section_row_floor,
)
from reckon.schedule import derive_schedule
from reckon.sprint_liveness import sprint_liveness

# A plan is authorised to run as soon as it exists and describes live work.
# ``draft`` belongs here: drafting is how a plan gets written, not a permission
# tier, and withholding readiness until someone re-labels it produces a queue of
# implementable work that reads as blocked. Only terminal or suspended states
# fall outside.
_AUTHORISED_STATUSES = frozenset({"draft", "pending", "active", "in-progress"})

# The status a gate carries once its verdict has been recorded, written by the
# pass and fail ops together with the verdict. It marks a settled gate, whose
# outcome the plan already carries, as distinct from one still to be read.
GATE_STATUS_CLOSED = "closed"


class GraphTargetError(ValueError):
    """A graph ship target cannot resolve to one complete dependency closure."""

    def __init__(self, handle: str, detail: str) -> None:
        self.handle = handle
        super().__init__(f"graph handle {handle!r}: {detail}")


def _progress(plan: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(plan.get("impl", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def implementable_sections(declarations: Mapping[str, Any] | None) -> list[str]:
    """Return the section ids a classification still declares implementable.

    Landing evidence records an outcome without closing the section. The schema
    predicate selects declared work until reclassification, so the answer comes
    from persisted declarations rather than from the
    section anchors a gate or a comment happens to reference. A plan with no
    persisted classification declares no section work, so the answer is empty
    rather than unknown: a plan row reports presence.
    """

    if not isinstance(declarations, Mapping):
        return []
    return sorted(
        section
        for raw_section, raw_classification in declarations.items()
        if (section := str(raw_section or "").strip())
        and is_implementable_section(raw_classification)
    )


def _plan_authored_state(
    plan: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> Mapping[str, Any]:
    """Read a plan's authored sections from its own file for what the row lacks.

    A composed inventory row carries ``section_declarations`` and the typed
    ``sections`` records already; a discovery row built for the MCP summary
    carries neither. Reading the file fills whichever the row lacks, so one
    authored record answers the section-existence question for both callers
    rather than an empty column on the surface a coordinator actually reads.
    ``docs_dir`` is the checkout the row was inventoried from, matching the
    wiring scan's rule that one tree's rows are judged against that same tree's
    declarations.
    """

    view = dict(plan)
    needs_declarations = not isinstance(plan.get("section_declarations"), Mapping)
    needs_records = plan.get("sections") is None
    if not (needs_declarations or needs_records):
        return view
    if docs_dir is None:
        docs_dir = _load_mounts().get(project)
    if docs_dir is None:
        return view
    try:
        resource = resolve_resource(
            docs_dir, project, slug, "plan", include_archived=False
        )
    except Exception:  # noqa: BLE001 — a resolution error is "no declaration"
        return view
    path = getattr(resource, "path", None)
    if path is None:
        return view

    def read_authored() -> dict[str, Any]:
        state, _text = read_state_and_text_file(path)
        return {
            "section_declarations": state.get("section_declarations"),
            "sections": state.get("sections"),
        }

    authored = memoized("section_authored", path, read_authored)
    if needs_declarations and isinstance(authored.get("section_declarations"), Mapping):
        view["section_declarations"] = authored["section_declarations"]
    if needs_records and authored.get("sections") is not None:
        view["sections"] = authored["sections"]
    return view


def _plan_authored_html(
    plan: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> str:
    """A plan's authored markup, for the heading walk.

    ``_plan_authored_state`` returns the parsed fields but not the text a
    heading walk reads when a row already carries its declarations. This reads
    the same file through the same resolution, memoized by path. An
    unresolvable plan yields ``""`` — the same "nothing authored" reading the
    state helper takes.
    """

    if docs_dir is None:
        docs_dir = _load_mounts().get(project)
    if docs_dir is None:
        return ""
    try:
        resource = resolve_resource(
            docs_dir, project, slug, "plan", include_archived=False
        )
    except Exception:  # noqa: BLE001 — a resolution error is "nothing authored"
        return ""
    path = getattr(resource, "path", None)
    if path is None:
        return ""

    def read_text() -> str:
        return _read_plan_text(path)

    return memoized("plan_authored_html", path, read_text)


_SECTION_WORD_RE = re.compile(
    rf"\b(?:section|sections)\s+({SECTION_NUMBER_PATTERN}"
    rf"(?:\s*(?:,|and)\s*{SECTION_NUMBER_PATTERN})*)",
    re.IGNORECASE,
)
_REF_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_CONTEXT_WINDOW = 8


def _gate_section_refs(
    plan: Mapping[str, Any],
    slug: str,
    gate: Mapping[str, Any],
    all_plans: Mapping[str, dict[str, Any]],
    declared_for: Callable[[str, Mapping[str, Any]], set[str]] | None = None,
) -> list[tuple[str, str, str]]:
    """Section refs a gate names as its outcome source, with their provenance.

    A gate waits on a section in one of two authored forms the roadmap follows:
    a declared outcome source carrying the ``#section`` anchor; or a section
    named in words beside the plan it belongs to, as in
    "cut-cell-current-attribution section 6" or a bare "sections 1 and 2"
    carrying the gate's own plan by context. The declared outcome source is read
    through ``gate_declared_refs``, the one gate-ref reader the wiring check also
    uses. The worded form is read from prose, marked as such, and binds only when
    the named section exists on the named plan, so a passing mention of a section
    never invents an edge.
    """

    found: list[tuple[str, str, str]] = []

    def bind(target_slug: str, stage: str, source: str) -> None:
        if not target_slug or not stage:
            return
        if source == "gate-text":
            target = plan if target_slug == slug else all_plans.get(target_slug)
            known = (
                declared_for(target_slug, target)
                if declared_for is not None and target is not None
                else declared_section_identities(target)
                if target is not None
                else set()
            )
            if stage not in known:
                return
        found.append((f"{target_slug}#{stage}", stage, source))

    # Only the outcome source the gate declares becomes an edge. The extractor's
    # other form — a ``#section`` anchor embedded in the gate's free text — is
    # what the wiring check reads when it asks whether some plan's gate names
    # another, a different question from the section an edge waits on.
    for ref, source in gate_declared_refs(gate):
        if source != "declared":
            continue
        parsed = parse_plan_ref(ref)
        if parsed is None:
            continue
        if parsed.stage:
            bind(parsed.slug or slug, parsed.stage, "declared")
        else:
            found.append((ref, "", "declared"))
    texts = [
        str(gate.get(key) or "") for key in ("measure", "required_evidence", "evidence")
    ]
    for text in texts:
        for match in _SECTION_WORD_RE.finditer(text):
            before = _REF_TOKEN_RE.findall(text[: match.start()])
            context = slug
            for token in reversed(before[-_CONTEXT_WINDOW:]):
                named = parse_plan_ref(token)
                if named is None or named.stage:
                    continue
                if named.slug == slug or named.slug in all_plans:
                    context = named.slug
                    break
            for number in re.split(
                r"\s*(?:,|and)\s*", match.group(1), flags=re.IGNORECASE
            ):
                stage = f"s{int(number)}" if number.isdecimal() else f"s{number}"
                bind(context, stage, "gate-text")
    return found


def _gate_section_edges(
    project: str,
    plan: Mapping[str, Any],
    slug: str,
    all_plans: Mapping[str, dict[str, Any]],
    docs_dir: Path | None,
) -> list[dict[str, Any]]:
    """Resolve each gate's section refs into dependency rows.

    A gate that names one section of another plan blocks its own section on that
    section, so the row carries the waiting section, the target ref with its
    anchor, and the gate it came from. The rows use the ``depends_on`` row shape,
    which puts them on the same surfaces as a declared section ref: the raw
    view's edge, the section readiness split, and the critical path's edge list.
    """

    def declared_for(target_slug: str, target: Mapping[str, Any]) -> set[str]:
        return set(
            declared_section_identities(
                _plan_authored_state(target, docs_dir, project, target_slug)
            )
        )

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for gate in execution_gates(dict(plan)):
        gate_section = str(gate.get("section") or "")
        if not gate_section:
            gated = list(gate.get("gated_sections") or [])
            gate_section = str(gated[0]) if gated else ""
        for ref, stage, source in _gate_section_refs(
            plan, slug, gate, all_plans, declared_for
        ):
            key = (str(gate.get("id") or ""), ref)
            if key in seen:
                continue
            seen.add(key)
            parsed = parse_plan_ref(ref)
            if parsed is None:
                continue
            row: dict[str, Any] = {
                "ref": ref,
                "source_section": gate_section,
                "stage": stage,
                "gate": str(gate.get("id") or ""),
                "source": source,
            }
            if parsed.is_external(project):
                blocking = _blocking_row(plan, ref)
                row.update(
                    {
                        "scope": "external",
                        "project": parsed.project,
                        "slug": parsed.slug,
                        "found": bool(blocking.get("found")) if blocking else True,
                        "status": blocking.get("status", "")
                        if blocking
                        else "satisfied",
                        "satisfied": blocking is None,
                    }
                )
                rows.append(row)
                continue
            target = all_plans.get(parsed.slug)
            if target is None:
                row.update(
                    {
                        "scope": "local",
                        "slug": parsed.slug,
                        "found": False,
                        "satisfied": False,
                    }
                )
                rows.append(row)
                continue
            target_status = _status(target)
            if stage:
                section_found = stage in declared_for(parsed.slug, target)
                satisfied = section_found and _section_satisfied(
                    target,
                    stage,
                    _plan_authored_state(target, docs_dir, project, parsed.slug).get(
                        "section_declarations"
                    ),
                )
                row["section_found"] = section_found
            else:
                satisfied, judgment = _whole_plan_dependency(
                    target, docs_dir, project, parsed.slug
                )
                if judgment is not None:
                    row["judgment"] = _judgment_member(
                        project, slug, project, parsed.slug, judgment
                    )
            row.update(
                {
                    "scope": "local",
                    "slug": parsed.slug,
                    "found": True,
                    "status": target_status,
                    "satisfied": satisfied,
                }
            )
            rows.append(row)
    return rows


def _plan_section_deps(
    plan: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> Mapping[str, Any] | None:
    """Read a plan's section-scoped dependency mapping, from the row or its file.

    A composition carries the mapping only once a composed inventory row has it;
    until then the declaration lives only in the authored markup, so the file is the
    fallback — the same rule the section classification follows, judged against
    the same docs tree the row was inventoried from. ``None`` means the plan
    declares no section-scoped edge, which is the whole-plan behaviour.
    """

    mapping = plan.get("section_depends_on")
    if isinstance(mapping, Mapping):
        return mapping
    if docs_dir is None:
        docs_dir = _load_mounts().get(project)
    if docs_dir is None:
        return None
    try:
        resource = resolve_resource(
            docs_dir, project, slug, "plan", include_archived=False
        )
    except Exception:  # noqa: BLE001 — a resolution error is "no declaration"
        return None
    path = getattr(resource, "path", None)
    if path is None:
        return None

    def read_mapping() -> dict[str, Any]:
        return dict(section_depends_on(_read_plan_text(path)) or {})

    return memoized("section_depends_on", path, read_mapping)


def _plan_decision_sections(
    plan: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> Mapping[str, Any] | None:
    """Read a plan's decision→sections mapping, from the row or its file.

    A composition carries the mapping only once a composed inventory row has
    been given it; until then the scoping lives in the authored markup alone, so
    the file is the fallback — the same rule the section classification and the
    section-scoped edges follow, judged against the same docs tree the row was
    inventoried from. ``None`` means no decision on this plan scopes itself,
    which is the whole-plan behaviour every decision had before.
    """

    mapping = plan.get("decision_sections")
    if isinstance(mapping, Mapping):
        return mapping
    if docs_dir is None:
        docs_dir = _load_mounts().get(project)
    if docs_dir is None:
        return None
    try:
        resource = resolve_resource(
            docs_dir, project, slug, "plan", include_archived=False
        )
    except Exception:  # noqa: BLE001 — a resolution error is "no declaration"
        return None
    path = getattr(resource, "path", None)
    if path is None:
        return None

    def read_mapping() -> dict[str, Any]:
        return dict(decision_sections(_read_plan_text(path)) or {})

    return memoized("decision_sections", path, read_mapping)


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


def dependency_row_state(
    plan: Mapping[str, Any] | None,
    section: str,
    declarations: Any = None,
) -> str:
    """The state a dependency row reports for the ref it named.

    A row-level dependency reports the dependency's own state, never the
    containing plan's: a ref that names a section reports that section's own
    declaration, falling back to the plan's status only where the plan declares
    nothing for it. A plan-level ref has only the plan's status to report.

    ``declarations`` is the plan's section-declaration mapping where the caller
    already holds it from the authored state rather than from the plan payload.
    One function answers this for every surface that builds such a row, so a
    section read on one surface and reported on another cannot disagree.
    """

    data = plan if isinstance(plan, Mapping) else {}
    plan_status = str(data.get("workflow_status") or data.get("status") or "")
    if not section:
        return plan_status
    if declarations is None:
        declarations = data.get("section_declarations")
    if not isinstance(declarations, Mapping):
        return plan_status
    return str(declarations.get(section) or "").strip() or plan_status


def _section_satisfied(
    plan: dict[str, Any],
    section: str,
    declarations: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a target plan has completed one named section.

    A completed plan satisfies every section, and a ``done`` declaration
    satisfies the section it names. An ``implementable`` or ``deferred``
    declaration keeps the section open even when a gate reads passed: the
    declaration is the author's own state for the section, and a deferred or
    still-implementable section satisfies nothing. A passed section gate
    remains the route for a section carrying no declaration.
    """

    if _status(plan) in COMPLETED_STATUSES:
        return True
    if declarations is None:
        declarations = plan.get("section_declarations")
    classification = ""
    if isinstance(declarations, Mapping):
        classification = str(declarations.get(section) or "").strip()
    if classification == SECTION_DECLARATION_DONE:
        return True
    if classification in OPEN_SECTION_DECLARATIONS:
        return False
    section_gates = [
        gate
        for gate in plan.get("gates") or []
        if isinstance(gate, dict) and str(gate.get("section") or "") == section
    ]
    return bool(section_gates) and all(
        str(gate.get("verdict") or "").strip().lower() == "passed"
        for gate in section_gates
    )


def _plan_dependency_satisfied(
    plan: dict[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> bool:
    """Return whether a whole-plan dependency on ``slug`` is satisfied.

    A completed plan satisfies it. Otherwise the dependency clears once at least
    one section is done and none is left implementable, read through the same
    open-section predicate the terminal guard uses: every section is then done
    or deferred, and a deferred section blocks only the work that names it
    rather than the plan's dependents. Requiring at least one done section keeps
    a dependency on unbuilt work from clearing: a plan whose sections are all
    deferred built nothing, and a plan that declares no section at all declares
    no work, so only completion satisfies a dependency on it. A heading with no
    declaration is outstanding work the dependency still needs.
    """

    if _status(plan) in COMPLETED_STATUSES:
        return True
    view = _plan_authored_state(plan, docs_dir, project, slug)
    declarations = view.get("section_declarations")
    if not isinstance(declarations, Mapping) or not declarations:
        return False
    if not any(
        str(value).strip() == SECTION_DECLARATION_DONE
        for value in declarations.values()
    ):
        return False
    for section in open_sections(view):
        if str(declarations.get(section) or "").strip() != SECTION_DECLARATION_DEFERRED:
            return False
    declared = {str(section).strip() for section in declarations}
    for identity, _heading in authored_section_headings(
        _plan_authored_html(plan, docs_dir, project, slug)
    ):
        if identity not in declared:
            return False
    return True


#: Reasons a whole-plan dependency holds without a fixed rule settling it. The
#: roadmap reports these so the orchestrator decides, rather than the code
#: silently clearing a dependency on work that was never built.
JUDGMENT_REASON_DEFERRED_ONLY = "deferred-only"
JUDGMENT_REASON_UNDECLARED_HEADING = "undeclared-heading"


def _plan_dependency_judgment(
    plan: dict[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> tuple[str, list[str]] | None:
    """Return the judgment reason holding a whole-plan dependency, if any.

    A whole-plan dependency is uncertain only where no fixed rule applies: the
    target has no section left implementable, yet the dependency does not clear.
    That is a target whose every declared section is deferred (nothing was
    built) or a target carrying an authored heading with no declaration. Both
    hold the dependent by default and are reported, so the orchestrator decides
    to build or un-defer the section, or to narrow or remove the dependency. A
    target with a section still implementable is plainly incomplete and is not
    uncertain, and a target that declares no sections keeps the completion-only
    rule — neither is a judgment case.
    """

    if _status(plan) in COMPLETED_STATUSES:
        return None
    view = _plan_authored_state(plan, docs_dir, project, slug)
    declarations = view.get("section_declarations")
    if not isinstance(declarations, Mapping) or not declarations:
        return None
    if any(
        str(declarations.get(section) or "").strip() != SECTION_DECLARATION_DEFERRED
        for section in open_sections(view)
    ):
        return None
    declared = {str(section).strip() for section in declarations}
    undeclared = [
        identity
        for identity, _heading in authored_section_headings(
            _plan_authored_html(plan, docs_dir, project, slug)
        )
        if identity not in declared
    ]
    if undeclared:
        return JUDGMENT_REASON_UNDECLARED_HEADING, undeclared
    if not any(
        str(value).strip() == SECTION_DECLARATION_DONE
        for value in declarations.values()
    ):
        return JUDGMENT_REASON_DEFERRED_ONLY, sorted(open_sections(view))
    return None


def _whole_plan_dependency(
    plan: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> tuple[bool, tuple[str, list[str]] | None]:
    """Return a whole-plan dependency's verdict and its judgment in one call.

    One helper owns the whole-plan decision so every site that can hold a
    dependent reaches the judgment with the verdict, rather than each site
    calling the verdict alone and the judgment only where a row is built. A
    site that reads ``judgment`` and, when it is not ``None``, records the
    member in ``judgment_required`` guarantees the orchestrator is told
    whenever an uncertain dependency holds something — through a declared
    dependency, a gate ref, an after edge, or the graph closure alike.
    """

    satisfied = _plan_dependency_satisfied(plan, docs_dir, project, slug)
    if satisfied:
        return True, None
    return False, _plan_dependency_judgment(plan, docs_dir, project, slug)


def _judgment_member(
    dependent_project: str,
    dependent: str,
    target_project: str,
    target: str,
    judgment: tuple[str, list[str]],
) -> dict[str, Any]:
    """Compose one ``judgment_required`` member naming a held dependency.

    Each side is qualified with its own project: a held dependency may cross a
    project boundary in a graph, so the target does not belong to the
    dependent's project and naming it there points at the wrong plan.
    """

    reason, sections = judgment
    return {
        "dependent": _qualified_plan(dependent_project, dependent),
        "target": _qualified_plan(target_project, target),
        "reason": reason,
        "sections": list(sections),
    }


def execution_gates(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Plan gates whose verdict holds execution.

    A transition gate holds a closure or a choice instead, so it never leaves
    the plan itself blocked; the split is what every surface reading readiness
    must share.
    """

    return [
        gate
        for gate in plan.get("gates") or []
        if isinstance(gate, dict) and gate.get("transition") not in GATE_TRANSITIONS
    ]


def split_unpassed_gates(
    plan: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a plan's unpassed execution gates by the work they still hold.

    A closed gate holds nothing, whatever its verdict: a recorded verdict is the
    plan's outcome, and a failed one records that the measure will not be met
    and the work proceeds without it, so a settled gate is never work to wait
    for. A gate is settled when its status says so, which the pass and fail ops
    write together with the verdict.

    An open gate holds a section only while that section is still implementable:
    an open gate whose named sections are all done, descoped or deferred holds
    nothing, because the declaration already carries the section. An open gate
    that names implementable sections holds those sections, so long as the plan
    declares at least one implementable section none of its open gates names --
    there is still work the gate leaves reachable. When every implementable
    section is named by an open gate, or the plan declares no implementable
    section at all, the open gates that name reachable work hold the whole plan
    because no section is left to carry it. An open gate that names no sections
    always holds the plan.

    A plan that classified no sections tells this split nothing about which of
    them a gate still holds, so it keeps the reading an undeclared plan had
    before declarations were consulted at all: every unpassed gate holds it.
    That population is the one the readiness ladder sets aside, and it is why
    the settled-gate reading above sits inside the declaration-aware branch.

    Both lists carry the row shape :func:`unpassed_gate_blockers` produces, so a
    caller reports the first into the plan's blocker list and
    ``effective_status``, and the second beside it as the section-scoped holds.
    """

    gates = unpassed_gate_blockers(execution_gates(plan))
    declarations = plan.get("section_declarations")
    if not isinstance(declarations, Mapping) or not declarations:
        return gates, []
    gates = [gate for gate in gates if not _gate_is_closed(gate)]
    declared = set(implementable_sections(declarations))
    naming = [gate for gate in gates if _names_implementable(gate, declared)]
    named = {
        str(section) for gate in naming for section in gate.get("gated_sections") or []
    }
    if declared - named:
        plan_holding = [gate for gate in gates if not gate.get("gated_sections")]
        return plan_holding, naming
    plan_holding = [
        gate
        for gate in gates
        if not gate.get("gated_sections") or _names_implementable(gate, declared)
    ]
    return plan_holding, []


def _gate_is_closed(gate: Mapping[str, Any]) -> bool:
    """Whether a gate's status records a verdict the plan has already settled.

    The pass and fail ops set this status together with the verdict, so it is
    the field that marks a gate as concluded rather than still to be read.
    """

    return str(gate.get("status") or "").strip().lower() == GATE_STATUS_CLOSED


def _names_implementable(gate: Mapping[str, Any], declared: set[str]) -> bool:
    """Whether a gate still names a section the plan declares implementable."""

    return any(str(section) in declared for section in gate.get("gated_sections") or [])


def is_closable(plan: Mapping[str, Any]) -> bool:
    """Whether a plan declares sections and has resolved every one of them.

    ``plan_executable_remainder`` is the one reader of "does a declaration still
    admit work", and its known zero is a plan whose declared sections are all
    done, descoped or deferred. This asks that reader the question its answer is
    about — a plan whose sections are all resolved — and reads a plan that
    declares no sections as the unknown it is: it keeps the readiness the ladder
    would otherwise give it. The key check below is where that reading differs
    from the two collaborators this function calls, and why.
    """

    declarations = plan.get("section_declarations")
    # Deliberate divergence from ``plan_executable_remainder``, which documents an
    # empty declaration as a known zero, and from ``implementable_sections``,
    # which reads an absent one as declaring no work. Neither reading survives
    # the row this runs on: ``readiness_from_state`` in ``metadata_index``
    # collapses an absent declaration set to an empty mapping for every
    # discovered plan, so an inventory row cannot tell a plan that never
    # classified its sections from one that classified none. Taking the empty
    # mapping as resolved would read every legacy plan as finished and empty the
    # ready set of any project whose plans carry no declarations, so the unknown
    # remainder wins here and only a plan that declares sections it has resolved
    # reads closable.
    if not isinstance(declarations, Mapping) or not declarations:
        return False
    return plan_executable_remainder(plan) == 0


def closure_blockers(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Name what stands between a plan and ``close``, with the ops that clear it.

    A plan that still admits implementable work is held for closure only by its
    plan-terminal gates: the question of closing is not live until the declared
    work is resolved. Once no declaration admits work, every remaining obstacle
    is named — deferred sections, open followups, open execution gates and
    plan-terminal gates — each carrying the ops that clear it, and the list ends
    with the ``close`` op so a reader holding the row has the writes to make. A
    finished plan kept open on purpose states its reason with a plan-terminal
    gate whose measure says why, which appears here as any other gate.
    """

    slug = plan.get("slug", "")
    if not is_closable(plan):
        return [
            {"kind": "gate", "plan": slug, **gate}
            for gate in pending_transition_gates(
                plan.get("gates") or [], "plan-terminal"
            )
        ]
    declarations = plan.get("section_declarations")
    blockers: list[dict[str, Any]] = [
        {
            "kind": "section",
            "plan": slug,
            "id": section,
            "status": SECTION_DECLARATION_DEFERRED,
            "ops": ["descope"],
        }
        for section, classification in sorted((declarations or {}).items())
        if str(classification or "").strip() == SECTION_DECLARATION_DEFERRED
    ]
    blockers.extend(
        {
            "kind": "followup",
            "plan": slug,
            "id": followup.get("id", ""),
            "ops": ["resolve"],
        }
        for followup in plan.get("followups") or []
        if isinstance(followup, dict) and followup.get("status", "open") == "open"
    )
    blockers.extend(
        {"kind": "gate", "plan": slug, **gate, "ops": ["pass", "fail"]}
        for gate in unpassed_gate_blockers(execution_gates(plan))
    )
    blockers.extend(
        {"kind": "gate", "plan": slug, **gate, "ops": ["pass", "fail"]}
        for gate in pending_transition_gates(plan.get("gates") or [], "plan-terminal")
    )
    blockers.append({"kind": "op", "plan": slug, "ops": ["close"]})
    return blockers


def _dispatchability(plan: dict[str, Any]) -> tuple[bool, list[str]]:
    gates = execution_gates(plan)
    if not gates:
        return True, []

    missing: list[str] = []
    if not any(
        isinstance(followup, dict) and followup.get("status", "open") == "open"
        for followup in plan.get("followups") or []
    ):
        missing.append("open_followup")
    return not missing, missing


def _decision_rows(
    plan: dict[str, Any],
    sections_map: Mapping[str, Any] | None = None,
    *,
    waiting_context: bool = False,
) -> list[dict[str, Any]]:
    """Return decisions with an explicit readiness state for roadmap consumers.

    ``sections_map`` names the decisions that scope themselves to the sections
    they govern. A scoped decision is reported like any other — its row carries
    the ``sections`` it holds — but it holds only those, so the plan's other
    sections stay dispatchable. It lives on the caller's side because the
    scoping is authored markup, which a composed row may not carry yet; see
    :func:`_plan_decision_sections`.

    ``waiting_context`` adds what a reader of a *wait* needs and what a row
    consumed for readiness does not: the owning project, the recorded
    recommendation and who made it, the plan's last-modified date, and the
    scope the wait holds — every waiting row carries ``sections`` as a list and
    ``holds_whole_plan``, so a plan-level wait and a row whose field is simply
    absent do not read alike. An open decision records no date of its own, so
    the plan's date is the only signal of how long the wait has run. It is
    opt-in because a caller that consumes rows for readiness compares them
    whole, and a new field would read as a different decision rather than the
    same one with more to say.
    """

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
    plan_context = {
        "project": str(plan.get("project") or ""),
        # An inventory row names the date differently on each read path, so
        # both spellings are tried rather than the wait reading as undated.
        "plan_modified": str(plan.get("modified") or plan.get("last") or ""),
    }
    for decision in decisions:
        key = str(decision.get("key") or "").strip()
        choice = str(decision.get("choice") or decision.get("chosen") or "").strip()
        rationale = str(decision.get("rationale") or "").strip()
        status = "locked" if choice else "deferred" if rationale else "open"
        transition_gates = [
            gate
            for gate in pending_transition_gates(
                plan.get("gates") or [], "decision-lockable"
            )
            if gate.get("decision") == key
        ]
        if transition_gates and not choice:
            status = "gated"
        scoped = decision.get("sections")
        if not isinstance(scoped, list):
            scoped = (sections_map or {}).get(key)
        sections = (
            [str(part).strip() for part in scoped if str(part or "").strip()]
            if isinstance(scoped, list)
            else []
        )
        # A wait that names no section holds the whole plan, and a reader
        # cannot tell that from a row whose field is missing. The waiting rows
        # therefore always state the scope: the list is present and possibly
        # empty, and the whole-plan case says so in a field of its own.
        scope = (
            {"sections": sections, "holds_whole_plan": not sections}
            if waiting_context
            else ({"sections": sections} if sections else {})
        )
        rows.append(
            {
                "kind": "decision",
                "plan": plan_slug,
                "id": key,
                "question": str(decision.get("title") or key),
                "status": status,
                "choice": choice,
                "rationale": rationale,
                **(
                    {
                        **plan_context,
                        "recommended": str(decision.get("recommended") or "").strip(),
                        "recommended_by": str(
                            decision.get("recommended_by") or ""
                        ).strip(),
                    }
                    if waiting_context
                    else {}
                ),
                **scope,
                **(
                    {
                        "transition": "decision-lockable",
                        "gate_id": transition_gates[0].get("id", ""),
                        "gating_plan": transition_gates[0].get("gating_plan", ""),
                        "transition_gates": transition_gates,
                    }
                    if transition_gates
                    else {}
                ),
            }
        )
    return rows


def unsettled_decisions(
    plan: dict[str, Any],
    sections_map: Mapping[str, Any] | None = None,
    *,
    waiting_context: bool = False,
) -> list[dict[str, Any]]:
    """Decisions awaiting a choice, whether open or held by a transition edge.

    A gated decision is held rather than blocking: readiness consumes only the
    open rows, while a reported set keeps both so the edge stays visible. A
    decision scoped to named sections is held the same way — reported in full,
    consumed by readiness only for the sections it names.

    ``waiting_context`` carries the owning project, the recorded recommendation
    and the plan's date onto each row, for a caller that lists the wait rather
    than consuming it for readiness; see :func:`_decision_rows`.
    """

    return [
        row
        for row in _decision_rows(plan, sections_map, waiting_context=waiting_context)
        if row["status"] in ("open", "gated")
    ]


def waiting_decision_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The open and gated decisions one roadmap report is waiting on.

    Every surface that lists a wait reads it from here rather than deriving the
    wait a second time, so a decision cannot appear on one surface and go
    missing from the other. A locked or deferred decision holds no work and is
    not a wait, so it is not listed.
    """

    return [
        dict(row)
        for row in report.get("waiting_decisions") or []
        if isinstance(row, dict)
    ]


def plan_level_decisions(
    decisions: list[dict[str, Any]], statuses: tuple[str, ...] = ("open",)
) -> list[dict[str, Any]]:
    """Decisions holding the whole plan rather than the sections they name.

    An unscoped decision is a wait with nowhere else to land, so the plan waits
    on it. A scoped one names its sections, and those sections carry the wait
    instead: the plan's other sections stay dispatchable, which is the whole
    point of scoping a decision rather than leaving it whole-plan.
    """

    return [
        row
        for row in decisions
        if row["status"] in statuses and not row.get("sections")
    ]


def plan_blocker_state(
    plan: Mapping[str, Any],
    *,
    dependency_blockers: Iterable[Mapping[str, Any]] = (),
    blockers: Iterable[Mapping[str, Any]] | None = None,
    sections_map: Mapping[str, Any] | None = None,
    released: bool = False,
) -> dict[str, Any]:
    """The blockers one plan row reports, and the blocked state they give it.

    Every surface that answers "is this plan blocked, and by what" answers it
    from here. The pass holds the assembly in one place, because two derivations
    of the same fact drift: a plan held by a plan-level open decision reads
    blocked through the roadmap and open through a second assembly that consults
    only gates and dependencies, and nothing compares them.

    ``dependency_blockers`` are the unsatisfied dependency rows the caller
    resolved, which are the one part of the list a per-plan pass cannot build
    for itself: resolution needs the project's other rows.

    ``blockers`` is the plan's reported blocker list when the caller has one
    outside the row, as a discovery row read beside composed sprint items does.
    Absent, the row's own ``blocking`` list is read. Only the ``explicit`` and
    ``held`` kinds are taken from either, because the gate and decision blockers
    are assembled here from the authored state rather than read back from a
    list that may already carry them.

    ``released`` names a plan whose cleared blockers a probe already released;
    its persisted ``blocked`` status is then a record of work rather than a
    wait, so no placeholder blocker is synthesized for it.
    """

    reported = (
        list(blockers) if blockers is not None else list(plan.get("blocking") or [])
    )
    reported = [dict(row) for row in reported if isinstance(row, Mapping)]
    explicit_blockers = [row for row in reported if row.get("kind") == "explicit"]
    held_blockers = [row for row in reported if row.get("kind") == "held"]
    gate_blockers, section_gate_blockers = split_unpassed_gates(plan)
    decisions = _decision_rows(plan, sections_map)
    plan_holding_decisions = plan_level_decisions(decisions)
    blocking = [
        *dependency_blockers,
        *explicit_blockers,
        *held_blockers,
        *gate_blockers,
        *plan_holding_decisions,
    ]
    # A stored blocked status is the open-state spelling for a wait whose rows a
    # composed list no longer carries; the placeholder keeps it visible instead
    # of the plan reading deferred while it is stored blocked.
    if (
        str(plan.get("status") or "").strip().lower() == "blocked"
        and not blocking
        and not released
    ):
        explicit_blockers = [{"kind": "persisted", "id": "unrecorded"}]
        blocking = [*explicit_blockers]
    closable = is_closable(plan)
    return {
        "blocking": blocking,
        "explicit_blockers": explicit_blockers,
        "held_blockers": held_blockers,
        "gate_blockers": gate_blockers,
        "section_gate_blockers": section_gate_blockers,
        "decision_blockers": [
            row for row in decisions if row["status"] in ("open", "gated")
        ],
        "deferred_decisions": [row for row in decisions if row["status"] == "deferred"],
        "plan_holding_decisions": plan_holding_decisions,
        "closable": closable,
        "blocked": bool(blocking) and not closable,
        "effective_status": effective_status(
            str(plan.get("status") or "draft"), blocking
        ),
    }


def _authorisation_age(
    project: str,
    plan: dict[str, Any],
) -> tuple[int | None, str]:
    docs_dir = _load_mounts().get(project)
    fallback_path = None
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
        created_at=plan.get("created"),
        fallback_path=fallback_path,
        repo_dir=docs_dir.parent if docs_dir is not None else None,
        docs_dir=docs_dir,
    )


def _blocking_row(plan: dict[str, Any], ref: str) -> dict[str, Any] | None:
    for row in plan.get("blocking") or []:
        if isinstance(row, dict) and row.get("ref") == ref:
            return row
    return None


def _after_edges(
    project: str,
    plan: dict[str, Any],
    all_plans: Mapping[str, dict[str, Any]],
    artifacts: Mapping[str, list[dict[str, Any]]],
    docs_dir: Path | None,
) -> list[dict[str, Any]]:
    """Resolve a plan's soft ``after`` refs into annotation rows.

    An after edge expresses *start after X lands if you can, do not wait*: it
    orders work and never holds it. Each row records what the ref resolved to
    so the ready set can name the targets it is sequenced behind. ``satisfied``
    is ``True`` for a target recorded complete, ``False`` for one observed
    in-project and still open, and ``None`` where nothing could be observed —
    an unresolvable ref, or a cross-project one the inventory reader does not
    follow for soft edges. ``None`` ranks nothing: an unobserved target is not
    evidence that it has not shipped.
    """
    rows: list[dict[str, Any]] = []
    for ref in plan.get("after") or []:
        parsed = parse_plan_ref(ref)
        if parsed is None:
            rows.append(
                {"ref": ref, "scope": "invalid", "found": False, "satisfied": None}
            )
            continue
        if parsed.is_external(project):
            row = {
                "ref": ref,
                "scope": "external",
                "project": parsed.project,
                "slug": parsed.slug,
                "found": True,
                "satisfied": None,
            }
            if parsed.stage:
                row["stage"] = parsed.stage
            rows.append(row)
            continue
        target = all_plans.get(parsed.slug)
        if target is None:
            rows.append(
                {
                    "ref": ref,
                    "scope": "local",
                    "slug": parsed.slug,
                    "found": False,
                    "artifact_types": [
                        item.get("type", "plan")
                        for item in artifacts.get(parsed.slug, [])
                        if item.get("type", "plan") != "plan"
                    ],
                }
            )
            continue
        target_status = _status(target)
        row = {
            "ref": ref,
            "scope": "local",
            "slug": parsed.slug,
            "found": True,
            "status": target_status,
        }
        if parsed.stage:
            declarations = _plan_authored_state(
                target, docs_dir, project, parsed.slug
            ).get("section_declarations")
            found_section = parsed.stage in declared_section_identities(
                _plan_authored_state(target, docs_dir, project, parsed.slug)
            )
            row["stage"] = parsed.stage
            row["section_found"] = found_section
            row["satisfied"] = found_section and _section_satisfied(
                target, parsed.stage, declarations
            )
        else:
            satisfied, judgment = _whole_plan_dependency(
                target, docs_dir, project, parsed.slug
            )
            row["satisfied"] = satisfied
            if judgment is not None:
                row["judgment"] = _judgment_member(
                    project,
                    str(plan.get("slug") or ""),
                    project,
                    parsed.slug,
                    judgment,
                )
        rows.append(row)
    return rows


def _section_scoped_edges(
    project: str,
    plan: Mapping[str, Any],
    slug: str,
    mapping: Mapping[str, Any],
    all_plans: Mapping[str, dict[str, Any]],
    docs_dir: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve a section-scoped mapping into per-section blocker rows and findings.

    Each row names the waiting section beside the target it waits on, so a plan's
    sections split into the dispatchable and the held while the plan itself keeps
    its place in the ready set: the edge runs between sections, and a plan-level
    veto is the very behaviour this declaration exists to replace. The refs use
    the ``depends_on`` grammar, a ``project:``-qualified one included.

    A ref that parses but resolves to no live section still holds its waiting
    section, because an unresolvable edge is no evidence that its dependency
    landed. A malformed ref names no section to hold and so holds nothing, but it
    is refused with a finding rather than dropped, so a mapping that does nothing
    says why. The same holds for an entry whose own identity is not a section:
    it is refused by name and its refs build no edge, so neither a phantom
    section nor a phantom blocker enters the sets. One malformed entry never
    exempts the entries beside it from being read.
    """

    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    def target_row(target_project: str, target_slug: str) -> Mapping[str, Any] | None:
        if not target_slug or target_project != project:
            return None
        row = all_plans.get(target_slug)
        if row is None:
            return None
        return _plan_authored_state(row, docs_dir, project, target_slug)

    refusals = section_dependency_refusals(
        mapping,
        target_row,
        owning_project=project,
    )
    findings.extend(
        _finding(
            str(refusal["code"]),
            "error",
            str(refusal["message"]),
            slug=slug,
            extra={"ref": refusal["ref"], "section": refusal["section"]},
        )
        for refusal in refusals
    )
    for raw_section, raw_refs in mapping.items():
        waiting_section = str(raw_section or "").strip()
        if not is_section_identity(waiting_section):
            # An entry that names no section holds nothing; the refusals have
            # refused it by name, so no phantom section is added to the sets.
            continue
        refs = [raw_refs] if isinstance(raw_refs, str) else raw_refs
        if not isinstance(refs, list):
            continue
        for ref in refs:
            parsed = parse_plan_ref(str(ref))
            if parsed is None or not parsed.stage:
                continue
            row: dict[str, Any] = {
                "ref": str(ref),
                "source_section": waiting_section,
                "stage": parsed.stage,
            }
            if parsed.is_external(project):
                blocking = _blocking_row(plan, str(ref))
                row.update(
                    {
                        "scope": "external",
                        "project": parsed.project,
                        "slug": parsed.slug,
                        "found": bool(blocking.get("found")) if blocking else True,
                        "status": blocking.get("status", "")
                        if blocking
                        else "satisfied",
                        "satisfied": blocking is None,
                    }
                )
                rows.append(row)
                continue
            target = all_plans.get(parsed.slug)
            if target is None:
                row.update(
                    {
                        "scope": "local",
                        "slug": parsed.slug,
                        "found": False,
                        "satisfied": False,
                    }
                )
                rows.append(row)
                findings.append(
                    _finding(
                        "dangling-hard-dependency",
                        "error",
                        (
                            f"{slug}: section dependency {ref!r} resolves to no "
                            "live plan"
                        ),
                        slug=slug,
                        extra={"ref": str(ref), "section": waiting_section},
                    )
                )
                continue
            authored = _plan_authored_state(target, docs_dir, project, parsed.slug)
            declarations = authored.get("section_declarations")
            section_found = parsed.stage in declared_section_identities(authored)
            row.update(
                {
                    "scope": "local",
                    "slug": parsed.slug,
                    "found": True,
                    "status": dependency_row_state(target, parsed.stage, declarations),
                    "section_found": section_found,
                    "satisfied": section_found
                    and _section_satisfied(target, parsed.stage, declarations),
                }
            )
            rows.append(row)
    return rows, findings


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


#: Section references as prose writes them: ``slug#sN`` or ``project:slug#sN``.
#: Candidates are validated by ``parse_plan_ref``, so the grammar stays the
#: ``depends_on`` ref's rather than a second copy of it.
_SECTION_REF_IN_TEXT_RE = re.compile(r"[A-Za-z0-9_.:\-]+#[A-Za-z0-9_.\-]+")


def _mentioned_section_refs(text: str) -> list[str]:
    """The section references a block of prose names, once each.

    A sentence's closing punctuation is trimmed before the candidate is
    validated, so a trailing full stop names no section.
    """

    found: list[str] = []
    for match in _SECTION_REF_IN_TEXT_RE.findall(str(text or "")):
        ref = match.rstrip(".,;:")
        parsed = parse_plan_ref(ref)
        if parsed is not None and parsed.stage and ref not in found:
            found.append(ref)
    return found


def _normalized_section_ref(project: str, ref: str) -> str:
    """One section reference spelled one way, so two spellings compare equal."""

    parsed = parse_plan_ref(ref)
    if parsed is None:
        return str(ref)
    prefix = f"{parsed.project}:" if parsed.project not in (None, project) else ""
    return f"{prefix}{parsed.slug}#{parsed.stage or ''}"


def _undeclared_wait_findings(
    project: str,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
    open_plan_slugs: Iterable[str],
    section_runs: Mapping[tuple[str, str], Iterable[Mapping[str, Any]]],
    section_holds_by_plan: Mapping[str, Mapping[str, Iterable[Mapping[str, Any]]]],
    declared_refs: Mapping[tuple[str, str], Iterable[str]],
) -> list[dict[str, Any]]:
    """Report a section held by something that names a wait it never declared.

    Three kinds of hold can name the section another one waits on: a run the
    classifier read blocked, whose reason carries the manifest's own blocker
    text; a gate measure whose verdict is not passed; and an open followup,
    attributed to the section its invocation names. The ref a hold names is a
    dependency in everything but the declaration, so the repair is one
    ``section_depends_on.<sid>`` write; the analyser never invents the edge.
    """

    open_plans = set(open_plan_slugs)
    mentions: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for (plan_ref, section), rows in sorted(section_runs.items()):
        if plan_ref not in open_plans:
            continue
        for row in run_blocker_rows(rows):
            text = str(row.get("blocker") or "")
            if text:
                mentions[(plan_ref, section)].append(("blocked run", text))
    for plan_ref in sorted(open_plans):
        for section, holds in sorted(section_holds_by_plan.get(plan_ref, {}).items()):
            for hold in holds:
                if str(hold.get("kind") or "") == GATE_BLOCKER and hold.get("measure"):
                    mentions[(plan_ref, section)].append(
                        ("gate measure", str(hold["measure"]))
                    )
        for followup in _plan_followups(
            plans.get(plan_ref) or {}, docs_dir, project, plan_ref
        ):
            if not isinstance(followup, Mapping):
                continue
            if str(followup.get("status") or "open") != "open":
                continue
            section = str(getattr(find_invocation(followup), "section", "") or "")
            if not section:
                continue
            text = " ".join(
                str(followup.get(field) or "")
                for field in ("prompt", "recommends_skill")
            ).strip()
            if text:
                mentions[(plan_ref, section)].append(("open followup", text))
    findings: list[dict[str, Any]] = []
    for (plan_ref, section), entries in sorted(mentions.items()):
        declared = {
            _normalized_section_ref(project, ref)
            for ref in declared_refs.get((plan_ref, section), ())
        }
        waiting = f"{plan_ref}#{section_record_id(section)}"
        reported: set[str] = set()
        for source, text in entries:
            for ref in _mentioned_section_refs(text):
                mentioned = _normalized_section_ref(project, ref)
                if mentioned == waiting or mentioned in declared | reported:
                    continue
                reported.add(mentioned)
                findings.append(
                    _finding(
                        "undeclared-wait",
                        "warn",
                        f"{waiting}: the {source} names {mentioned}, which "
                        f"{waiting}'s dependencies do not include",
                        slug=plan_ref,
                        extra={
                            "waiting": waiting,
                            "ref": mentioned,
                            "source": source,
                            "text": text,
                        },
                    )
                )
    return findings


def _overlapping_scope_findings(
    open_by_plan: Mapping[str, Iterable[str]],
    section_runs: Mapping[tuple[str, str], Iterable[Mapping[str, Any]]],
    claims: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The section pairs whose claimed write paths intersect across plans.

    A write path is declared by the run that claims it, binding or not, and only
    runs placed on a section are attributed. Two sections of one plan claiming a
    path is that plan's own sequencing; across plans the pairs are read through
    the dispatcher's own overlap rule. Only a section the plan still declares
    open is reviewed, so a run pointer left on a landed section is not a
    collision.
    """

    from reckon.crew.runs import _scopes_overlap

    placement = {
        str(row.get("run_id") or ""): key
        for key, rows in section_runs.items()
        for row in rows
    }
    paths: dict[tuple[str, str], set[str]] = defaultdict(set)
    for claim in claims:
        key = placement.get(str(claim.get("run_id") or ""))
        if key is not None and claim.get("path"):
            paths[key].add(str(claim["path"]))
    open_keys = (key for key in paths if key[1] in set(open_by_plan.get(key[0], ())))
    reviewed = sorted(open_keys)
    findings: list[dict[str, Any]] = []
    for index, left in enumerate(reviewed):
        for right in reviewed[index + 1 :]:
            if left[0] == right[0]:
                continue
            shared = [
                {"left_path": first, "right_path": second}
                for first in sorted(paths[left])
                for second in sorted(paths[right])
                if _scopes_overlap(first, second)
            ]
            if not shared:
                continue
            left_ref, right_ref = f"{left[0]}#{left[1]}", f"{right[0]}#{right[1]}"
            findings.append(
                _finding(
                    "overlapping-scope",
                    "warn",
                    f"{left_ref} and {right_ref} declare intersecting write "
                    f"paths: {', '.join(pair['left_path'] for pair in shared)}",
                    slug=left[0],
                    extra={"sections": [left_ref, right_ref], "paths": shared},
                )
            )
    return findings


def _standalone_reason(docs_dir: Path | None, project: str, slug: str) -> str | None:
    """Read a plan's standalone declaration from its own file, if it has one."""

    if docs_dir is None:
        return None
    try:
        resource = resolve_resource(
            docs_dir, project, slug, "plan", include_archived=False
        )
    except Exception:  # noqa: BLE001 — a resolution error is "no declaration"
        return None
    if resource is None or getattr(resource, "path", None) is None:
        return None
    path = resource.path

    def read_declaration() -> str | None:
        return standalone_reason(_read_plan_text(path))

    return memoized("standalone_reason", path, read_declaration)


def _declares_section_wire(
    plan: Mapping[str, Any], docs_dir: Path | None, project: str, slug: str
) -> bool:
    """Whether a plan row declares a section-level wire rather than a plan-level one.

    The two forms are the soft ``after`` field and the section-scoped
    ``plan-section-depends-on`` mapping, read from the row or its file through
    the same reader the roadmap builds its edges from. Either one wires the
    plan as surely as a plan-level entry, so neither leaves it unwired.
    """

    return bool(plan.get("after")) or bool(
        _plan_section_deps(plan, docs_dir, project, slug)
    )


def _unwired_plan_findings(
    project: str,
    plans: Mapping[str, dict[str, Any]],
    docs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Report in-scope implementable plans that declare no wire at all.

    The same condition, message and severities the audit emits, so a plan the
    audit refuses is also visible on the sprint view rather than only in a
    separate command.

    ``docs_dir`` is the checkout the plan rows were inventoried from. A
    worktree-scoped roadmap inventories a worktree's rows, so it must read the
    standalone declarations from that same tree — reading the registered mount
    instead reports one tree's plans against another tree's declarations. It
    falls back to the registered mount only when the caller supplies none.
    """

    if docs_dir is None:
        docs_dir = _load_mounts().get(project)

    # Derived once over the whole project: a plan some other plan's gate names
    # as its required evidence is wired by that gate, and one row's check
    # cannot see the gates the rest of the project holds.
    inbound_evidence = set()
    for other in plans.values():
        if str(other.get("type") or "plan") != "plan":
            continue
        inbound_evidence |= gate_evidence_slugs_from_gates(
            other.get("gates") or [], project=project
        )

    out: list[dict[str, Any]] = []
    for slug, plan in sorted(plans.items()):
        if str(plan.get("type") or "plan") != "plan":
            continue
        links = (
            list(plan.get("depends_on") or [])
            + list(plan.get("blocks") or [])
            + list(plan.get("informs") or [])
        )
        gates = list(plan.get("gates") or [])
        if links or gates:
            continue
        finding = unwired_plan_finding(
            doc_type="plan",
            status=str(plan.get("status") or "").strip().lower(),
            modified=str(plan.get("modified") or ""),
            links=links,
            gate_count=len(gates),
            standalone=_standalone_reason(docs_dir, project, slug),
            slug=slug,
            section_wires=_declares_section_wire(plan, docs_dir, project, slug),
            inbound_evidence=slug in inbound_evidence,
        )
        if finding is not None:
            out.append(
                _finding(
                    finding.code,
                    finding.severity,
                    finding.message,
                    slug=slug,
                )
            )
    return out


def _plan_followups(
    plan: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> list[Any]:
    """Read a plan's followups from its own file when the row carries none.

    A composed inventory row carries ``followups`` already; a discovery row
    built for another surface does not. The file fallback is the same one the
    section classification takes, so a roadmap composed from either kind of row
    reports the same followups.
    """

    rows = plan.get("followups")
    if isinstance(rows, list):
        return rows
    if docs_dir is None:
        return []
    try:
        resource = resolve_resource(
            docs_dir, project, slug, "plan", include_archived=False
        )
    except Exception:  # noqa: BLE001 — a resolution error is "no followups"
        return []
    path = getattr(resource, "path", None)
    if path is None:
        return []
    try:
        return list(read_state_and_text_file(path)[0].get("followups") or [])
    except Exception:  # noqa: BLE001 — an unreadable plan holds no readable row
        return []


def _followup_work_findings(
    project: str,
    plans: Mapping[str, dict[str, Any]],
    docs_dir: Path | None = None,
    sprint_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Report open followups whose invocation hides work rather than pointing.

    Every plan the roadmap inventories is judged, completed plans included: a
    followup on a completed plan names work no surface can dispatch, and the
    finding is how the roadmap says so. The classification is
    :func:`reckon.followup_pointers.classify_followup`'s and nothing here
    re-derives it.

    ``docs_dir`` is the checkout the plan rows were inventoried from, so a row
    carrying no ``section_declarations`` is judged against the same tree's
    declarations rather than the registered mount's.
    """

    out: list[dict[str, Any]] = []
    sprint_set = {str(identity) for identity in sprint_ids}
    for slug, plan in sorted(plans.items()):
        if str(plan.get("type") or "plan") != "plan":
            continue
        declarations = _plan_authored_state(plan, docs_dir, project, slug).get(
            "section_declarations"
        )
        for followup in _plan_followups(plan, docs_dir, project, slug):
            if not isinstance(followup, Mapping):
                continue
            if str(followup.get("status") or "open") != "open":
                continue
            verdict = classify_followup(
                plan,
                followup,
                project=project,
                declarations=declarations,
                sprint_ids=sprint_set,
            )
            if verdict.pointer:
                continue
            followup_id = str(followup.get("id") or "")
            out.append(
                _finding(
                    "followup-hides-work",
                    "warn",
                    f"{slug}: followup {followup_id} hides work ({verdict.reason})",
                    slug=slug,
                    extra={"followup": followup_id, "reason": verdict.reason},
                )
            )
    return out


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
    sprints: list[dict[str, Any]], owning_project: str = ""
) -> tuple[dict[str, list[str]], dict[str, int]]:
    membership: dict[str, list[str]] = defaultdict(list)
    order: dict[str, int] = {}
    closed_statuses = {"done", "shipped", "archived"}
    for position, sprint in enumerate(sprints):
        sprint_id = str(sprint.get("id") or "")
        if not sprint_id:
            continue
        sprint_ref = str(sprint.get("_ref") or sprint_id)
        sprint_project = str(sprint.get("_project") or owning_project)
        order[sprint_ref] = position
        if str(sprint.get("status") or "").lower() in closed_statuses:
            continue
        for item in sprint.get("items") or []:
            ref = sprint_item_slug(item)
            parsed = parse_plan_ref(ref)
            target_project = (
                str(parsed.project) if parsed and parsed.project else sprint_project
            )
            if parsed is not None and target_project == owning_project:
                membership[parsed.slug].append(sprint_ref)
            elif ref:
                membership[ref].append(sprint_ref)
    return dict(membership), order


def _sprints_reachable_from_plans(
    project: str,
    sprints: list[dict[str, Any]],
    plans: Mapping[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve plan-declared sprint refs and retain each reachable resource."""

    local_by_id = {
        str(sprint.get("id") or ""): sprint for sprint in sprints if sprint.get("id")
    }
    mounts = _load_mounts()
    external_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def lookup(target_project: str, sprint_id: str) -> Mapping[str, Any] | None:
        if target_project == project:
            return local_by_id.get(sprint_id)
        key = (target_project, sprint_id)
        if key not in external_cache:
            docs_dir = mounts.get(target_project)
            if docs_dir:
                try:
                    external_cache[key] = read_sprint_record(
                        docs_dir, target_project, sprint_id
                    )
                except (OSError, ValueError):
                    external_cache[key] = {}
            else:
                external_cache[key] = {}
        return external_cache[key]

    effective = [
        {**sprint, "_ref": str(sprint.get("id") or ""), "_project": project}
        for sprint in sprints
    ]
    declarations: dict[str, dict[str, Any]] = {}
    added_refs = {str(sprint.get("_ref") or "") for sprint in effective}
    for slug, plan in plans.items():
        ref = str(plan.get("sprint") or "")
        if not ref:
            continue
        row = resolve_plan_ref(ref, project, lookup)
        declarations[slug] = row
        parsed = parse_plan_ref(ref)
        if parsed is None or not parsed.is_external(project) or ref in added_refs:
            continue
        sprint = lookup(str(parsed.project), parsed.slug)
        if sprint:
            effective.append({**sprint, "_ref": ref, "_project": str(parsed.project)})
        else:
            effective.append(
                {
                    "id": parsed.slug,
                    "status": "unresolved",
                    "items": [],
                    "_ref": ref,
                    "_project": str(parsed.project),
                    "_unresolved": True,
                }
            )
        added_refs.add(ref)
    return effective, declarations


def _resolved_sprint_items(
    project: str,
    sprints: list[dict[str, Any]],
    local_plans: Mapping[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Resolve authored sprint membership without changing its order."""

    mounts = _load_mounts()
    external_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def lookup(target_project: str, slug: str) -> Mapping[str, Any] | None:
        if target_project == project:
            return local_plans.get(slug)
        key = (target_project, slug)
        if key not in external_cache:
            docs_dir = mounts.get(target_project)
            external_cache[key] = (
                read_plan_record(docs_dir, target_project, slug) if docs_dir else {}
            )
        return external_cache[key]

    return {
        str(sprint.get("_ref") or sprint.get("id") or ""): [
            resolve_plan_ref(
                sprint_item_slug(item), str(sprint.get("_project") or project), lookup
            )
            for item in sprint.get("items") or []
            if sprint_item_slug(item)
        ]
        for sprint in sprints
        if sprint.get("id")
    }


def _open_sprints(
    sprints: list[dict[str, Any]],
    plans: dict[str, dict[str, Any]],
    resolved_items: Mapping[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """Return ordered sprints that still contain executable work."""

    result: list[str] = []
    for sprint in sprints:
        sprint_id = str(sprint.get("id") or "")
        sprint_ref = str(sprint.get("_ref") or sprint_id)
        status = str(sprint.get("status") or "").lower()
        if not sprint_id or status in TERMINAL_STATUSES:
            continue
        if resolved_items is not None:
            members = resolved_items.get(sprint_ref, [])
            if any(
                not member.get("found")
                or str(member.get("status") or "") not in TERMINAL_STATUSES
                for member in members
            ):
                result.append(sprint_ref)
            continue
        item_plans = [
            plans[slug]
            for item in sprint.get("items") or []
            if (slug := sprint_item_slug(item)) in plans
        ]
        if any(_status(plan) not in TERMINAL_STATUSES for plan in item_plans):
            result.append(sprint_ref)
    return result


# The stored statuses that make a sprint finished for the summary list. This is
# deliberately narrower than ``CLOSED_SPRINT_STATUSES``, which also counts
# ``closed`` and ``archived`` as statuses nothing will schedule from again: the
# summary lists every sprint whose stored status is not ``done`` or ``shipped``,
# so a ``closed`` or ``archived`` sprint is still in the open bucket rather than
# being dropped or filed as recently closed. Its own name, because one module
# must not bind one name twice and the later binding would silently win.
SPRINT_FINISHED_STATUSES = frozenset({"done", "shipped"})


def _sprint_closed_at(sprint: Mapping[str, Any]) -> datetime | None:
    """The instant a closed sprint closed, or None when no readable date exists.

    The close is read from the sprint's own record. ``closed_at`` is the field a
    close writes; ``ends`` is the declared end a sprint may carry instead, and is
    accepted only as a fallback because it is a plan-like date rather than an
    observation. A sprint with neither cannot prove its close is recent, so it
    is left out of the window rather than guessed into it.
    """
    for key in ("closed_at", "ends"):
        raw = sprint.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed = parse_utc(raw.strip())
            if parsed is not None:
                return parsed
    return None


def _sprint_member_counts(members: list[Mapping[str, Any]]) -> tuple[int, int, float]:
    """Member count, pending count and completion percentage for one sprint.

    A member is a plan the sprint names; a pending member is one that has not
    reached a terminal status, including a member that did not resolve — a ref
    that names no plan is work nothing has finished. The percentage is the share
    of members whose status is terminal, so a sprint with no members reads 0.0
    rather than a division on an empty set.
    """
    members_count = len(members)
    pending = sum(
        1
        for member in members
        if not member.get("found")
        or str(member.get("status") or "") not in TERMINAL_STATUSES
    )
    if not members_count:
        return 0, 0, 0.0
    return (
        members_count,
        pending,
        round(100 * (members_count - pending) / members_count, 1),
    )


def _sprint_recent_days(project: str, docs_dir: str | Path | None) -> int:
    """The configured recently-closed window for ``project``, or the default.

    The window is read from the project's resolved flight config so a host or
    project layer can retune it. A project whose config cannot be resolved — no
    mount, no layer, a malformed layer — falls back to the shipped default
    rather than failing a summary read on routing configuration.
    """
    from reckon import flight

    if docs_dir is None:
        return flight.DEFAULT_SPRINT_RECENT_DAYS
    try:
        config = flight.resolve(project, checkout_path=Path(docs_dir).parent).config
    except (flight.FlightConfigError, OSError):
        return flight.DEFAULT_SPRINT_RECENT_DAYS
    return flight.sprint_recent_days(config)


def sprint_summary_rows(
    project: str,
    sprints: list[dict[str, Any]],
    plans: Mapping[str, dict[str, Any]],
    *,
    recent_days: int,
    docs_dir: str | Path | None = None,
    live_records: Iterable[Mapping[str, Any]] | None = None,
    liveness: Mapping[str, Mapping[str, Any]] | None = None,
    resolved_items: Mapping[str, list[dict[str, Any]]] | None = None,
    moment: datetime | None = None,
) -> list[dict[str, Any]]:
    """The compact sprint list a discovery or roadmap summary carries.

    One row per sprint a reader needs to see: every sprint a crew is working
    now, every sprint whose stored status is not ``done`` or ``shipped``, and
    every sprint closed within ``recent_days``. Rows are ordered live first,
    then open, then recently closed.

    Liveness is shared with the fleet pane: the same derived read, never a stored
    marker. ``liveness`` and ``resolved_items`` may be passed in when the caller
    has already derived them — the roadmap does — so one read serves every
    surface; when they are omitted the function derives them itself from
    ``docs_dir``. Nothing here writes.
    """
    moment = moment or datetime.now(UTC)
    if liveness is None:
        liveness = (
            sprint_liveness(project, docs_dir, live_records)
            if docs_dir is not None
            else {}
        )
    if resolved_items is None:
        resolved_items = _resolved_sprint_items(project, sprints, plans)

    rows: list[dict[str, Any]] = []
    for sprint in sprints:
        sprint_id = str(sprint.get("id") or "")
        if not sprint_id:
            continue
        sprint_ref = str(sprint.get("_ref") or sprint_id)
        status = str(sprint.get("status") or "").lower()
        live_row = liveness.get(sprint_id) or liveness.get(sprint_ref) or {}
        live = bool(live_row.get("live"))
        closed_at = (
            _sprint_closed_at(sprint) if status in SPRINT_FINISHED_STATUSES else None
        )
        if not live and status not in SPRINT_FINISHED_STATUSES:
            include = True
        elif not live and status in SPRINT_FINISHED_STATUSES:
            include = closed_at is not None and moment - closed_at <= timedelta(
                days=max(0, recent_days)
            )
        else:
            include = True
        if not include:
            continue

        members = list(resolved_items.get(sprint_ref, []))
        members_count, pending, completion_pct = _sprint_member_counts(members)
        rows.append(
            {
                "id": sprint_id,
                "theme": str(sprint.get("theme") or ""),
                "status": status,
                "live": live,
                "live_runs": list(live_row.get("live_runs") or []),
                "live_sessions": list(live_row.get("live_sessions") or []),
                "last_activity_at": live_row.get("last_activity_at"),
                "members": members_count,
                "pending": pending,
                "completion_pct": completion_pct,
                "closed_at": closed_at.isoformat() if closed_at is not None else None,
            }
        )

    def order(row: dict[str, Any]) -> tuple[int, str]:
        if row["live"]:
            bucket = 0
        elif row["status"] in SPRINT_FINISHED_STATUSES:
            bucket = 2
        else:
            bucket = 1
        return (bucket, row["id"])

    rows.sort(key=order)
    return rows


def _schedule_horizon(project_manifest: dict[str, Any] | None) -> int | None:

    value = (project_manifest or {}).get("schedule_horizon_sprints")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _schedule_bars(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The bar list, each bar naming its plan by typed key rather than by row.

    The schedule travels inside every discovery payload and every fleet read,
    so an embedded plan row — sections, decisions, follow-ups and gates — is
    downloaded again for each bar that references it. The reader already holds
    the inventory the key resolves against, so a reference is what the bar
    owes it; embedding the row multiplies the payload by the row's size. The
    key is the plan's ``nav_key``, so a plan and another kind of document
    sharing a slug cannot resolve to one another's row.
    """

    return [
        {
            "plan": item["slug"],
            "nav_key": item["nav_key"],
            "state": item["state"],
            "start": item["start"],
            "split": item["split"],
            "end": item["end"],
            "wall_hours": item["wall_hours"],
            "lane": item["lane"],
        }
        for item in items
    ]


def schedule_report(
    project: str,
    inventory: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    reference: datetime,
) -> dict[str, Any]:
    """Compact schedule read for roadmap consumers: the chain's far end and
    the realisable lane width, with the full bar list beneath.

    ``reference`` is required and never the wall clock, so a caller asking
    "when does this chain finish" and the surface drawing it agree on the same
    instant. Far end is the high axis bound the surface's chain figure reads;
    the latest actual bar end is reported separately.
    """

    schedule = derive_schedule(inventory, runs, project, reference)
    return {
        "reference": reference.isoformat(),
        "far_end_hours": schedule["high"],
        "latest_end_hours": schedule["latest_end"],
        "lane_count": len(schedule["lanes"]),
        "item_count": len(schedule["items"]),
        "low": schedule["low"],
        "high": schedule["high"],
        "bars": _schedule_bars(schedule["items"]),
    }


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
) -> list[str]:
    """Return the slugs in scope, in a stable slug order.

    The roadmap report visits its plans in the order this returns, appending
    decision-blocker and wiring rows as it goes. A set here made that order
    depend on the process hash seed, so two reads of an unchanged project
    emitted the same rows in different orders. Sorting by slug derives the
    order from the plan identity instead.
    """

    if not sprint_id:
        return sorted(plans)
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
    return sorted(selected)


# Sprint statuses that mean the sprint will not schedule anything again. A plan
# declaring one of these is not merely mis-filed: nothing will pick it up.
CLOSED_SPRINT_STATUSES = frozenset({"done", "shipped", "closed", "archived"})


def _derived_sprint_state(members: list[dict[str, Any]]) -> str:
    """Return lifecycle state from the resolved members that actually exist."""

    found = [member for member in members if member.get("found")]
    if not found:
        return "empty"
    if all(_progress(member) >= 1.0 for member in found):
        return "shipped"
    if any(
        _progress(member) > 0.0 or str(member.get("status") or "") == "active"
        for member in found
    ):
        return "in-progress"
    return "planned"


# A sprint's status is a scheduling fact, and "in-progress" describes the work
# inside it, so a sprint whose members have started does not contradict a status
# of "open" (work remains, not the pushed sprint) or "active" (pushed). It does
# contradict "planned" — that status says the work has not started, and a
# started member is precisely what reports drift for.
_DRIFT_MEMBER_PROGRESS_STORED = frozenset({"open", "active"})


def _drift_is_member_progress(stored: str, derived: str) -> bool:
    """Return True when a member-derived label does not contradict the stored one."""
    return derived == "in-progress" and stored.lower() in _DRIFT_MEMBER_PROGRESS_STORED


def _sprint_status_buckets(
    sprints: list[dict[str, Any]],
    declared_active: str | None,
    liveness: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str | None, list[str]]:
    """Derive the pushed sprint and the open-sprint bucket from sprint status.

    The sprint a live crew is working wins the focus, most recent stream
    activity first; with no live crew the stored status decides, and where
    several sprints are stored ``active`` the deterministic pick documented on
    :func:`reckon.project_state.focus_sprint_id` applies. The declared value
    survives only when neither settles it. ``open`` sprints — work remains and
    this is not the one being pushed — are returned as their own bucket.
    """
    focused = focus_sprint_id(sprints, liveness)
    open_ids = [
        str(sprint.get("id") or "")
        for sprint in sprints
        if str(sprint.get("status") or "").lower() == "open"
    ]
    return (focused or declared_active), open_ids


def plans_released_by_cleared_blockers(
    docs_dir: Path | None,
    project: str,
    sprints: Iterable[Mapping[str, Any]],
) -> set[str]:
    """Plan slugs whose every recorded blocker is a cleared blocker resource.

    Composition drops a cleared blocker from the sprint item that named it, so
    a plan whose hold has cleared reads exactly like a plan that never carried
    one. The raw sprint record is the one place the hold and its clear stay
    attached to the plan, so the release is read from there; a blocker whose
    resource is missing or still holds keeps its plan out of the set.

    Both surfaces that read a plan's blocked state call this, so a released
    plan reads released on each rather than one of them synthesizing a
    placeholder blocker for a hold a probe already cleared.
    """

    if docs_dir is None:
        return set()
    recorded: dict[str, set[str]] = defaultdict(set)
    for sprint in sprints:
        if sprint.get("_project") not in (None, project) or sprint.get("_unresolved"):
            continue
        sprint_id = str(sprint.get("id") or "")
        if not sprint_id:
            continue
        try:
            raw, _version = read_resource(docs_dir, project, "sprint", sprint_id)
        except (OSError, ProjectStateError, ValueError):
            continue
        for item in raw.get("items") or []:
            if not isinstance(item, dict) or not item.get("slug"):
                continue
            recorded[str(item["slug"])].update(
                str(blocker_id) for blocker_id in item.get("blocked_by") or []
            )
    released: set[str] = set()
    for slug, blocker_ids in recorded.items():
        if not blocker_ids:
            continue
        for blocker_id in blocker_ids:
            try:
                blocker, _version = read_resource(
                    docs_dir, project, "blocker", blocker_id
                )
            except (OSError, ProjectStateError, ValueError):
                break
            if (
                blocker.get("status") != "cleared"
                or not str(blocker.get("cleared_reason") or "").strip()
            ):
                break
        else:
            released.add(slug)
    return released


def _blocked_section_run_seconds(
    project: str, pointers: Iterable[Mapping[str, Any]]
) -> dict[tuple[str, str], float]:
    """The age of each section's oldest run whose manifest reports it blocked.

    A blocked run's age bounds how long the section has been in that state, and
    the section it was dispatched on is what a ranked row needs. A pointer
    naming no section contributes nothing, and where a section carries several
    blocked runs the oldest is the one that bounds it.
    """

    from reckon.crew.recovery import classify_pointer

    recorded = recorded_live_run_classifications(project)
    now = datetime.now(UTC)
    seconds: dict[tuple[str, str], float] = {}
    for pointer in pointers:
        if not isinstance(pointer, Mapping) or pointer.get("project") != project:
            continue
        node = pointer.get("node")
        node = node if isinstance(node, Mapping) else {}
        plan_ref = str(node.get("plan") or "")
        section_ref = str(node.get("section") or "")
        if not plan_ref or not section_ref:
            continue
        classified = run_classification_record(
            pointer, classify=classify_pointer, recorded=recorded
        )
        if run_classification(classified) != BLOCKED_RUN_CLASSIFICATION:
            continue
        started = parse_utc(pointer.get("created_at"))
        if started is None:
            continue
        age = max(0.0, (now - started).total_seconds())
        key = (plan_ref, section_ref)
        seconds[key] = max(seconds.get(key, 0.0), age)
    return seconds


def _read_scope_claims(
    project: str,
    docs_dir: Path | None,
    manifest: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """The scope registry's claims, read once for every holder question.

    The claims come from the same registry the scopes view reads, so a path
    reported from them is one the dispatcher would refuse a second writer.
    A checkout with no measurable repository, or a registry whose reader refuses
    the call, reads as no registry rather than as a guess: a roadmap that cannot
    read the claims still owes its reader the rest of the page.
    """

    if docs_dir is None:
        return []
    from reckon.crew import runs

    repo_root = Path(docs_dir).parent
    try:
        return list(
            runs.scope_claims(
                project,
                repo_root,
                derivations=(manifest or {}).get("derivations") or {},
            )
        )
    except (OSError, TypeError):
        return []


def _held_path_blockers_by_section(
    claims: Iterable[Mapping[str, Any]],
    pointers: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The held paths of each waiting section, keyed by its plan and section.

    The blocker lands on the waiting run's section — the row that has to say why
    work on it has not started.

    Which runs are queued is read from the pointers, because the registry
    records a claim's binding but not the condition that stopped it binding, and
    only a queued claim waits on the path it names.
    """

    placement: dict[str, tuple[str, str]] = {}
    queued_runs: set[str] = set()
    for pointer in pointers:
        if not isinstance(pointer, Mapping):
            continue
        node = pointer.get("node")
        node = node if isinstance(node, Mapping) else {}
        plan_ref = str(node.get("plan") or "")
        run_section = str(node.get("section") or "")
        run_id = str(pointer.get("run_id") or "")
        if str(pointer.get("phase") or "") == "queued":
            queued_runs.add(run_id)
        if plan_ref and run_section:
            placement[run_id] = (plan_ref, run_section)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for blocker in held_path_blockers(claims, queued_runs=queued_runs):
        key = placement.get(str(blocker.get("waiting") or ""))
        if key is not None:
            grouped[key].append(blocker)
    return dict(grouped)


def _held_paths_by_section(
    claims: Iterable[Mapping[str, Any]],
    section_runs: Mapping[tuple[str, str], Iterable[Mapping[str, Any]]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The paths each section's own runs hold, keyed by its plan and section.

    A section reports what the work on it has fenced, and one run holding a path
    is the whole of that fact for the section: nothing has to be waiting on the
    path for the run to hold it. The waiting-section blockers above cannot say
    so, because they are a pair, so a section whose single running run holds a
    path would otherwise report no path at all.

    Read from the same claims those blockers are read from, so a path reported
    here is one the dispatcher would refuse a second writer.
    """

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, rows in section_runs.items():
        held = held_path_rows(
            claims, run_ids=[str(row.get("run_id") or "") for row in rows]
        )
        if held:
            grouped[key] = held
    return grouped


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
    docs_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return pending work, execution paths, progress, blockers, and wiring findings.

    ``docs_dir`` names the checkout ``inventory`` was read from; it is passed
    through to the wiring scan so a worktree-scoped roadmap reads standalone
    declarations from the same tree as the rows it judges. Absent, the wiring
    scan falls back to the project's registered mount.

    The pass is read-only, so every slug it resolves shares one scan of each
    docs tree rather than rescanning the tree per plan.
    """

    with resource_scan_scope():
        return _build_roadmap(
            project,
            inventory,
            sprints,
            active_sprint_id=active_sprint_id,
            sprint_id=sprint_id,
            max_paths=max_paths,
            project_manifest=project_manifest,
            review=review,
            docs_dir=docs_dir,
        )


def _build_roadmap(
    project: str,
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    *,
    active_sprint_id: str | None,
    sprint_id: str | None,
    max_paths: int,
    project_manifest: dict[str, Any] | None,
    review: dict[str, Any] | None,
    docs_dir: Path | str | None,
) -> dict[str, Any]:

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

    sprints, declared_sprint_rows = _sprints_reachable_from_plans(
        project, sprints, all_plans
    )
    membership, sprint_order = _sprint_membership(sprints, project)
    resolved_docs = (
        Path(docs_dir) if docs_dir is not None else _load_mounts().get(project)
    )
    liveness = (
        sprint_liveness(project, resolved_docs) if resolved_docs is not None else {}
    )
    released_by_clear = plans_released_by_cleared_blockers(
        resolved_docs, project, sprints
    )
    active_sprint_id, open_sprint_ids = _sprint_status_buckets(
        sprints, active_sprint_id, liveness
    )
    resolved_sprint_items = _resolved_sprint_items(project, sprints, all_plans)
    open_sprints = _open_sprints(sprints, all_plans, resolved_sprint_items)
    schedule_horizon = _schedule_horizon(project_manifest)
    schedule_ready_sprints = (
        open_sprints[:schedule_horizon]
        if schedule_horizon is not None
        else open_sprints
    )
    schedule_boundary = schedule_ready_sprints[-1] if schedule_ready_sprints else None
    selected_slugs = _scope_slugs(all_plans, membership, sprint_id)
    plans = {slug: all_plans[slug] for slug in selected_slugs}
    from reckon import crew
    from reckon._plan_html import section_record_id
    from reckon.crew import runs

    pointers = crew.list_live()
    live_runs, interrupted_runs = partition_live_runs(project, pointers)
    # Work in flight sits on its section: the rows the plan-level partition
    # already classified are grouped under the section each run names, so a
    # section row reports its runs with the one classification rather than
    # reading the pointers again.
    section_runs = section_run_rows(
        {
            plan_ref: [
                *live_runs.get(plan_ref, []),
                *interrupted_runs.get(plan_ref, []),
            ]
            for plan_ref in {*live_runs, *interrupted_runs}
        }
    )
    # One read of the scope registry answers both holder questions — which
    # waiting section is stopped by a path, and which paths each section's own
    # runs hold — so a claim cannot hold a path for one row and be missing from
    # the other.
    scope_claims = _read_scope_claims(project, resolved_docs, project_manifest)
    held_path_blockers_by_section = _held_path_blockers_by_section(
        scope_claims, pointers
    )
    held_paths_by_section = _held_paths_by_section(scope_claims, section_runs)
    attempts_by_plan = section_attempts_by_plan(
        project,
        root=resolved_docs.parent if resolved_docs is not None else None,
        pointers=runs._list_live_records(project=project),
    )
    findings: list[dict[str, Any]] = []
    dependency_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    judgment_required_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    after_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    local_graph: dict[str, list[str]] = defaultdict(list)
    dependents: dict[str, set[str]] = defaultdict(set)
    # The hours of each section node on the graph, so a path that runs through
    # sections is priced by the section the work sits in.
    section_effort: dict[str, float] = {}
    # The section an edge waits on, when the dependency names one. A plan-level
    # edge carries no entry, so a reader sees the plan name as before.
    edge_meta: dict[tuple[str, str], dict[str, str]] = {}
    # The ready sections each plan holds, enumerated once as the plan row is
    # derived, and the age of a run on a section that reports itself blocked.
    # Both feed the ranked section rows below.
    ready_sections_by_plan: dict[str, list[str]] = {}
    # Each plan's named holds, by section: the blocker kinds a section row
    # reports beside the runs sitting on it.
    section_holds_by_plan: dict[str, dict[str, list[dict[str, Any]]]] = {}
    authored_by_plan: dict[str, Mapping[str, Any]] = {}
    # The sections each open plan still declares implementable, and the refs
    # each of its sections declares it waits on. The first scopes the wiring
    # findings to work that is still open, the second is what an undeclared
    # wait is judged against.
    open_sections_by_plan: dict[str, set[str]] = {}
    declared_section_refs: dict[tuple[str, str], set[str]] = {}
    blocked_run_seconds = _blocked_section_run_seconds(project, pointers)
    north_stars = [
        dict(item)
        for item in (project_manifest or {}).get("north_stars", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    declared_north_stars = {str(item["id"]).strip() for item in north_stars}

    for membership_sprint_id, members in resolved_sprint_items.items():
        for member in members:
            parsed = parse_plan_ref(str(member.get("ref") or ""))
            if (
                parsed is not None
                and parsed.project is not None
                and not member.get("found")
            ):
                findings.append(
                    _finding(
                        "unresolved-sprint-item",
                        "warn",
                        (
                            f"sprint {membership_sprint_id}: qualified item "
                            f"{member['ref']!r} does not resolve"
                        ),
                        slug=str(member.get("slug") or ""),
                        extra={
                            "sprint": membership_sprint_id,
                            "ref": member["ref"],
                        },
                    )
                )

    for slug, sprint_row in declared_sprint_rows.items():
        if sprint_row.get("scope") == "external" and not sprint_row.get("found"):
            findings.append(
                _finding(
                    "unresolved-plan-sprint",
                    "warn",
                    (
                        f"{slug}: qualified sprint {sprint_row['ref']!r} "
                        "does not resolve"
                    ),
                    slug=slug,
                    extra={"ref": sprint_row["ref"]},
                )
            )

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
        # A plan whose recorded hold has cleared is released, not orphaned: its
        # blocker resource exists and carries the clear, so the status has an
        # explanation even though no live blocker remains.
        if (
            status == "blocked"
            and not (plan.get("blocking") or [])
            and slug not in released_by_clear
        ):
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
            target_view = _plan_authored_state(target, docs_dir, project, parsed.slug)
            target_sections = declared_section_identities(target_view)
            section_found = not parsed.stage or parsed.stage in target_sections
            if parsed.stage:
                satisfied = section_found and _section_satisfied(
                    target,
                    parsed.stage,
                    target_view.get("section_declarations"),
                )
            else:
                satisfied, judgment = _whole_plan_dependency(
                    target, docs_dir, project, parsed.slug
                )
                if judgment is not None:
                    judgment_required_rows[slug].append(
                        _judgment_member(project, slug, project, parsed.slug, judgment)
                    )
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
            if not satisfied and parsed.slug in plans:
                local_graph[slug].append(parsed.slug)
                dependents[parsed.slug].add(slug)
                if parsed.stage:
                    edge_meta[(slug, parsed.slug)] = {
                        "ref": ref,
                        "section": parsed.stage,
                        "gate": "",
                    }
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

        section_mapping = _plan_section_deps(plan, docs_dir, project, slug)
        if section_mapping:
            section_edge_rows, section_edge_findings = _section_scoped_edges(
                project, plan, slug, section_mapping, all_plans, docs_dir
            )
            dependency_rows[slug].extend(section_edge_rows)
            findings.extend(section_edge_findings)
            _lift_section_edges(
                section_edge_rows,
                slug,
                plans=plans,
                local_graph=local_graph,
                dependents=dependents,
                section_effort=section_effort,
                edge_meta=edge_meta,
                authored=lambda target: _plan_authored_state(
                    all_plans[target], docs_dir, project, target
                ),
                waiting_authored=_plan_authored_state(plan, docs_dir, project, slug),
            )

        gate_edge_rows = _gate_section_edges(project, plan, slug, all_plans, docs_dir)
        if gate_edge_rows:
            dependency_rows[slug].extend(gate_edge_rows)
            for edge_row in gate_edge_rows:
                target_slug = str(edge_row.get("slug") or "")
                if (
                    edge_row.get("scope") == "local"
                    and edge_row.get("found")
                    and not edge_row.get("satisfied")
                    and target_slug
                    and target_slug != slug
                    and target_slug in plans
                ):
                    local_graph[slug].append(target_slug)
                    dependents[target_slug].add(slug)
                    edge_meta[(slug, target_slug)] = {
                        "ref": str(edge_row.get("ref") or target_slug),
                        "section": str(edge_row.get("stage") or ""),
                        "gate": str(edge_row.get("gate") or ""),
                    }
            for edge_row in gate_edge_rows:
                member = edge_row.pop("judgment", None)
                if member is not None:
                    judgment_required_rows[slug].append(member)

        # A decision scoped to a section the plan never declares holds nothing
        # and can never be satisfied, so it is reported rather than silently
        # rendered as a frozen wait. The declared set is read from the same
        # source the write boundary refuses against, so one scope has one
        # answer on both surfaces.
        decision_mapping = _plan_decision_sections(plan, docs_dir, project, slug)
        declared_sections = declared_section_identities(
            _plan_authored_state(plan, docs_dir, project, slug)
        )
        findings.extend(
            _finding(
                refusal["code"],
                "error",
                (
                    f"{slug}: decision {refusal['decision']!r} is scoped to "
                    f"section {refusal['section']!r}, which this plan does "
                    "not declare"
                ),
                slug=slug,
                extra={
                    "decision": refusal["decision"],
                    "section": refusal["section"],
                },
            )
            for refusal in decision_section_refusals(
                decision_mapping, declared_sections
            )
        )

        # Soft sequencing: resolved here, ranked below, and deliberately kept
        # out of every blocker list — an after target that has not shipped
        # orders the plan later without ever holding it.
        after_rows[slug] = _after_edges(project, plan, all_plans, artifacts, docs_dir)
        for after_row in after_rows[slug]:
            member = after_row.pop("judgment", None)
            if member is not None:
                judgment_required_rows[slug].append(member)
        for row in after_rows[slug]:
            if row.get("found"):
                continue
            findings.append(
                _finding(
                    "unresolved-after-edge",
                    "warn",
                    (
                        f"{slug}: after edge {row['ref']!r} resolves to no live "
                        "plan — the edge orders nothing and holds nothing"
                    ),
                    slug=slug,
                    extra={"ref": row["ref"]},
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
        declared_sprint_row = declared_sprint_rows.get(slug)
        assigned_sprints = membership.get(slug, [])
        if (
            declared_sprint
            and declared_sprint not in assigned_sprints
            and not (
                declared_sprint_row
                and declared_sprint_row.get("scope") == "external"
                and not declared_sprint_row.get("found")
            )
        ):
            # Two failures wore one badge, and the worse one was the quieter.
            # A plan absent from an OPEN sprint's items is untidy. A plan
            # declaring a sprint that has CLOSED is invisible to the advancing
            # horizon — the work will never be scheduled — and a reader who
            # meets it as one `warn` among others has no way to tell which they
            # are looking at. Measured: a session authored two plans into a
            # closed sprint, was told twice, and walked past both times.
            declared_status = str(
                next(
                    (
                        sprint.get("status")
                        for sprint in sprints
                        if str(sprint.get("_ref") or sprint.get("id") or "")
                        == declared_sprint
                    ),
                    "",
                )
            ).lower()
            if declared_status in CLOSED_SPRINT_STATUSES:
                findings.append(
                    _finding(
                        "plan-sprint-closed",
                        "error",
                        (
                            f"{slug}: declares sprint {declared_sprint}, which is "
                            f"{declared_status} — this work is invisible to the "
                            "advancing horizon until it moves to an open sprint"
                        ),
                        slug=slug,
                        extra={"sprint": declared_sprint, "status": declared_status},
                    )
                )
            else:
                findings.append(
                    _finding(
                        "plan-sprint-missing-item",
                        "warn",
                        (
                            f"{slug}: declares sprint {declared_sprint} but is "
                            "absent from its items"
                        ),
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

    endpoint_rows = _dependency_endpoints(project, plans)
    endpoint_memberships: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for endpoint_row in endpoint_rows:
        closure = {
            "project": endpoint_row["project"],
            "slug": endpoint_row["slug"],
            "ref": endpoint_row["ref"],
            "handle": endpoint_row["handle"],
            "addressable": endpoint_row["handle"] is not None,
        }
        for member in endpoint_row["members"]:
            endpoint_memberships[member["slug"]].append(dict(closure))
    for endpoint_row in endpoint_rows:
        if endpoint_row["handle"] is not None:
            continue
        live_descendants = [
            member
            for member in endpoint_row["members"]
            if member["slug"] != endpoint_row["slug"]
            and member["status"] not in TERMINAL_STATUSES
        ]
        if not live_descendants:
            continue
        member_slugs = [member["slug"] for member in endpoint_row["members"]]
        findings.append(
            _finding(
                "endpoint-without-handle",
                "warn",
                (
                    f"{endpoint_row['slug']}: structural endpoint carries no graph "
                    f"handle; its {len(member_slugs)}-member closure is not "
                    "addressable as a ship target"
                ),
                slug=endpoint_row["slug"],
                extra={"members": member_slugs},
            )
        )

    pending: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    schedule_deferred: list[dict[str, Any]] = []
    decision_blockers_report: list[dict[str, Any]] = []
    waiting_decisions_report: list[dict[str, Any]] = []
    open_decisions_report: list[dict[str, Any]] = []
    deferred_decisions_report: list[dict[str, Any]] = []
    unauthorised: list[dict[str, Any]] = []
    closable_slugs: set[str] = set()
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
        authored = _plan_authored_state(plan, docs_dir, project, slug)
        decision_sections_map = _plan_decision_sections(plan, docs_dir, project, slug)
        # One assembly of this row's blockers and blocked state, shared with the
        # served view: a held path, an unpassed gate and an unscoped open
        # decision each hold the plan on both surfaces, from the same rows.
        blocker_state = plan_blocker_state(
            authored,
            dependency_blockers=plan_dependency_blockers,
            blockers=plan.get("blocking") or [],
            sections_map=decision_sections_map,
            released=slug in released_by_clear,
        )
        explicit_blockers = blocker_state["explicit_blockers"]
        held_blockers = blocker_state["held_blockers"]
        gate_blockers = blocker_state["gate_blockers"]
        section_gate_blockers = blocker_state["section_gate_blockers"]
        decision_blockers = blocker_state["decision_blockers"]
        deferred_decisions = blocker_state["deferred_decisions"]
        decisions = _decision_rows(plan, decision_sections_map)
        open_decisions = [
            decision for decision in decisions if decision["status"] == "open"
        ]
        # The rows a reader of the wait needs, carrying what the readiness rows
        # do not. The surfaces that list a wait read this list rather than the
        # readiness rows, so a decision waiting on the lead is visible without
        # opening the plan.
        waiting_decisions = unsettled_decisions(
            plan, decision_sections_map, waiting_context=True
        )
        dispatchable, missing_dispatchability = _dispatchability(plan)
        # A soft after edge whose target is observed open ranks the plan later
        # without removing it from the ready set; an unobserved target (external
        # or unresolvable) ranks nothing.
        after_hold = any(
            record.get("satisfied") is False for record in after_rows.get(slug, [])
        )
        # A legacy persisted blocked status is the open-state spelling for work
        # whose blockers are all cleared, so a released plan is authorised like
        # any other open work.
        authorised = status in _AUTHORISED_STATUSES or slug in released_by_clear
        # A plan whose declarations admit no implementable work has nothing to
        # dispatch, so it leaves the ready set, the blocked set and the critical
        # path whichever blockers it still carries. What stands between it and
        # its close is enumerated by ``closure_blockers`` instead.
        closable = blocker_state["closable"]
        is_ready = (
            dispatchable
            and authorised
            and not blocker_state["blocking"]
            and slug not in cycle_members
            and not closable
        )
        # A dependency cycle is the one hold a single plan row cannot see, so it
        # joins the assembled blockers here rather than in the assembly.
        is_blocked = blocker_state["blocked"] or (
            slug in cycle_members and not closable
        )
        readiness = (
            "closable"
            if closable
            else "ready"
            if is_ready
            else "blocked"
            if is_blocked
            else "deferred"
        )
        if closable:
            closable_slugs.add(slug)
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
        # A plan's implementable sections are enumerated once per read; the row
        # both reports them and counts their attempts with.
        plan_implementable = implementable_sections(
            authored.get("section_declarations")
        )
        row = {
            "slug": slug,
            "title": plan.get("title") or slug,
            "status": status,
            "dispatchable": dispatchable,
            "missing_dispatchability": missing_dispatchability,
            "authorised": authorised,
            "effective_status": blocker_state["effective_status"],
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
            "after": after_rows.get(slug, []),
            "came_after": [
                after_row["ref"]
                for after_row in after_rows.get(slug, [])
                if after_row.get("found") and after_row.get("satisfied") is not True
            ],
            "after_hold": after_hold,
            "explicit_blockers": explicit_blockers,
            "held_blockers": held_blockers,
            "gate_blockers": gate_blockers,
            "section_gate_blockers": section_gate_blockers,
            "closure_blockers": closure_blockers(authored),
            "decision_blockers": decision_blockers,
            "deferred_decisions": deferred_decisions,
            "decisions": decisions,
            "unlocks": sorted(dependents.get(slug, set())),
            "ready": is_ready,
            "readiness": readiness,
            "implementable_sections": plan_implementable,
            "section_attempts": {
                section: attempts_by_plan.get(slug, {})
                .get(section_record_id(section), {})
                .get("attempts", 0)
                for section in plan_implementable
            },
            "dependency_ready": is_ready,
            "dependency_readiness": readiness,
            "schedule_ready": not is_schedule_deferred,
            "schedule_readiness": "deferred" if is_schedule_deferred else "ready",
            "schedule_deferred_reason": schedule_reason,
            "schedule_behind_sprint": schedule_boundary
            if is_schedule_deferred
            else None,
            "judgment_required": {
                "required": bool(judgment_required_rows.get(slug)),
                "count": len(judgment_required_rows.get(slug, [])),
                "members": list(judgment_required_rows.get(slug, [])),
            },
        }
        mapped_edges = [
            row for row in dependency_rows.get(slug, []) if row.get("source_section")
        ]
        # The refs each of this plan's sections declares it waits on, and the
        # sections it still holds open. Openness is the declaration's, read
        # through the one open-section predicate: a run pointer left on a
        # landed section is a record of work, not work still in flight.
        for edge in mapped_edges:
            waiting_section = str(edge.get("source_section") or "")
            if waiting_section:
                declared_section_refs.setdefault((slug, waiting_section), set()).add(
                    _normalized_section_ref(project, str(edge.get("ref") or ""))
                )
        open_sections_by_plan[slug] = set(open_sections(authored))
        # A decision that names the sections it governs holds those sections
        # rather than the plan: the row keeps the decision in its reported
        # blockers, and the sections it names carry the wait in row form so a
        # reader sees why the section is not dispatchable.
        decision_section_blockers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for decision in decisions:
            if decision["status"] != "open":
                continue
            for section in decision.get("sections") or []:
                decision_section_blockers[section].append(decision)
        if section_dependency_blockers or mapped_edges or decision_section_blockers:
            # A staging anchor holds the source plan's section of the same
            # identity; the mapping names its waiting section outright, so every
            # section-scoped edge keys its blockers by the section that waits.
            section_blockers: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for blocker in section_dependency_blockers:
                waiting = str(blocker.get("source_section") or blocker.get("stage"))
                section_blockers[waiting].append(blocker)
            for section, holding in decision_section_blockers.items():
                section_blockers[section].extend(holding)
            # The readiness split enumerates a plan's sections from the same
            # authored set every existence question reads, so a section that
            # lives only in a declaration or a typed record still appears; the
            # section-scoped blockers beside it are the split's own addition.
            sections = sorted(
                declared_section_identities(
                    _plan_authored_state(plan, docs_dir, project, slug)
                )
                | set(section_blockers)
            )
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
            row["section_depends_on"] = mapped_edges
        # The section rows rank the plan's own ready work, which the row already
        # states twice: the sections it declares implementable, less the ones a
        # section-scoped hold names. The hold set is the row's own, so a hold
        # the row does not report cannot hold a section here.
        authored_by_plan[slug] = authored
        plan_ready_sections = [
            section
            for section in row["implementable_sections"]
            if section not in set(row.get("blocked_sections") or [])
        ]
        row["stuck"] = any(
            section_is_stuck(
                attempts_by_plan.get(slug, {})
                .get(section_record_id(section), {})
                .get("attempts", 0),
                blocked_run_seconds.get((slug, section))
                or blocked_run_seconds.get((slug, section_record_id(section))),
            )
            for section in plan_ready_sections
        )
        ready_sections_by_plan[slug] = plan_ready_sections
        # Every hold the plan itself names, by section and by kind, so a section
        # row reports a dependency, a gate and a decision alongside the two
        # kinds only a run can carry. A row that reports a hold of one kind and
        # hides another reads as less blocked than the section is.
        named_holds: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for blocker in section_dependency_blockers:
            waiting = str(blocker.get("source_section") or blocker.get("stage") or "")
            if waiting:
                named_holds[waiting].append(
                    {**blocker, "kind": SECTION_DEPENDENCY_BLOCKER}
                )
        for held_section, holding in decision_section_blockers.items():
            for decision in holding:
                named_holds[str(held_section)].append(
                    {**decision, "kind": DECISION_BLOCKER}
                )
        for gate in section_gate_blockers:
            for gated in gate.get("gated_sections") or []:
                named_holds[str(gated)].append({**gate, "kind": GATE_BLOCKER})
        section_holds_by_plan[slug] = named_holds
        if slug in live_runs:
            row["in_flight"] = live_runs[slug]
        if slug in interrupted_runs:
            row["interrupted"] = interrupted_runs[slug]
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
        elif not closable:
            deferred.append(row)
        if is_schedule_deferred:
            schedule_deferred.append(row)
        decision_blockers_report.extend(decision_blockers)
        waiting_decisions_report.extend(waiting_decisions)
        open_decisions_report.extend(open_decisions)
        deferred_decisions_report.extend(deferred_decisions)

    # The two wiring findings that make the edges coordinators carry in
    # messages visible, emitted through the one helper every finding uses.
    findings.extend(
        _undeclared_wait_findings(
            project,
            plans,
            resolved_docs,
            authored_by_plan,
            section_runs,
            section_holds_by_plan,
            declared_section_refs,
        )
    )
    findings.extend(
        _overlapping_scope_findings(open_sections_by_plan, section_runs, scope_claims)
    )

    open_paths, critical, critical_members = rank_open_paths(
        plans,
        section_effort=section_effort,
        local_graph=local_graph,
        edge_meta=edge_meta,
        cycle_members=cycle_members,
        closable_slugs=closable_slugs,
        max_paths=max_paths,
        remaining_wall_hours=_remaining_wall_hours,
        remaining_effort_hours=_remaining_effort_hours,
        uncalibrated_plans=_uncalibrated_plans,
        terminal_statuses=TERMINAL_STATUSES,
        status_of=_status,
    )
    # The sections the critical path runs through, so a ranked section row
    # reports the same membership a plan row does.
    critical_sections = set(critical.get("sections") or [])

    def plan_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return plan_ranking_key(
            row, critical_members=critical_members, sprint_order=sprint_order
        )

    ready.sort(key=plan_key)
    pending.sort(key=lambda row: (not row["ready"], *plan_key(row)))
    blocked.sort(key=plan_key)
    deferred.sort(key=plan_key)
    schedule_deferred.sort(key=plan_key)
    unauthorised.sort(
        key=lambda row: (
            row["age_days"] is None,
            -(row["age_days"] or 0),
            row["slug"],
        )
    )
    plan_rows: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        (
            plan_key(row),
            {
                "slug": row["slug"],
                "sprint": row["sprint"],
                "progress_pct": row["progress_pct"],
                "section_attempts": row["section_attempts"],
                "unlocks": row["unlocks"],
                "dependency_ready": row["dependency_ready"],
                "dependency_readiness": row["dependency_readiness"],
                "schedule_ready": row["schedule_ready"],
                "schedule_readiness": row["schedule_readiness"],
                "schedule_deferred_reason": row["schedule_deferred_reason"],
                "schedule_behind_sprint": row["schedule_behind_sprint"],
                "endpoint_closures": endpoint_memberships.get(row["slug"], []),
                "reason": (
                    f"ready \u2014 sequenced after {', '.join(row['came_after'])}"
                    if row.get("came_after")
                    else "critical path"
                    if row["slug"] in critical_members
                    else "stuck"
                    if row["stuck"]
                    else "ready with all hard prerequisites satisfied"
                ),
            },
        )
        for row in ready
    ]
    # One row per ready section, ranked by the same extended key as the plan
    # rows. The plan stays ranked too: a section-scoped wait holds its section,
    # not the plan, so both are ready and both are reported. The ref names the
    # section as a clickable anchor and names its plan beside it.
    section_rows: list[dict[str, Any]] = []
    ready_plan_slugs = {str(row["slug"]) for row in ready}
    # A section that carries a run is reported whether or not its plan is ready:
    # work in flight is on the page because somebody is doing it, and a section
    # a blocked or deferred plan hides would take the run on it off the page
    # with it. One row per section either way, never two.
    for row in pending:
        plan_slug = str(row["slug"])
        authored = authored_by_plan.get(plan_slug, {})
        plan_ready_sections = (
            (list(ready_sections_by_plan.get(plan_slug, [])))
            if plan_slug in ready_plan_slugs
            else []
        )
        run_sections = sorted(
            section
            for ref_plan, section in section_runs
            if ref_plan == plan_slug and section not in plan_ready_sections
        )
        for section in plan_ready_sections + run_sections:
            node = f"{plan_slug}#{section}"
            attempts = (
                attempts_by_plan.get(plan_slug, {})
                .get(section_record_id(section), {})
                .get("attempts", 0)
            )
            stuck = section_is_stuck(
                attempts,
                blocked_run_seconds.get((plan_slug, section))
                or blocked_run_seconds.get((plan_slug, section_record_id(section))),
            )
            section_holders = sorted(
                section_runs.get((plan_slug, section), []),
                key=lambda run_row: str(run_row.get("run_id") or ""),
            )
            blockers = [
                *section_holds_by_plan.get(plan_slug, {}).get(section, []),
                *run_blocker_rows(section_holders),
                *held_path_blockers_by_section.get((plan_slug, section), []),
            ]
            section_rows.append(
                {
                    "ref": node,
                    "slug": node,
                    "plan": plan_slug,
                    "sprint": row["sprint"],
                    "roi": row["roi"],
                    "effort": row["effort"],
                    "remaining_effort_hours": _section_effort_hours(authored, section),
                    "unlocks": sorted(dependents.get(node, set())),
                    "stuck": stuck,
                    "after_hold": bool(row.get("after_hold")),
                    "ready": section in plan_ready_sections,
                    "runs": section_holders,
                    "held_paths": held_paths_by_section.get((plan_slug, section), []),
                    "blockers": blockers,
                }
            )
    # One ordering over both populations: a plan row and a section row are ranked
    # by the same key and read in that order, so the row a reader should start
    # with leads whichever kind of row it is. A term one population cannot carry
    # is neutral for it, which is what lets one key serve both.
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = list(plan_rows)
    ranked.extend(
        (
            section_ranking_key(
                row, critical_sections=critical_sections, sprint_order=sprint_order
            ),
            row,
        )
        for row in section_rows
    )
    ranked.sort(key=lambda entry: entry[0])
    # A section row reports the term of the shared key on which it leads the
    # section row below it, so its reason names the signal that did the placing
    # rather than the one left over once the stronger signals are absent. Plan
    # rows keep the reason the readiness walk gives them.
    section_entries = [entry for entry in ranked if "ref" in entry[1]]
    for index, (key, row) in enumerate(section_entries):
        rival = (
            section_entries[index + 1][0]
            if index + 1 < len(section_entries)
            else section_row_floor(key)
        )
        row["reason"] = leading_signal(key, rival)
    immediate: list[dict[str, Any]] = []
    for position, (_key, row) in enumerate(ranked, start=1):
        row["order"] = position
        immediate.append(row)

    downstream = _downstream_sprints(all_plans, membership)
    sprint_rows: list[dict[str, Any]] = []
    for position, sprint in enumerate(sprints, start=1):
        sprint_name = str(sprint.get("id") or "")
        sprint_ref = str(sprint.get("_ref") or sprint_name)
        sprint_project = str(sprint.get("_project") or project)
        sprint_status = str(sprint.get("status") or "planned")
        members = resolved_sprint_items.get(sprint_ref, [])
        derived_state = _derived_sprint_state(members)
        completed = sum(
            bool(member.get("found"))
            and str(member.get("status") or "") in COMPLETED_STATUSES
            for member in members
        )
        progress = sum(
            max(0.0, min(1.0, float(member.get("impl", 0.0) or 0.0)))
            for member in members
            if member.get("found")
        )
        member_slugs = [
            str(member["slug"])
            for member in members
            if member.get("found") and member.get("slug")
        ]
        # A sprint's runs are its members' runs. The two lists are kept apart on
        # the row so a reader counting work in progress does not absorb a run
        # that has stopped and is waiting for a decision.
        sprint_in_flight = [
            run
            for member_slug in member_slugs
            for run in live_runs.get(member_slug, [])
        ]
        sprint_interrupted = [
            run
            for member_slug in member_slugs
            for run in interrupted_runs.get(member_slug, [])
        ]
        sprint_rows.append(
            {
                "order": position,
                "id": sprint_name,
                "ref": sprint_ref,
                "scope": "external" if sprint_project != project else "local",
                "project": sprint_project,
                "found": not bool(sprint.get("_unresolved")),
                "status": sprint_status,
                "derived_state": derived_state,
                "items": len(members),
                "members": members,
                "in_flight": sprint_in_flight,
                "interrupted": sprint_interrupted,
                "resolved_items": sum(bool(member.get("found")) for member in members),
                "completed": completed,
                "lifecycle_completion_pct": round(100 * completed / len(members), 1)
                if members
                else (100.0 if sprint_status in COMPLETED_STATUSES else 0.0),
                "implementation_pct": round(100 * progress / len(members), 1)
                if members
                else (100.0 if sprint_status in COMPLETED_STATUSES else 0.0),
                "ready": sum(row.get("sprint") == sprint_ref for row in ready),
                "blocked": sum(row.get("sprint") == sprint_ref for row in blocked),
                "deferred": sum(row.get("sprint") == sprint_ref for row in deferred),
                "schedule_ready": sum(
                    row.get("sprint") == sprint_ref and row["schedule_ready"]
                    for row in pending
                ),
                "schedule_deferred": sum(
                    row.get("sprint") == sprint_ref for row in schedule_deferred
                ),
                "feeds_sprints": downstream.get(sprint_ref, {}).get(
                    "feeds_sprints", []
                ),
                "unblocks": downstream.get(sprint_ref, {}).get("unblocks", []),
                **(
                    {
                        "state_drift": {
                            "stored": sprint_status,
                            "derived": derived_state,
                        }
                    }
                    if sprint_status != derived_state
                    and not _drift_is_member_progress(sprint_status, derived_state)
                    else {}
                ),
            }
        )

    plan_values = list(plans.values())
    completed_count = sum(_status(plan) in COMPLETED_STATUSES for plan in plan_values)
    uncalibrated = _uncalibrated_plans(plans)
    allocation = (project_manifest or {}).get("scope") or {}
    if review is None:
        review_docs_dir = _load_mounts().get(project)
        review_block = None
        if review_docs_dir is not None:
            review_block, _review_version = load_composed_review(
                review_docs_dir,
                project,
                list(all_plans.values()),
                sprints,
                project_manifest,
            )
    elif review:
        review_block = compose_review(
            review, list(all_plans.values()), sprints, project, project_manifest
        )
    else:
        review_block = None
    review_findings = _review_health(project, review_block, local_graph)
    inventory_docs_dir = Path(docs_dir) if docs_dir is not None else None
    findings.extend(_unwired_plan_findings(project, plans, inventory_docs_dir))
    sprint_ids = {
        identity
        for sprint in sprints
        for identity in (str(sprint.get("id") or ""), str(sprint.get("_ref") or ""))
        if identity
    }
    findings.extend(
        _followup_work_findings(project, all_plans, inventory_docs_dir, sprint_ids)
    )
    if review_block is not None:
        review_block = dict(review_block)
        review_block["findings"] = [
            dict(row)
            for row in review_block.get("findings") or []
            if isinstance(row, dict) and not row.get("resolved_at")
        ]
    findings.extend(review_findings)
    summary_sprints = sprint_summary_rows(
        project,
        sprints,
        all_plans,
        recent_days=_sprint_recent_days(project, resolved_docs),
        liveness=liveness,
        resolved_items=resolved_sprint_items,
    )
    judgment_members = sorted(
        (member for members in judgment_required_rows.values() for member in members),
        key=lambda member: (
            str(member.get("dependent") or ""),
            str(member.get("target") or ""),
            str(member.get("reason") or ""),
        ),
    )
    return {
        "project": project,
        "scope": {"sprint": sprint_id, "plans": len(plan_values)},
        "active_sprint_id": active_sprint_id,
        "live_sprint_ids": live_sprint_ids(liveness),
        "open_sprint_ids": open_sprint_ids,
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
        "endpoints": endpoint_rows,
        "sprints": sprint_rows,
        "summary_sprints": summary_sprints,
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
        "waiting_decisions": waiting_decisions_report,
        "judgment_required": {
            "required": bool(judgment_members),
            "count": len(judgment_members),
            "members": judgment_members,
        },
        "deferred_decisions": deferred_decisions_report,
        "decision_readiness": {
            "ready": not decision_blockers_report,
            "open": len(open_decisions_report),
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
