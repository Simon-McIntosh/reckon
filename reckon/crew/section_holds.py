"""Which section nodes a run has taken, read from both run records.

A run leaves the live pointer set when it is promoted, so the roadmap's only
run input — the live set — cannot answer whether a section has already been
built. This module answers that question from both records a run leaves behind:
the live pointers, and the ledger rows promotion writes.

The hold is per node. A section split on purpose into several dispatch nodes
names one node on each run record, and a node whose request is stored but not
yet launched (queued, awaiting its upstream, or held for its lane) is a node the
records know of that is not holding. A section is held only when every node the
records know of is held, so a section with a second node still to dispatch stays
ready for that node's work.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon._plan_html import section_record_id
from reckon.evidence import EXECUTABLE_SECTION_ROLES

#: The kind of hold a section carries while a run has taken one of its nodes.
#: It is a section blocker like the declared kinds, and it is the only one that
#: comes from a run record rather than from the plan.
RUN_HOLD = "run"

#: The phases a stored request carries before it delivers anything. Such a
#: pointer names a node that exists and is not holding: the request is waiting.
#: ``queued`` and ``held`` wait for a lane slot, the two upstream phases wait for
#: work the section declares, and ``launch-failed`` delivered nothing.
_PRE_LAUNCH_PHASES = frozenset(
    {
        "queued",
        "held",
        "awaiting-upstream",
        "blocked-upstream",
        "launch-failed",
    }
)

#: The phases of a run that stopped without delivering. A stopped run holds
#: nothing; ``interrupted`` is one the classifier read as an involuntary stop,
#: and neither is work sitting on the section.
_DEAD_PHASES = frozenset({"failed", "stopped", "interrupted"})

#: Every phase in which a live pointer holds nothing.
RELEASED_PHASES = _PRE_LAUNCH_PHASES | _DEAD_PHASES


def _pointer_parts(pointer: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """The node identity, plan, section, role and phase of one live pointer."""

    node = pointer.get("node")
    node = node if isinstance(node, Mapping) else {}
    node_id = str(node.get("id") or "")
    plan = str(node.get("plan") or pointer.get("plan") or "")
    section = section_record_id(node.get("section") or pointer.get("section") or "")
    role = str(node.get("role") or pointer.get("role") or "implement")
    return node_id, plan, section, role, str(pointer.get("phase") or "")


def _row_parts(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """The node identity, plan, section and role of one ledger row, stored flat.

    The plan is the ledger's own answer rather than the row's flat field: a run
    dispatched with a brief records no top-level ``plan`` and names it on its
    impl move or its node definition, and the projection this reader consumes
    drops both. :func:`reckon.ledger.row_plan` is the one owner of that fact,
    and the projection stores its answer, so a brief-carried run holds the same
    section an ordinary one does.
    """

    from reckon import ledger

    return (
        str(row.get("node") or ""),
        ledger.row_plan(row),
        section_record_id(row.get("section") or ""),
        str(row.get("role") or "implement"),
    )


#: The fields this module reads from a promoted row. A delivery reads only the
#: node identity, the plan and section it belongs to, its role and when it
#: completed; the wide detail a row also carries (its node definition, budget,
#: lane receipt, throughput) is never consulted here. Asking the store for these
#: and nothing else keeps the decoded population a few short strings per row
#: rather than the whole of every record, which is what the batch's peak
#: resident memory turned on.
_HISTORY_FIELDS = (
    "run_id",
    "node",
    "plan",
    "section",
    "role",
    "completed_at",
)

#: The fields :func:`reckon.ledger.row_plan` falls back to when a row carries no
#: flat plan: a brief-carried dispatch records its plan on the impl move or the
#: node definition. Read only for the rows whose flat plan is empty, so the
#: common row still costs the narrow projection above.
_PLAN_FALLBACK_FIELDS = ("run_id", "plan", "impl_move", "node_definition")


def _attach_plans(project: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill in the plan for rows that record it only on a fallback field.

    The store's own ``plan`` column is :func:`reckon.ledger.row_plan`'s answer,
    but the projected payload does not carry it when a brief-carried record
    wrote none. Those few rows are read again for the two fallback fields, and
    the answer is folded back so :func:`_row_parts` resolves the same plan it
    always did.
    """

    from reckon import ledger, run_store

    missing = [row for row in rows if not str(row.get("plan") or "").strip()]
    if not missing:
        return rows
    wanted = [str(row.get("run_id") or "") for row in missing]
    try:
        fallback = run_store.stored_run_rows(
            project, run_ids=wanted, fields=_PLAN_FALLBACK_FIELDS
        )
    except Exception:  # noqa: BLE001 - an unreadable fallback leaves the plan empty
        fallback = None
    by_id = {
        str(extra.get("run_id")): extra for extra in (fallback or ())
    }
    for row in missing:
        extra = by_id.get(str(row.get("run_id") or ""))
        if extra:
            if extra.get("impl_move") is not None:
                row["impl_move"] = extra["impl_move"]
            if extra.get("node_definition") is not None:
                row["node_definition"] = extra["node_definition"]
            resolved = ledger.row_plan(row)
            if resolved:
                row["plan"] = resolved
    return rows


