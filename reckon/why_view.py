"""Compose the one-call answer that explains any plan ref.

``read_plan(view="why", ...)`` answers the question a coordinator otherwise
settles by reading a plan, then a pointer file, then messaging a peer: what the
thing is and its state, what it waits on transitively, what waits on it, which
runs and sessions are working on it and which paths they hold, and its last
five events.

Every fact here has an owner and this module derives none of them again. The
waits are the roadmap's own section-scoped edge rows, resolved by
``roadmap._section_scoped_edges`` from the mapping ``roadmap._plan_section_deps``
reads, so a wait cannot read satisfied here and unsatisfied on a roadmap row.
The holds are the roadmap's named-hold assembly — ``plan_blocker_state`` and
``_decision_rows`` for a gate or a decision, ``run_blocker_rows`` from the
shared classifier for a run reported blocked — so the same gate, decision or
blocked run holds a section on both surfaces. The runs come from
``mcp_views.partition_live_runs`` and the paths they hold are the dispatch scope
claims the pointer carries. The events are the plan's own timestamped records,
sorted newest first.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon._plan_html import read_state
from reckon._schema import declared_section_identities, parse_plan_ref
from reckon.live_run_classification import (
    DECISION_BLOCKER,
    GATE_BLOCKER,
    HELD_PATH,
    RUN_BLOCKED,
    SECTION_DEPENDENCY_BLOCKER,
)

#: How many sections of a wait chain are walked before the answer says it
#: stopped, so a cycle or a very deep graph cannot make one read unbounded.
MAX_WAIT_DEPTH = 4
#: The newest five events are carried; the plan's own record holds the rest, so
#: a busy plan's history stays a glance rather than the answer.
MAX_EVENTS = 5
#: One prose field is trimmed to this before it travels.
SHOWN_TEXT = 160

#: A reader of one plan's section-scoped waits, keyed by the waiting section.
EdgeReader = Callable[[str], Mapping[str, list[dict[str, Any]]]]


class WhyRefusalError(ValueError):
    """A ref the read cannot resolve, carrying the nearest refs that do."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def why_answer(
    project: str,
    slug: str,
    section: str | None,
    *,
    inventory: Iterable[Mapping[str, Any]],
    docs_dir: Path | None,
    plan_data: Mapping[str, Any] | None = None,
    pointers: list[dict[str, Any]] | None = None,
    limit: int | None = None,
    edges: EdgeReader | None = None,
) -> dict[str, Any]:
    """Answer what one ref is, what it waits on, and who is working on it."""

    plans = {
        str(row.get("slug") or ""): row
        for row in inventory
        if isinstance(row, Mapping) and row.get("slug")
    }
    wanted = _section_identity(section)
    if slug not in plans:
        raise WhyRefusalError(
            "ref_not_found",
            f"No live plan named {slug!r} in project {project!r}.",
            _nearest_refs(slug, plans),
        )
    plan_row = plans[slug]
    authored = _authored(plan_row, docs_dir, project, slug)
    identities = declared_section_identities(
        plan_row, authored.get("section_declarations")
    )
    if wanted and wanted not in identities:
        raise WhyRefusalError(
            "section_not_found",
            f"Plan {slug!r} declares no section {wanted!r}.",
            _nearest_refs(
                wanted, [f"{slug}#{identity}" for identity in sorted(identities)]
            ),
        )

    data = dict(plan_data or {})
    states = _PlanStates(project, plans, docs_dir, seed={slug: data} if data else None)
    read_edges = edges or _memoised_edges(project, plans, docs_dir)
    run_rows = _manageable_runs(project, pointers)

    waits, truncated = _transitive_waits(
        slug, wanted, plans, states, run_rows, read_edges
    )
    answer: dict[str, Any] = {
        "view": "why",
        "project": project,
        "ref": f"{slug}#{wanted}" if wanted else slug,
        "what": _what(plan_row, data, slug, wanted, authored),
        "holds": _holds_for(slug, wanted, plans, states),
        "waits_on": waits,
        "waited_on_by": _waited_on_by(slug, wanted, plans, states, read_edges),
        "runs": _runs_on(run_rows, slug, wanted),
        "followups": _open_followups(data, wanted),
    }
    answer["events"] = _events(data, answer["runs"])
    if truncated:
        answer["waits_truncated"] = True
    answer["counts"] = {
        key: len(answer[key])
        for key in ("holds", "waits_on", "waited_on_by", "runs", "followups")
    }
    if limit is not None and limit >= 0:
        for key in ("holds", "waits_on", "waited_on_by", "runs", "followups"):
            answer[key] = answer[key][:limit]
    return answer


