"""Compose the one-call answer that explains any plan ref.

``read_plan(view="why", ...)`` answers the question a coordinator otherwise
settles by reading a plan, then a pointer file, then messaging a peer: what the
thing is and its state, what it waits on transitively, what waits on it, which
runs and sessions are working on it and which paths they hold, and its last
five events.

Every fact here has an owner and this module derives none of them again. The
waits come from the roadmap's section-dependency edges
(``roadmap._plan_authored_html`` read through ``_schema.section_depends_on``)
and its readiness judgement (``roadmap.dependency_row_state`` and
``roadmap._section_satisfied``). The runs come from
``mcp_views.partition_live_runs``, through the classifier the roadmap, the
sprint view and ``recover`` already share, and the paths they hold are the
dispatch scope claims the pointer carries. The events are the plan's own
timestamped records — a followup written or resolved, a decision locked, a run's
start — sorted newest first.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon._schema import (
    declared_section_identities,
    parse_plan_ref,
    section_depends_on,
)
from reckon.live_run_classification import (
    HELD_PATH,
    RUN_BLOCKED,
    SECTION_DEPENDENCY_BLOCKER,
)

#: The most characters one ``why`` answer may return. This is the contract the
#: view is built to, and the read enforces it before the answer leaves, so a
#: wide wait graph is trimmed rather than refused. It is the same figure a
#: ``read_plan`` default view is held to in ``reckon/mcp_budget.py``, and this
#: view joins that table once its own row is registered there.
WHY_VIEW_BUDGET = 24_000
#: How many sections of a wait chain are walked before the answer says it
#: stopped, so a cycle or a very deep graph cannot make one read unbounded.
MAX_WAIT_DEPTH = 4
#: How many events the answer carries; the goal fixes this at five.
MAX_EVENTS = 5
#: One prose field is trimmed to this before it travels.
SHOWN_TEXT = 160


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
) -> dict[str, Any]:
    """Answer what one ref is, what it waits on, and who is working on it.

    ``inventory`` is the project's composed plan rows — the same rows the
    roadmap read is built from — so a slug resolves against the project's live
    documents rather than against one caller's arguments. ``plan_data`` is the
    queried plan's parsed state where the caller already holds it; absent, it is
    read through the same file the roadmap reads.
    """

    plans = {
        str(row.get("slug") or ""): row
        for row in inventory
        if isinstance(row, Mapping) and row.get("slug")
    }
    wanted_section = _section_identity(section)
    if slug not in plans:
        raise WhyRefusalError(
            "ref_not_found",
            f"No live plan named {slug!r} in project {project!r}.",
            _nearest_refs(slug, plans),
        )
    plan_row = plans[slug]
    authored = _authored_state(plan_row, docs_dir, project, slug)
    identities = declared_section_identities(
        plan_row, authored.get("section_declarations")
    )
    if wanted_section and wanted_section not in identities:
        raise WhyRefusalError(
            "section_not_found",
            f"Plan {slug!r} declares no section {wanted_section!r}.",
            _nearest_refs(
                wanted_section,
                [f"{slug}#{identity}" for identity in sorted(identities)],
            ),
        )

    data = dict(plan_data or {})
    run_rows = _manageable_runs(project, pointers)
    target_ref = f"{slug}#{wanted_section}" if wanted_section else slug

    waits, wait_walks = _transitive_waits(
        project, slug, wanted_section, plans, docs_dir, run_rows
    )
    answer: dict[str, Any] = {
        "view": "why",
        "project": project,
        "ref": target_ref,
        "what": _what(plan_row, data, slug, wanted_section, authored),
        "waits_on": waits,
        "waited_on_by": _waited_on_by(project, slug, wanted_section, plans, docs_dir),
        "runs": _runs_on(run_rows, slug, wanted_section),
        "followups": _open_followups(data, wanted_section),
    }
    answer["events"] = _events(data, answer["waits_on"], answer["runs"])
    if wait_walks["truncated"]:
        answer["waits_truncated"] = True
    answer["counts"] = {
        "waits_on": len(answer["waits_on"]),
        "waited_on_by": len(answer["waited_on_by"]),
        "runs": len(answer["runs"]),
        "followups": len(answer["followups"]),
    }
    if limit is not None and limit >= 0:
        for key in ("waits_on", "waited_on_by", "runs", "followups"):
            answer[key] = answer[key][:limit]
    return answer


# ── facts the answer carries ─────────────────────────────────────────────────


def _what(
    plan_row: Mapping[str, Any],
    data: Mapping[str, Any],
    slug: str,
    section: str,
    authored: Mapping[str, Any],
) -> dict[str, Any]:
    """The thing's identity and its own state, never its container's."""

    from reckon.roadmap import dependency_row_state

    declarations = authored.get("section_declarations")
    plan_status = str(plan_row.get("status") or data.get("status") or "")
    what: dict[str, Any] = {
        "kind": "section" if section else "plan",
        "plan": slug,
        "title": str(data.get("title") or plan_row.get("title") or slug),
        "plan_status": plan_status,
        "state": dependency_row_state(plan_row, section, declarations),
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


def _transitive_waits(
    project: str,
    slug: str,
    section: str,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
    run_rows: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Walk what the ref waits on, and what each of those waits on in turn.

    Each entry carries the wait's kind — a section-dependency, a gate, a
    decision, a run reported blocked, a path another run holds — so a reader
    sees why the target is not done rather than only that it is not; the
    roadmap owns each kind's judgement and this walk only follows the edges it
    names.
    """

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = {(slug, section)}
    frontier: list[tuple[str, str, int]] = [(slug, section, 0)]
    truncated = False
    while frontier:
        current_slug, current_section, depth = frontier.pop(0)
        if depth >= MAX_WAIT_DEPTH:
            truncated = True
            continue
        for ref in _declared_refs(
            project, current_slug, current_section, plans, docs_dir
        ):
            parsed = parse_plan_ref(ref)
            if parsed is None:
                continue
            target_section = parsed.stage or ""
            key = (parsed.slug, target_section)
            row = _wait_row(project, ref, parsed, plans, docs_dir, run_rows, depth + 1)
            rows.append(row)
            if row.get("resolvable") and key not in seen:
                seen.add(key)
                frontier.append((parsed.slug, target_section, depth + 1))
    return rows, {"truncated": truncated}