#: The last section-history read per project and filter, held with the run
#: store's change token it was read at. The queue ordering rebuilds the roadmap
#: whenever a plan input moves, which is most ticks, and every rebuild asks this
#: module for the project's promoted rows. Those rows live in the run store,
#: which gains a few an hour, so a token that has not moved answers a rebuild
#: rows without reading the store; a promotion replaces the entry rather than
#: accumulating one per change. Each entry holds the narrow projection this
#: module reads, not the rows' wide detail.
_HISTORY_READS: dict[
    tuple[str, str, str | None, str | None],
    tuple[tuple[int, ...], list[dict[str, Any]]],
] = {}


def _store_history_rows(
    project: str,
    *,
    plan: str | None = None,
    node: str | None = None,
) -> list[dict[str, Any]] | None:
    """The project's promoted rows, projected to the fields this module reads.

    ``None`` means the store holds no rows for the project, so the caller falls
    back to the committed ledger. A plan or node filter is answered by the
    store's own indexed read, so queuing one node decodes that node's rows and
    not the project's whole history. The answer is kept beside the store's
    change token, so an unchanged store answers a rebuild without reading it.
    """

    from reckon import run_store

    stamp = run_store.store_stamp()
    key = (project, str(run_store.store_path()), plan, node)
    if stamp is not None:
        cached = _HISTORY_READS.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    try:
        rows = run_store.stored_run_rows(
            project, plan=plan, node=node, fields=_HISTORY_FIELDS
        )
    except Exception:  # noqa: BLE001 - an unreadable store holds nothing
        return None
    if rows is None:
        return None
    rows = _attach_plans(project, rows)
    if stamp is not None and run_store.store_stamp() == stamp:
        _HISTORY_READS[key] = (stamp, rows)
    return rows


def _history_rows(
    project: str,
    root: str | Path | None,
    *,
    plan: str | None = None,
    node: str | None = None,
) -> list[dict[str, Any]]:
    """The project's completed promotions, through the ledger's indexed read.

    Every row a store read returns is a completed promotion. A row's gate
    records what that run's own check found — ``passed``, ``not-run`` or
    ``failed`` — and not whether the node was delivered, so a delivery promoted
    under any of them holds its node. Filtering on ``passed`` would read a
    section whose run skipped or lost its check as never delivered, which is the
    delivered state this module exists to stop offering again.

    The answer is projected to the narrow set this module reads and is released
    once the caller has built its held/free index, so the population held at
    once is the index, not the store. A ``plan`` or ``node`` narrows the store's
    own read; a project with no store falls back to the committed ledger, which
    is one aggregate read and is then filtered in memory.
    """

    from reckon import ledger

    rows = _store_history_rows(project, plan=plan, node=node)
    if rows is None:
        try:
            rows = list(ledger.picker_runs(project, root))
        except Exception:  # noqa: BLE001 - an unreadable ledger holds nothing
            return []
        if plan is not None:
            rows = [row for row in rows if ledger.row_plan(row) == plan]
        if node is not None:
            rows = [
                row for row in rows if str(row.get("node") or "") == node
            ]
    return rows