def _what(plan_row, data, slug, section, authored) -> dict[str, Any]:
    """The thing's identity and its own state, never its container's."""

    from reckon.roadmap import dependency_row_state

    what: dict[str, Any] = {
        "kind": "section" if section else "plan",
        "plan": slug,
        "title": str(data.get("title") or plan_row.get("title") or slug),
        "plan_status": str(plan_row.get("status") or data.get("status") or ""),
        "state": dependency_row_state(
            plan_row, section, authored.get("section_declarations")
        ),
    }
    if section:
        what["section"] = section
    impl = data.get("impl", plan_row.get("impl"))
    if isinstance(impl, (int, float)):
        what["impl"] = float(impl)
    sprint = data.get("sprint") or plan_row.get("sprint")
    if sprint:
        what["sprint"] = str(sprint)
    return what


# ── waits, read through the roadmap's own edges ─────────────────────────────


def _memoised_edges(
    project: str,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
) -> EdgeReader:
    """A reader of one plan's roadmap edge rows, resolved once per plan."""

    cache: dict[str, Mapping[str, list[dict[str, Any]]]] = {}

    def read(slug: str) -> Mapping[str, list[dict[str, Any]]]:
        if slug not in cache:
            cache[slug] = _section_edges(project, slug, plans, docs_dir)
        return cache[slug]

    return read


def _section_edges(
    project: str,
    slug: str,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
) -> Mapping[str, list[dict[str, Any]]]:
    """A plan's section waits, grouped by the section that declares them."""

    from reckon.roadmap import _plan_section_deps, _section_scoped_edges

    mapping = _plan_section_deps(plans[slug], docs_dir, project, slug)
    if not mapping:
        return {}
    rows, _findings = _section_scoped_edges(
        project, plans[slug], slug, mapping, plans, docs_dir
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("source_section") or "")].append(row)
    return dict(grouped)


def _waits_of(slug, section, plans, edges) -> list[dict[str, Any]]:
    """The edges one section — or one plan — declares."""

    if not section:
        return [
            {"ref": str(ref), "stage": ""}
            for ref in (plans.get(slug) or {}).get("depends_on") or []
            if parse_plan_ref(str(ref)) is not None
        ]
    return list(edges(slug).get(section, []))


def _transitive_waits(slug, section, plans, states, run_rows, edges):
    """Walk what the ref waits on, and what each of those waits on in turn."""

    rows: list[dict[str, Any]] = []
    seen = {(slug, section)}
    frontier = [(slug, section, 0)]
    truncated = False
    while frontier:
        current_slug, current_section, depth = frontier.pop(0)
        if depth >= MAX_WAIT_DEPTH:
            truncated = True
            continue
        for edge in _waits_of(current_slug, current_section, plans, edges):
            parsed = parse_plan_ref(str(edge.get("ref") or ""))
            if parsed is None:
                continue
            target_section = parsed.stage or str(edge.get("stage") or "")
            rows.append(_wait_row(edge, parsed, plans, states, run_rows, depth + 1))
            key = (parsed.slug, target_section)
            if parsed.slug in plans and key not in seen:
                seen.add(key)
                frontier.append((parsed.slug, target_section, depth + 1))
    return rows, truncated


