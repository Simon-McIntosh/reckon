"""Cross-project directory of coordinators represented by live run pointers."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon.crew.query import _mounted_projects as mounted_projects
from reckon.crew.query import project_run_rows, run_section_index
from reckon.crew.recovery import classify_pointer
from reckon.crew.runs import list_live


class DirectoryError(RuntimeError):
    """A directory selector does not identify one live run."""


#: Guards the ready-idle join against re-entry: the plan-coordinator resolver
#: reads this view to decide which sessions are still dispatching.
_JOIN_STATE = threading.local()

#: The page size the ready-idle join reads the ready-set view at. The join reads
#: every page, so this figure only bounds one request's rows.
_READY_IDLE_PAGE_LIMIT = 200


#: The fields a directory run row carries.
#:
#: The coordinator rows and the unowned rows are projected by their own calls,
#: and either list can be empty — a project whose runs all belong to a session
#: has no unowned run, and a session whose runs have all ended has no
#: coordinator row — so the vocabulary is declared here rather than read from
#: the rows in hand. An empty list says nothing about which fields the view has;
#: a name is accepted whether or not a row of that kind is in it, and a field
#: whose value is empty is left off the row by the projection.
DIRECTORY_RUN_FIELDS = (
    "run_id",
    "node",
    "project",
    "repository",
    "plan",
    "section",
    "plan_section",
    "phase",
    "classification",
    "process_alive",
    # Present only where the pointer recorded one.
    "sprint",
    "transport",
)


def _node(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("node")
    return value if isinstance(value, Mapping) else {}


def _observed_transport(record: Mapping[str, Any]) -> dict[str, str] | None:
    """Return a messaging address only when a run event explicitly supplied it."""
    log_value = str(record.get("log_path") or "")
    if not log_value:
        return None
    log = Path(log_value)
    original = log.parent / "stream.jsonl" if log.name.startswith("resume-") else log
    streams = [
        original,
        *sorted(log.parent.glob("resume-*.jsonl"), key=ledger._resume_stream_order),
    ]
    observed: str | None = None
    observed_in: str | None = None
    for stream in streams:
        if not stream.is_file():
            continue
        try:
            with stream.open(encoding="utf-8") as events:
                for line in events:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    address = str(event.get("messaging_socket_path") or "").strip()
                    if address:
                        observed = address
                        observed_in = str(stream)
        except OSError:
            continue
    if observed is None or observed_in is None:
        return None
    return {
        "address": observed,
        "kind": "messaging-socket",
        "observed_in": observed_in,
    }


def _run_row(record: Mapping[str, Any]) -> dict[str, Any]:
    node = _node(record)
    # Liveness is derived inside the classifier, under its host gate: the
    # directory view asks the process table only where the record's launching
    # host is this host, so a pointer written on another machine is never read
    # through a pid this host happens to hold.
    classified = classify_pointer(record)
    # The classifier's phase is the run's own evidence — its stream, its worker
    # record, its delivery — while the pointer's is the launcher's label, which
    # only ``observe`` advances. The row carries the same reading the live and
    # classifier rows do, so one run cannot read two ways across the views that
    # render it; the stored label stands only where the derivation has nothing
    # to say about it.
    row = {
        "run_id": str(record.get("run_id") or ""),
        "node": str(node.get("id") or ""),
        "project": str(record.get("project") or ""),
        "repository": str(record.get("repo") or ""),
        "plan": str(node.get("plan") or ""),
        "section": str(node.get("section") or ""),
        "phase": str(classified.get("phase") or "") or str(record.get("phase") or ""),
        "classification": str(classified.get("classification") or ""),
        "process_alive": classified.get("process_alive"),
    }
    if row["plan"] and row["section"]:
        row["plan_section"] = (
            f"{row['plan']}#{ledger.normalize_section(row['section'])}"
        )
    sprint = record.get("sprint") or node.get("sprint")
    if sprint:
        row["sprint"] = str(sprint)
    transport = _observed_transport(record)
    if transport is not None:
        row["transport"] = transport
    return row


def _is_dispatching(run: Mapping[str, Any]) -> bool:
    """A session remains active while a worker lives or has resumable work."""
    if run.get("process_alive") is True:
        return True
    return str(run.get("classification") or "") in {"running", "waiting"}


def _held_paths(
    record: Mapping[str, Any], row: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The write paths one run holds, each with the scope check's own verdict.

    The claim a run publishes is the same claim the dispatch scope check judges,
    so the verdict is read from that check (``claim_disposition``) rather than
    inferred here: a reader asking which files a peer holds needs to know
    whether the claim still fences them, and a second opinion about that is how
    the board and the enforcer come to disagree.
    """
    from reckon.crew.node import claim_disposition

    node = _node(record)
    declared = [
        str(path) for path in node.get("write_paths") or () if str(path).strip()
    ]
    if not declared:
        return []
    disposition = claim_disposition(record)
    return [
        {
            "path": path,
            "run_id": str(row.get("run_id") or ""),
            "node": str(row.get("node") or ""),
            "binding": bool(disposition.binding),
            "disposition_reason": str(disposition.reason or ""),
        }
        for path in declared
    ]


