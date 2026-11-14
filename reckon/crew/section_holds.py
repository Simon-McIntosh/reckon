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

import threading
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

#: The gate a promoted ledger row carries. Only a promoted row is a delivered
#: run; a failed or superseded row recorded an outcome rather than a delivery.
_PROMOTED_GATE = "passed"


def _pointer_parts(pointer: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """The node identity, plan, section, role and phase of one live pointer."""

    node = pointer.get("node")
    node = node if isinstance(node, Mapping) else {}
    node_id = str(node.get("id") or "")
    plan = str(node.get("plan") or pointer.get("plan") or "")
    section = section_record_id(node.get("section") or pointer.get("section") or "")
    role = str(node.get("role") or pointer.get("role") or "implement")
    return node_id, plan, section, role, str(pointer.get("phase") or "")


def _row_parts(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """The same five parts for one ledger row, which stores them flat."""

    return (
        str(row.get("node") or ""),
        str(row.get("plan") or ""),
        section_record_id(row.get("section") or ""),
        str(row.get("role") or "implement"),
        _PROMOTED_GATE,
    )


#: The ledger file a project's rows were last read from, its stat identity, and
#: the rows themselves. A roadmap build reads the same history once per process
#: until promotion writes another ledger row, so the read is bounded by the file
#: rather than charged to every build.
_HISTORY: dict[
    tuple[str, str], tuple[tuple[int, int] | None, list[dict[str, Any]]]
] = {}

_HISTORY_LOCK = threading.Lock()


def _history_identity(project: str, root: str | Path | None) -> tuple[int, int] | None:
    from reckon import ledger

    try:
        stat = ledger.ledger_path(project, root).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _promoted_rows(project: str, root: str | Path | None) -> list[dict[str, Any]]:
    """The project's promoted ledger rows, read once per ledger state."""

    key = (project, str(root))
    identity = _history_identity(project, root)
    if identity is None:
        return []
    with _HISTORY_LOCK:
        cached = _HISTORY.get(key)
        if cached is not None and cached[0] == identity:
            return cached[1]
    from reckon import ledger

    try:
        rows = ledger.picker_runs(project, root)
    except Exception:  # noqa: BLE001 - an unreadable ledger holds nothing
        return []
    with _HISTORY_LOCK:
        _HISTORY[key] = (identity, rows)
    return rows


def _collect(
    project: str,
    root: str | Path | None,
    pointers: Iterable[Mapping[str, Any]],
    *,
    plans: Iterable[str] | set[str] | frozenset[str] | None = None,
) -> tuple[
    dict[tuple[str, str], dict[str, dict[str, Any]]],
    dict[tuple[str, str], set[str]],
]:
    """Split each section's known nodes into held and free, by node id.

    A node is identified by the dispatch node id its run record names. The two
    records can name the same node, and they agree in that case — the pointer is
    the request that has not been promoted yet — so the held row is keyed by node
    and a later promoted row replaces an earlier live one rather than counting
    twice. The section id stands in when a record names no node, so a row written
    before node ids were recorded still holds its own section.
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

    for row in _promoted_rows(project, root):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("gate") or "") != _PROMOTED_GATE:
            continue
        node_id, plan, section, role, _gate = _row_parts(row)
        if role not in EXECUTABLE_SECTION_ROLES:
            continue
        key = target(plan, section)
        if key is None:
            continue
        node_key = node_id or section
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

    held, _free = _collect(project, root, pointers, plans={plan})
    key = (str(plan), section_record_id(section))
    nodes = held.get(key, {})
    wanted = str(node or "") or key[1]
    return [row for node_key, row in sorted(nodes.items()) if node_key == wanted]


def section_nodes(
    project: str,
    root: str | Path | None,
    pointers: Iterable[Mapping[str, Any]] = (),
    *,
    plans: Iterable[str] | None = None,
) -> dict[tuple[str, str], set[str]]:
    """Every node id the run records know of, held or free, per section."""

    held, free = _collect(project, root, pointers, plans=plans)
    nodes: dict[tuple[str, str], set[str]] = {}
    for key, entries in held.items():
        nodes.setdefault(key, set()).update(entries)
    for key, entries in free.items():
        nodes.setdefault(key, set()).update(entries)
    return nodes