def _wait_row(edge, parsed, plans, states, run_rows, depth) -> dict[str, Any]:
    """One wait, resolved to the roadmap's edge row and the target's holds."""

    from reckon.roadmap import dependency_row_state

    section = parsed.stage or str(edge.get("stage") or "")
    row: dict[str, Any] = {
        "ref": str(edge.get("ref") or ""),
        "kind": SECTION_DEPENDENCY_BLOCKER,
        "depth": depth,
        "resolvable": False,
        "satisfied": bool(edge.get("satisfied")),
    }
    target = plans.get(str(parsed.slug or ""))
    if target is None:
        return row
    authored = states.of(parsed.slug)
    row["resolvable"] = True
    row["state"] = dependency_row_state(
        target, section, authored.get("section_declarations")
    )
    if edge.get("status"):
        row["state_kind"] = str(edge["status"])
    holders = run_rows.get((parsed.slug, section), [])
    if holders:
        row["runs"] = [str(holder.get("run_id") or "") for holder in holders]
        row["held_paths"] = sorted(
            {path for holder in holders for path in holder.get("held_paths") or ()}
        )
        row["hold"] = HELD_PATH if row["held_paths"] else RUN_BLOCKED
    holds = _named_holds(parsed.slug, section, plans, states)
    holds.extend(_run_holds(holders))
    if holds:
        row["holds"] = holds
    return row


# ── holds: the roadmap's named-gate, decision and run blockers ──────────────


def _holds_for(slug, section, plans, states) -> list[dict[str, Any]]:
    """What the roadmap reports holding this ref."""

    return _named_holds(slug, section, plans, states)


def _named_holds(slug, section, plans, states) -> list[dict[str, Any]]:
    """The gate and decision holds the roadmap names for one section or plan."""

    from reckon.roadmap import (
        _decision_rows,
        _plan_decision_sections,
        plan_blocker_state,
    )

    plan_row = plans.get(slug)
    if plan_row is None:
        return []
    state = states.of(slug)
    sections_map = _plan_decision_sections(
        plan_row, states.docs_dir, states.project, slug
    )
    blocker_state = plan_blocker_state(state, sections_map=sections_map)
    if not section:
        return [
            *(
                _decision_hold(decision)
                for decision in blocker_state["plan_holding_decisions"]
            ),
            *(_gate_hold(gate) for gate in blocker_state["gate_blockers"]),
        ]
    holds = [
        _decision_hold(decision)
        for decision in _decision_rows(state, sections_map)
        if decision["status"] == "open"
        and section in {str(s) for s in decision.get("sections") or ()}
    ]
    holds.extend(
        _gate_hold(gate)
        for gate in blocker_state["section_gate_blockers"]
        if section in {str(s) for s in gate.get("gated_sections") or ()}
    )
    return holds


def _decision_hold(decision: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": DECISION_BLOCKER,
        "id": str(decision.get("key") or decision.get("id") or ""),
        "status": str(decision.get("status") or "open"),
    }
    text = decision.get("title") or decision.get("question") or decision.get("body")
    if text:
        row["what"] = _shown(str(text))
    return row


def _gate_hold(gate: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": GATE_BLOCKER,
        "id": str(gate.get("id") or ""),
        "status": str(gate.get("status") or ""),
    }
    for key in ("verdict", "measure"):
        if gate.get(key):
            row[key] = _shown(str(gate[key]))
    return row