def _declared_refs(
    project: str,
    slug: str,
    section: str,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
) -> list[str]:
    """The refs one section (or one plan) declares it waits on."""

    from reckon.roadmap import _plan_authored_html

    plan_row = plans.get(slug) or {}
    if not section:
        return [
            str(ref)
            for ref in plan_row.get("depends_on") or []
            if parse_plan_ref(str(ref)) is not None
        ]
    mapping = (
        section_depends_on(_plan_authored_html(plan_row, docs_dir, project, slug)) or {}
    )
    raw = mapping.get(section)
    refs = [raw] if isinstance(raw, str) else raw
    if not isinstance(refs, list):
        return []
    return [str(ref) for ref in refs if parse_plan_ref(str(ref)) is not None]


def _wait_row(
    project: str,
    ref: str,
    parsed: Any,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
    run_rows: Mapping[tuple[str, str], list[dict[str, Any]]],
    depth: int,
) -> dict[str, Any]:
    """One wait, resolved to the target's own state and its holders."""

    from reckon.roadmap import (
        _section_satisfied,
        dependency_row_state,
    )

    target_project = parsed.project or project
    section = parsed.stage or ""
    row: dict[str, Any] = {
        "ref": ref,
        "kind": SECTION_DEPENDENCY_BLOCKER,
        "depth": depth,
        "resolvable": False,
    }
    target = plans.get(parsed.slug)
    if target_project != project or target is None:
        row["satisfied"] = False
        return row
    authored = _authored_state(target, docs_dir, project, parsed.slug)
    declarations = authored.get("section_declarations")
    row["resolvable"] = True
    row["state"] = dependency_row_state(target, section, declarations)
    row["satisfied"] = (
        _section_satisfied(target, section, declarations) if section else False
    )
    holders = run_rows.get((parsed.slug, section), [])
    if holders:
        row["runs"] = [holder["run_id"] for holder in holders]
        row["held_paths"] = sorted(
            {path for holder in holders for path in holder.get("held_paths") or []}
        )
        # The wait itself is the declared section dependency; the target's own
        # hold is the second fact, so a reader knows both why it waits and why
        # the thing it waits for has not moved.
        row["hold"] = HELD_PATH if row["held_paths"] else RUN_BLOCKED
    return row