def _queued_rows(
    records: Sequence[Mapping[str, Any]],
    rows_by_run: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Each queued request's place, by run id, from the admission pass's order.

    The place is the one the admission pass will launch at, read through
    :func:`reckon.crew.queue_admission.queued_placements`, so the board and the
    pass cannot disagree about which request goes first.
    """
    from reckon.crew.queue_admission import queued_placements

    queued = [
        record for record in records if str(record.get("phase") or "") == "queued"
    ]
    if not queued:
        return {}
    placements = queued_placements(records)
    placed: dict[str, dict[str, Any]] = {}
    for record in queued:
        run_id = str(record.get("run_id") or "")
        row = rows_by_run.get(run_id) or {}
        placement = placements.get(run_id) or {}
        entry: dict[str, Any] = {
            "run_id": run_id,
            "node": str(row.get("node") or ""),
        }
        if row.get("plan_section"):
            entry["plan_section"] = row["plan_section"]
        for key in ("position", "rank", "effective_rank", "reason"):
            if placement.get(key) is not None:
                entry[key] = placement[key]
        placed[run_id] = entry
    return placed


def _ready_idle_rows(
    project: str, pointers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The ready-idle section rows a coordinator owns, from the existing views.

    This joins two owners and derives neither fact: the plan-coordinator
    resolver names each plan's session, and the ready-set view filters the
    canonical roadmap to the rows with no live run. A plan whose coordinator
    owns no ready-idle row contributes nothing.

    The resolver reads the directory to decide which sessions are still
    dispatching, so this join is not made again inside that read: the re-entry
    would recurse between the two, and the inner read is about liveness rather
    than about ready work.
    """
    if getattr(_JOIN_STATE, "running", False):
        return []
    _JOIN_STATE.running = True
    try:
        # A workstation-scope read spans every mounted project, so the join is
        # made once per project: the roadmap and the ready-set view are read for
        # one project at a time, and an omitted join would report every project
        # as having no idle owner.
        if project not in (None, "*"):
            return _ready_idle_rows_from_owners(project, pointers)
        rows: list[dict[str, Any]] = []
        # The mounted set is read from the owner that resolves it for the rest
        # of the package, so a registry shape this view does not know about is
        # still every project, and an unreadable registry fails closed there
        # rather than reading as no projects at all.
        for name in sorted(mounted_projects()):
            owned = [
                pointer
                for pointer in pointers
                if str(pointer.get("project") or "") == name
            ]
            rows.extend(_ready_idle_rows_from_owners(name, owned))
        return rows
    finally:
        _JOIN_STATE.running = False


def _ready_idle_rows_from_owners(
    project: str, pointers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    from reckon.crew.plan_coordinator import resolve_plan_coordinators
    from reckon.mcp_discovery import _discover_project, _inventory_row
    from reckon.mcp_views import ready_set_view
    from reckon.roadmap import build_roadmap

    resolved = resolve_plan_coordinators(project, pointers=pointers)
    coordinators = resolved.get("plans") or {}
    if not coordinators:
        return []
    discovered = _discover_project(project, None)
    roadmap = build_roadmap(
        project,
        [_inventory_row(item) for item in discovered.get("inventory") or ()],
        list(discovered.get("sprints") or ()),
        active_sprint_id=discovered.get("active_sprint_id"),
    )
    # The ready-set view pages like every other view, so its first page is not
    # its answer: every page is read, or an owner whose sections sort past the
    # first page is reported as idle over nothing.
    ready: list[Mapping[str, Any]] = []
    cursor: str | None = None
    while True:
        view = ready_set_view(
            roadmap,
            coordinators=coordinators,
            ready_idle=True,
            cursor=cursor,
            limit=_READY_IDLE_PAGE_LIMIT,
        )
        ready.extend(view.get("ready") or ())
        cursor = (view.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    rows: list[dict[str, Any]] = []
    for row in ready:
        session = str(row.get("coordinator") or "")
        if not session:
            continue
        slug = str(row.get("slug") or "")
        sections = (
            [slug]
            if "#" in slug
            else [f"{slug}#{section}" for section in row.get("ready_sections") or ()]
        )
        if not sections:
            continue
        rows.append(
            {
                "session": session,
                "plan": slug.partition("#")[0],
                "sections": sections,
                "plan_owner": str(row.get("plan_owner") or ""),
            }
        )
    return rows


def directory(
    project: str | None = None,
    *,
    run_id: str | None = None,
    node_id: str | None = None,
    pointers: Iterable[Mapping[str, Any]] | None = None,
    fields: Iterable[str] | None = None,
    ready_idle: bool = False,
    queue_places: bool = True,
) -> dict[str, Any]:
    """Group every live pointer by its owning coordinator session.

    Each run row is built here and projected here, so this view and every other
    view that lists a run carry the same field vocabulary and the same refusal
    for a field the view does not have.

    ``ready_idle`` asks for the ready-idle owners beside the runs. Those rows are
    not read on every call: deriving them runs plan discovery, which takes the
    server's discovery lock for this project, and the plan-coordinator resolver
    reaches this view from inside that same lock when a roadmap is built. A
    reader that wants them asks; the resolver, which is asking about liveness,
    does not.

    ``queue_places`` reads each queued request's place from the admission
    order. That order ranks requests by their project's roadmap, which runs plan
    discovery too, so the resolver turns it off for the same reason: from inside
    the discovery lock it would wait on the lock its own caller holds.
    """
    if run_id and node_id:
        raise DirectoryError("select by run id or node id, not both")
    records = list(pointers) if pointers is not None else list_live()
    if project not in (None, "*"):
        records = [
            record for record in records if str(record.get("project") or "") == project
        ]

    selected_by: str | None = None
    selected_value: str | None = None
    if run_id:
        selected_by, selected_value = "run", run_id
        records = [record for record in records if record.get("run_id") == run_id]
    elif node_id:
        selected_by, selected_value = "node", node_id
        records = [
            record
            for record in records
            if str(_node(record).get("id") or "") == node_id
        ]
    if selected_by and not records:
        raise DirectoryError(f"no live {selected_by} {selected_value!r}")
    if selected_by == "node" and len(records) > 1:
        matches = ", ".join(
            sorted(str(record.get("run_id") or "") for record in records)
        )
        raise DirectoryError(
            f"node {selected_value!r} is not unique across live runs: {matches}"
        )

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    unowned_runs: list[dict[str, Any]] = []
    records_by_run: dict[str, Mapping[str, Any]] = {}
    rows_by_run: dict[str, dict[str, Any]] = {}
    for record in records:
        row = _run_row(record)
        run_id = str(row.get("run_id") or "")
        if run_id:
            records_by_run[run_id] = record
            rows_by_run[run_id] = row
        if not str(record.get("session") or ""):
            unowned_runs.append(row)
            continue
        key = (
            str(record.get("session") or ""),
            str(record.get("project") or ""),
            str(record.get("repo") or ""),
        )
        grouped[key].append(row)

    queued_by_run = _queued_rows(records, rows_by_run) if queue_places else {}
    ready_idle_rows = _ready_idle_rows(project, records) if ready_idle else []
    idle_by_session: dict[str, list[str]] = defaultdict(list)
    idle_owner_by_session: dict[str, str] = {}
    for row in ready_idle_rows:
        idle_by_session[row["session"]].extend(row["sections"])
        idle_owner_by_session[row["session"]] = row["plan_owner"]

    coordinators: list[dict[str, Any]] = []
    for (session, project_name, repository), runs in sorted(grouped.items()):
        ordered_runs = sorted(runs, key=lambda row: (row["node"], row["run_id"]))
        transports = {
            run["transport"]["address"]: run["transport"]
            for run in ordered_runs
            if "transport" in run
        }
        coordinator = {
            "session": session or None,
            "project": project_name or None,
            "repository": repository or None,
            "plans": sorted({run["plan"] for run in ordered_runs if run["plan"]}),
            "sprints": sorted(
                {str(run["sprint"]) for run in ordered_runs if run.get("sprint")}
            ),
            "state": (
                "dispatching"
                if any(_is_dispatching(run) for run in ordered_runs)
                else "all-terminal"
            ),
            "live_node_count": sum(_is_dispatching(run) for run in ordered_runs),
            "runs": ordered_runs,
        }
        held: list[dict[str, Any]] = []
        for run in ordered_runs:
            record = records_by_run.get(str(run["run_id"]))
            if record is not None:
                held.extend(_held_paths(record, run))
        if held:
            coordinator["held_paths"] = sorted(
                held, key=lambda item: (item["path"], item["run_id"])
            )
        queued = [
            queued_by_run[run["run_id"]]
            for run in ordered_runs
            if run["run_id"] in queued_by_run
        ]
        if queued:
            coordinator["queued_runs"] = queued
        if idle_by_session.get(session):
            coordinator["ready_idle_sections"] = sorted(idle_by_session[session])
        if transports:
            coordinator["transports"] = [transports[key] for key in sorted(transports)]
        coordinators.append(coordinator)

    listed_sessions = {row["session"] for row in coordinators}
    coordinators.extend(
        {
            "session": session,
            "project": project if project not in (None, "*") else None,
            "repository": None,
            "plans": sorted(
                {ref.partition("#")[0] for ref in idle_by_session[session]}
            ),
            "sprints": [],
            "state": "idle",
            "live_node_count": 0,
            "runs": [],
            "plan_owner": idle_owner_by_session.get(session, ""),
            "ready_idle_sections": sorted(idle_by_session[session]),
            "ready_idle_source": "plan-coordinator",
        }
        for session in sorted(set(idle_by_session) - listed_sessions)
    )

    result: dict[str, Any] = {
        "ok": True,
        "view": "directory",
        "project": project,
        "coordinator_count": len(coordinators),
        "live_node_count": sum(row["live_node_count"] for row in coordinators),
        "coordinators": coordinators,
        "unowned_run_count": len(unowned_runs),
        "unowned_runs": sorted(unowned_runs, key=lambda row: row["run_id"]),
        # Which scope the ready-idle join ran over, so a read that did not ask
        # for those rows is not mistaken for one that found none. A
        # workstation-scope read spans every mounted project, so its join is
        # made per project and named here rather than reported as its answer.
        "ready_idle_join": (
            "not_requested"
            if not ready_idle
            else "project"
            if project not in (None, "*")
            else "workstation_scope"
        ),
    }
    if selected_by:
        owned = bool(grouped)
        run = coordinators[0]["runs"][0] if owned else unowned_runs[0]
        result["resolved"] = {
            "by": selected_by,
            "value": selected_value,
            "run_id": run["run_id"],
            "node": run["node"],
            "session": coordinators[0]["session"] if owned else None,
        }
    sections = run_section_index([records])
    for coordinator in coordinators:
        coordinator["runs"] = project_run_rows(
            coordinator["runs"],
            fields=fields,
            view="directory",
            vocabulary=DIRECTORY_RUN_FIELDS,
            sections=sections,
        )
    result["unowned_runs"] = project_run_rows(
        unowned_runs,
        fields=fields,
        view="directory",
        vocabulary=DIRECTORY_RUN_FIELDS,
        sections=sections,
    )
    return result