def _run_holds(holders: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The roadmap's run-blocked rows for the runs sitting on one section."""

    from reckon.live_run_classification import run_blocker_rows

    rows: list[dict[str, Any]] = []
    for blocker in run_blocker_rows(holders):
        row: dict[str, Any] = {"kind": str(blocker.get("kind") or RUN_BLOCKED)}
        for key in ("run_id", "id", "classification", "reason", "blocked_by"):
            if blocker.get(key):
                row[key] = _shown(str(blocker[key]))
        rows.append(row)
    return rows


def _waited_on_by(slug, section, plans, states, edges) -> list[dict[str, Any]]:
    """The sections of this project whose roadmap edge names this ref."""

    from reckon.roadmap import dependency_row_state

    rows: list[dict[str, Any]] = []
    for waiting_slug in sorted(plans):
        for waiting_section, declared in edges(waiting_slug).items():
            for edge in declared:
                parsed = parse_plan_ref(str(edge.get("ref") or ""))
                if parsed is None or parsed.slug != slug:
                    continue
                if (parsed.stage or "") != section:
                    continue
                rows.append(
                    {
                        "ref": f"{waiting_slug}#{waiting_section}",
                        "kind": SECTION_DEPENDENCY_BLOCKER,
                        "state": dependency_row_state(
                            plans[waiting_slug],
                            waiting_section,
                            states.of(waiting_slug).get("section_declarations"),
                        ),
                        "satisfied": bool(edge.get("satisfied")),
                    }
                )
    rows.sort(key=lambda row: str(row["ref"]))
    return rows


# ── runs and the paths they hold ────────────────────────────────────────────


def _manageable_runs(project, pointers) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group live runs by their target section, keeping each pointer's claims."""

    from reckon import crew
    from reckon.mcp_views import partition_live_runs

    if pointers is None:
        try:
            pointers = crew.list_live()
        except OSError:
            pointers = []
    in_flight, interrupted = partition_live_runs(project, pointers)
    by_run = {
        str(pointer.get("run_id") or ""): pointer
        for pointer in pointers
        if isinstance(pointer, Mapping)
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for plan_ref, row in (
        (ref, row)
        for group in (in_flight, interrupted)
        for ref, rows in group.items()
        for row in rows
    ):
        pointer = by_run.get(str(row.get("run_id") or "")) or {}
        node = pointer.get("node")
        node = node if isinstance(node, Mapping) else {}
        enriched = {
            **row,
            "held_paths": [str(path) for path in node.get("write_paths") or ()],
            "session": str(pointer.get("session_id")),
            "phase": str(pointer.get("phase") or ""),
        }
        key = (plan_ref, str(row.get("section") or ""))
        grouped.setdefault(key, []).append(enriched)
    return grouped


def _runs_on(run_rows, slug, section) -> list[dict[str, Any]]:
    """The runs working on this ref, with the paths they hold.

    A plan-level ref is worked by whatever is on any of its sections, so those
    runs are named too rather than the plan reading unheld.
    """

    if section:
        return [_run_row(row) for row in run_rows.get((slug, section), [])]
    return [
        _run_row(row)
        for (plan_ref, _held), holders in sorted(run_rows.items())
        if plan_ref == slug
        for row in holders
    ]


def _run_row(row) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "run_id": str(row.get("run_id") or ""),
        "classification": str(row.get("classification") or ""),
        "member": str(row.get("member") or ""),
    }
    for key in ("section", "session", "phase", "started_at"):
        if row.get(key):
            rendered[key] = str(row[key])
    held = [str(path) for path in row.get("held_paths") or ()]
    if held:
        rendered["held_paths"] = held
    return rendered


# ── the plan's own records ──────────────────────────────────────────────────


def _open_followups(data, section) -> list[dict[str, Any]]:
    """The plan's open followups, filtered to those that name this section."""

    from reckon._plan_html import section_record_id
    from reckon.followup_pointers import find_invocation

    wanted = section_record_id(section) if section else ""
    rows: list[dict[str, Any]] = []
    for followup in data.get("followups") or []:
        if not isinstance(followup, Mapping):
            continue
        if str(followup.get("status") or "open").strip().lower() != "open":
            continue
        if wanted:
            invocation = find_invocation(followup)
            if invocation is None or str(invocation.section or "") != wanted:
                continue
        row: dict[str, Any] = {
            "id": str(followup.get("id") or ""),
            "status": "open",
            "title": _shown(str(followup.get("title") or followup.get("body") or "")),
        }
        if followup.get("written_at"):
            row["written_at"] = str(followup["written_at"])
        rows.append(row)
    return rows


def _events(data, runs) -> list[dict[str, Any]]:
    """The plan's own timestamped records, newest first, five at most."""

    events: list[tuple[str, dict[str, Any]]] = []
    for followup in data.get("followups") or []:
        if not isinstance(followup, Mapping):
            continue
        identifier = str(followup.get("id") or "")
        if followup.get("written_at"):
            events.append(
                (
                    str(followup["written_at"]),
                    {
                        "at": str(followup["written_at"]),
                        "event": "followup-written",
                        "ref": identifier,
                    },
                )
            )
        if followup.get("resolved_at"):
            events.append(
                (
                    str(followup["resolved_at"]),
                    {
                        "at": str(followup["resolved_at"]),
                        "event": "followup-resolved",
                        "ref": identifier,
                    },
                )
            )
    for decision in data.get("decisions") or []:
        if not isinstance(decision, Mapping) or not decision.get("when"):
            continue
        events.append(
            (
                str(decision["when"]),
                {
                    "at": str(decision["when"]),
                    "event": "decision-locked"
                    if decision.get("choice")
                    else "decision-open",
                    "ref": str(decision.get("key") or decision.get("id") or ""),
                },
            )
        )
    events.extend(
        (
            str(run["started_at"]),
            {
                "at": str(run["started_at"]),
                "event": "run-started",
                "ref": str(run.get("run_id") or ""),
            },
        )
        for run in runs
        if run.get("started_at")
    )
    events.sort(key=lambda item: item[0], reverse=True)
    return [event for _stamp, event in events[:MAX_EVENTS]]


# ── small helpers ───────────────────────────────────────────────────────────


class _PlanStates:
    """One plan's parsed state, read once per answer."""

    def __init__(self, project, plans, docs_dir, seed=None) -> None:
        self.project = project
        self.plans = plans
        self.docs_dir = docs_dir
        self._cache: dict[str, Mapping[str, Any]] = dict(seed or {})

    def of(self, slug: str) -> Mapping[str, Any]:
        cached = self._cache.get(slug)
        if cached:
            return cached
        from reckon.roadmap import _plan_authored_html

        row = self.plans.get(slug) or {}
        state = read_state(_plan_authored_html(row, self.docs_dir, self.project, slug))
        self._cache[slug] = state
        return state


def _authored(plan_row, docs_dir, project, slug) -> Mapping[str, Any]:
    from reckon.roadmap import _plan_authored_state

    return _plan_authored_state(plan_row, docs_dir, project, slug)


def _section_identity(section) -> str:
    value = str(section or "").strip()
    if "#" in value:
        parsed = parse_plan_ref(value)
        if parsed is not None and parsed.stage:
            return parsed.stage
    return value


def _shown(text: str) -> str:
    collapsed = " ".join(str(text or "").split())
    return (
        collapsed
        if len(collapsed) <= SHOWN_TEXT
        else collapsed[: SHOWN_TEXT - 3] + "..."
    )


def _nearest_refs(wanted: str, candidates: Iterable[str] | Mapping[str, Any]) -> str:
    """The refusal's hint: the closest refs that do resolve."""

    pool = sorted({str(candidate) for candidate in candidates if candidate})
    nearest = difflib.get_close_matches(str(wanted or ""), pool, n=5, cutoff=0.0)
    if not nearest:
        return (
            f"Known refs: {', '.join(pool[:5])}." if pool else "No refs resolve here."
        )
    return f"Nearest refs that resolve: {', '.join(nearest)}."


__all__ = [
    "MAX_EVENTS",
    "MAX_WAIT_DEPTH",
    "WhyRefusalError",
    "why_answer",
]