def _waited_on_by(
    project: str,
    slug: str,
    section: str,
    plans: Mapping[str, Mapping[str, Any]],
    docs_dir: Path | None,
) -> list[dict[str, Any]]:
    """The sections of this project whose declared waits name this ref."""

    from reckon.roadmap import _plan_authored_html, dependency_row_state

    rows: list[dict[str, Any]] = []
    for waiting_slug in sorted(plans):
        mapping = (
            section_depends_on(
                _plan_authored_html(
                    plans[waiting_slug], docs_dir, project, waiting_slug
                )
            )
            or {}
        )
        authored = _authored_state(plans[waiting_slug], docs_dir, project, waiting_slug)
        for waiting_section, raw_refs in mapping.items():
            refs = [raw_refs] if isinstance(raw_refs, str) else raw_refs
            if not isinstance(refs, list):
                continue
            for ref in refs:
                parsed = parse_plan_ref(str(ref))
                if parsed is None:
                    continue
                if (parsed.project or project) != project:
                    continue
                if parsed.slug != slug or (parsed.stage or "") != section:
                    continue
                rows.append(
                    {
                        "ref": f"{waiting_slug}#{waiting_section}",
                        "kind": SECTION_DEPENDENCY_BLOCKER,
                        "state": dependency_row_state(
                            plans[waiting_slug],
                            waiting_section,
                            authored.get("section_declarations"),
                        ),
                    }
                )
    rows.sort(key=lambda row: str(row["ref"]))
    return rows


def _manageable_runs(
    project: str, pointers: list[dict[str, Any]] | None
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group live runs by their target section, keeping each pointer's claims.

    The classification is ``partition_live_runs``' answer, so a run reads the
    same here as it does on the roadmap row and in ``crew live``. The paths each
    run holds and the session it belongs to come from the pointer the partition
    already read, never from a second probe.
    """

    from reckon import crew
    from reckon.mcp_views import partition_live_runs

    if pointers is None:
        try:
            pointers = crew.list_live()
        except OSError:
            pointers = []
    in_flight, interrupted = partition_live_runs(project, pointers)
    by_run: dict[str, Mapping[str, Any]] = {
        str(pointer.get("run_id") or ""): pointer
        for pointer in pointers
        if isinstance(pointer, Mapping)
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for plan_ref, rows in (
        *((ref, rows) for ref, rows in in_flight.items()),
        *((ref, rows) for ref, rows in interrupted.items()),
    ):
        for row in rows:
            pointer = by_run.get(str(row.get("run_id") or "")) or {}
            node = pointer.get("node")
            node = node if isinstance(node, Mapping) else {}
            enriched = {
                **row,
                "held_paths": [str(path) for path in node.get("write_paths") or ()],
                "session": str(pointer.get("session_id")),
                "phase": str(pointer.get("phase") or ""),
            }
            grouped.setdefault((plan_ref, str(row.get("section") or "")), []).append(
                enriched
            )
    return grouped


def _runs_on(
    run_rows: Mapping[tuple[str, str], list[dict[str, Any]]],
    slug: str,
    section: str,
) -> list[dict[str, Any]]:
    """The runs working on this ref now, with the paths they hold."""

    return [_run_row(row) for row in run_rows.get((slug, section), [])]


def _run_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One run as the answer renders it: identity, target, holder of paths."""

    rendered: dict[str, Any] = {
        "run_id": str(row.get("run_id") or ""),
        "classification": str(row.get("classification") or ""),
        "member": str(row.get("member") or ""),
    }
    for key in ("section", "session", "phase", "started_at"):
        value = row.get(key)
        if value:
            rendered[key] = str(value)
    held = [str(path) for path in row.get("held_paths") or ()]
    if held:
        rendered["held_paths"] = held
    return rendered


def _open_followups(data: Mapping[str, Any], section: str) -> list[dict[str, Any]]:
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


def _events(
    data: Mapping[str, Any],
    waits: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The plan's own timestamped records, newest first, five at most.

    These are records the plan already carries rather than a second store: a
    followup written or resolved, a decision locked, and a run that started on
    the ref. A record with no timestamp is not an event and is skipped, because
    ordering it would put it somewhere a reader would trust.
    """

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


def _authored_state(
    plan_row: Mapping[str, Any],
    docs_dir: Path | None,
    project: str,
    slug: str,
) -> Mapping[str, Any]:
    from reckon.roadmap import _plan_authored_state

    return _plan_authored_state(plan_row, docs_dir, project, slug)


def _section_identity(section: Any) -> str:
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


def _nearest_refs(wanted: str, candidates: Iterable[str]) -> str:
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
    "WHY_VIEW_BUDGET",
    "WhyRefusalError",
    "why_answer",
]