def _collect(
    project: str,
    root: str | Path | None,
    pointers: Iterable[Mapping[str, Any]],
    *,
    plans: Iterable[str] | set[str] | frozenset[str] | None = None,
    history_plan: str | None = None,
    history_node: str | None = None,
) -> tuple[
    dict[tuple[str, str], dict[str, dict[str, Any]]],
    dict[tuple[str, str], set[str]],
]:
    """Split each section's known nodes into held and free, by node id.

    A node is identified by the dispatch node id its run record names. The two
    records can name the same node, and they agree in that case — the pointer is
    the request that was promoted — so the held row is keyed by node and a later
    promoted row replaces an earlier live one rather than counting twice. A
    promoted row also takes its node out of the free set: a pointer released since
    the delivery still names that node, and the row says the node was built. The
    section id stands in when a record names no node, so a row written before node
    ids were recorded still holds its own section.
    """

    wanted = None if plans is None else {str(plan) for plan in plans}
    held: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    free: dict[tuple[str, str], set[str]] = defaultdict(set)

    def target(plan: str, section: str) -> tuple[str, str] | None:
        if not plan or not section:
            return None
        if wanted is not None and plan not in wanted:
            return None
        return (plan, section)

    for pointer in pointers:
        if not isinstance(pointer, Mapping):
            continue
        if str(pointer.get("project") or project) != project:
            continue
        node_id, plan, section, role, phase = _pointer_parts(pointer)
        if role not in EXECUTABLE_SECTION_ROLES:
            continue
        key = target(plan, section)
        if key is None:
            continue
        node_key = node_id or section
        if phase in RELEASED_PHASES:
            free[key].add(node_key)
            continue
        held[key][node_key] = {
            "kind": RUN_HOLD,
            "run_id": str(pointer.get("run_id") or ""),
            "node": node_key,
            "plan": plan,
            "section": section,
            "state": "live",
            "phase": phase,
        }

    for row in _history_rows(
        project, root, plan=history_plan, node=history_node
    ):
        if not isinstance(row, Mapping):
            continue
        node_id, plan, section, role = _row_parts(row)
        if role not in EXECUTABLE_SECTION_ROLES:
            continue
        key = target(plan, section)
        if key is None:
            continue
        node_key = node_id or section
        # A promoted row is the node's last record. A pointer released since then
        # (a stored request that was dropped, a run that stopped without
        # delivering) still names the node and must not keep it free, because the
        # row says the node was delivered.
        free[key].discard(node_key)
        held[key][node_key] = {
            "kind": RUN_HOLD,
            "run_id": str(row.get("run_id") or ""),
            "node": node_key,
            "plan": plan,
            "section": section,
            "state": "promoted",
            "completed_at": str(row.get("completed_at") or ""),
        }
    return held, free


def held_section_blockers(
    project: str,
    root: str | Path | None,
    pointers: Iterable[Mapping[str, Any]] = (),
    *,
    plans: Iterable[str] | None = None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every section that every known node holds, and the runs holding it.

    A section named here has no node the records read as free, so there is no
    node of it left to dispatch. A section one of whose nodes is waiting in a
    stored request is absent: that request is the section's remaining work.
    """

    held, free = _collect(project, root, pointers, plans=plans)
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, nodes in held.items():
        if free.get(key):
            continue
        result[key] = sorted(
            nodes.values(),
            key=lambda row: (str(row.get("run_id") or ""), str(row.get("node"))),
        )
    return result


def held_node_rows(
    project: str,
    root: str | Path | None,
    pointers: Iterable[Mapping[str, Any]] = (),
    *,
    plan: str,
    section: str,
    node: str = "",
) -> list[dict[str, Any]]:
    """The runs holding one dispatch node, for a caller about to queue it.

    ``node`` is the dispatch node id. An empty one asks about the section's own
    node, which is the identity a record with no node id carries.
    """

    wanted_node = str(node or "")
    # A named node read narrows the store to that node's rows, so queuing a
    # section decodes its own delivery and not the project's whole history. A
    # record that names no node is keyed by its section, so only that case falls
    # back to the plan-wide read.
    held, _free = _collect(
        project,
        root,
        pointers,
        plans={plan},
        history_plan=plan,
        history_node=wanted_node or None,
    )
    key = (str(plan), section_record_id(section))
    nodes = held.get(key, {})
    wanted = str(node or "") or key[1]
    return [row for node_key, row in sorted(nodes.items()) if node_key == wanted]
