"""A plan's coordinating session is derived from the ledger, beside its owner.

Two ownership facts already exist and this module joins them rather than adding
a third. The plan's ``<meta name="plan-owner">`` names the accountable person,
and every ledger run row carries ``session`` — the crew session that dispatched
it — beside ``session_id``, which is the worker's own harness session. A plan
with no run has no recorded coordinator, so a reader that only lists live run
pointers cannot say who to ask about it.

A plan's coordinator is the dispatching session of its most recent dispatched
run, live or committed, inside a staleness window. A run is matched to its plan
by the row's ``plan`` field, and, where that is null because the run was
dispatched from a brief, by its ``plan_link``. A run carrying neither is matched
by the plan its node id names: a plan review's node is minted from the plan
review prefix followed by the plan slug and names that slug, and a run review's
node ``review-of-<source>`` names the run
it reviews, whose own plan the review then carries. That source is the reviewed
run's node id in the ordinary case and its run id for a paired arm, and the
resolver the fleet already mints and reads those node ids with —
``_reviewed_run_id`` in ``dispatch_sessions`` — turns either spelling into the
reviewed run's id over the same rows this module reads. A run matched by none of
these belongs to no plan, and a plan with no matching row is unowned — reported
as itself, never guessed from a neighbouring row or an older dispatch.

The window is a configuration value so the caller, never this module, decides
how long a dispatch still speaks for a plan. A dispatch older than it is
history: the ledger keeps the row, and the plan's coordinator reverts to the
most recent dispatch still inside the window, or to unowned when there is none.
Only rows that name a session are consulted, because a run nobody's session
dispatched can name no coordinator.

The answer carries both facts the plan records — the ``plan-owner`` person and
the coordinator session by its id — and says whether that session is live, so an
ended session reads as ended. No surface resolves a session to a peer name
today, so none is claimed here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon._plan_html import read_state_and_text_file
from reckon._store import _docs_dir_for_project
from reckon._timestamps import parse_utc
from reckon.crew import directory, runs
from reckon.crew.dispatch_sessions import _reviewed_run_id
from reckon.crew.node import parse_duration
from reckon.crew.recovery_vocabulary import (
    PLAN_REVIEW_NODE_PREFIX,
    REVIEW_NODE_PREFIX,
)
from reckon.crew.repair import round_node_source

#: How long a dispatched run still speaks for its plan. A run is dispatched by a
#: session that usually outlives it by days, so the window is measured in days
#: rather than in the minutes a lane reading is trusted for; a caller that wants
#: a tighter answer passes its own value.
DEFAULT_COORDINATOR_WINDOW = "168h"

#: The run fields that name a plan, consulted in this order. Each is a fallback
#: for the one before it: a run dispatched from a plan section carries ``plan``,
#: a brief-dispatched run carries ``plan_link``, and a review carries neither and
#: names its subject in its node id.
MATCH_FIELDS = ("plan", "plan_link", "node")

#: The two tiers a plan resolves to: a coordinator session, or itself as unowned.
TIER_COORDINATOR = "coordinator"
TIER_UNOWNED = "unowned"


def _plan_token(value: object) -> str:
    """The plan slug a ledger field names, with any section suffix removed.

    A ``plan_link`` written by an unplanned landing names the section it changed
    (``iter-power-supply-model#s2``), so the slug is the part before the ``#``.
    """
    return str(value or "").split("#", 1)[0].strip()


def _node_id(row: Mapping[str, Any]) -> str:
    """The node id a run row carries, as a bare string.

    A live pointer holds its node as an object and a committed row holds the id
    as a string, so both spellings of the same identity read back here.
    """
    node = row.get("node")
    if isinstance(node, Mapping):
        return str(node.get("id") or "")
    return str(node or "")


def _plan_of_run(
    row: Mapping[str, Any], *, rows: Mapping[str, Mapping[str, Any]]
) -> tuple[str, str]:
    """The plan a run belongs to, and the field that named it.

    Returns ``("", "")`` for a run that names no plan, which is a fact about the
    run rather than a failure: a brief run with no plan link and no review node
    is dispatched against a brief, and belongs to no plan.
    """
    for field in MATCH_FIELDS:
        if field in ("plan", "plan_link"):
            plan = _plan_token(row.get(field))
            if plan:
                return plan, field
            continue
        plan = _plan_from_node(row, rows=rows)
        if plan:
            return plan, field
    return "", ""


def _reviewed_run(
    row: Mapping[str, Any], *, rows: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The run a review row reviews, or ``None`` when its node id names none.

    The node id carries what the review was composed for — the reviewed run's
    node id, or its run id where the node id was minted for a paired arm — and
    the fleet's own resolver, which mints and reads those node ids everywhere
    else, turns either spelling into the reviewed run's id over the rows this
    module is reading. A source that resolves to no row this run set holds is no
    subject at all, rather than one guessed from the listing order.
    """
    source = round_node_source(_node_id(row), REVIEW_NODE_PREFIX)
    if not source:
        return None
    return rows.get(_reviewed_run_id(source, list(rows.values())))


def _plan_from_node(
    row: Mapping[str, Any], *, rows: Mapping[str, Mapping[str, Any]]
) -> str:
    """The plan a run's node id names, or ``""`` for one that names no plan.

    A plan review names its plan directly, since its node id embeds the slug it
    reviews. A run review names the run it reviews — a node id, not a run id —
    so its plan is that run's own, resolved by the same rules, and the review's
    dispatcher, never the reviewed run's, is the session that answered.
    """
    node_id = _node_id(row)
    if node_id.startswith(PLAN_REVIEW_NODE_PREFIX):
        return round_node_source(node_id, PLAN_REVIEW_NODE_PREFIX)
    if node_id.startswith(REVIEW_NODE_PREFIX):
        subject = _reviewed_run(row, rows=rows)
        return _plan_of_run(subject, rows=rows)[0] if subject else ""
    return ""


def _dispatch_instant(row: Mapping[str, Any]) -> datetime | None:
    """When a run was dispatched, or ``None`` when no readable stamp says so.

    A committed row carries ``dispatched_at``; a live pointer written before that
    field existed carries only ``created_at``, which is the same instant for a
    run that has not been re-queued. A row that names neither cannot be shown to
    be inside any window, so it is treated as too old rather than as current.
    """
    return parse_utc(row.get("dispatched_at") or row.get("created_at"))


def _run_rows(
    project: str, root: str | Path | None, pointers: Iterable[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """Every run of a project by run id, committed rows and live pointers alike.

    A promoted run's pointer is gone by definition and an in-flight run's row is
    not committed yet, so neither source alone holds the project's dispatches.
    The pointer wins where both hold one, being the copy the run's own dispatcher
    most recently wrote.
    """
    rows: dict[str, Mapping[str, Any]] = {}
    for row in ledger.run_rows_from_documents(project, root=root):
        run_id = str(row.get("run_id") or "")
        if run_id:
            rows[run_id] = row
    for pointer in pointers:
        run_id = str(pointer.get("run_id") or "")
        if run_id:
            rows[run_id] = pointer
    return rows


def _plans_and_owners(docs_dir: Path | None) -> dict[str, str]:
    """Every plan of a project by slug, with the owner its own meta records."""
    if docs_dir is None:
        return {}
    plans_dir = docs_dir / "plans"
    if not plans_dir.is_dir():
        return {}
    owners: dict[str, str] = {}
    for path in sorted(plans_dir.glob("*.html")):
        state, _text = read_state_and_text_file(path)
        owners[path.stem] = str(state.get("owner") or "").strip()
    return owners


def _live_sessions(project: str, pointers: Iterable[Mapping[str, Any]]) -> set[str]:
    """The sessions the coordinator directory reads as still dispatching.

    A session whose runs have all reached a terminal phase is not live, so an
    ended coordinator reads as ended rather than as one that never existed. The
    verdict comes from the directory's own grouping, which is the one place the
    fleet already decides what a dispatching session is.
    """
    view = directory.directory(project=project, pointers=list(pointers))
    return {
        str(row["session"])
        for row in view["coordinators"]
        if row.get("state") == "dispatching" and row.get("session")
    }


def _supersedes(
    candidate: tuple[datetime, str], held: tuple[datetime, str] | None
) -> bool:
    """Whether a run's dispatch replaces the one a plan currently holds.

    A plan's coordinator is its most recent dispatch, so the later instant
    wins and the run id breaks a tie between two rows stamped alike. It is
    named rather than inlined so the ordering rule is one thing a caller can
    replace with a broken one and see the answer move.
    """
    return held is None or candidate > held


def resolve_plan_coordinators(
    project: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    window: str = DEFAULT_COORDINATOR_WINDOW,
    pointers: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Name each plan's coordinator.

    Reads the committed ledger rows and the live pointers, matches each run to
    its plan, and keeps the most recent match per plan inside ``window``. Returns
    one row per plan of the project, keyed by slug, each naming the recorded
    ``plan-owner`` beside the coordinator session, the tier it resolved to, and
    whether that session is live. A plan whose newest dispatch falls outside the
    window, or which has no dispatch at all, resolves to ``unowned``.

    ``root`` resolves both the ledger and the plans directory against a specific
    checkout, which is what lets a caller read a project's history from the
    checkout it names rather than from the registered one.
    """
    moment = now or datetime.now(UTC)
    horizon = moment - timedelta(seconds=parse_duration(window))
    live_pointers = (
        list(pointers)
        if pointers is not None
        else list(runs.list_live(project=project))
    )
    rows = _run_rows(project, root, live_pointers)
    live_sessions = _live_sessions(project, live_pointers)

    winners: dict[str, tuple[datetime, str, Mapping[str, Any], str]] = {}
    for run_id, row in rows.items():
        session = str(row.get("session") or "").strip()
        if not session:
            # A run nobody's session dispatched names no coordinator; it is not a
            # weaker match for the plan, it is no match at all.
            continue
        plan, matched_by = _plan_of_run(row, rows=rows)
        if not plan:
            continue
        instant = _dispatch_instant(row)
        if instant is None or instant < horizon or instant > moment:
            continue
        held = winners.get(plan)
        if held is not None and not _supersedes((instant, run_id), (held[0], held[1])):
            continue
        winners[plan] = (instant, run_id, row, matched_by)

    owners = _plans_and_owners(_docs_dir_for_project(project, root))
    plans: dict[str, dict[str, Any]] = {}
    for slug in sorted(set(owners) | set(winners)):
        winner = winners.get(slug)
        session = str(winner[2].get("session") or "").strip() if winner else ""
        plans[slug] = {
            "plan": slug,
            "plan_owner": owners.get(slug, ""),
            "coordinator": session or None,
            "tier": TIER_COORDINATOR if session else TIER_UNOWNED,
            "live": bool(session) and session in live_sessions,
            "run_id": winner[1] if winner else None,
            "matched_by": winner[3] if winner else "",
            "dispatched_at": (
                str(winner[2].get("dispatched_at") or winner[2].get("created_at") or "")
                if winner
                else ""
            ),
        }
    return {
        "ok": True,
        "project": project,
        "window": window,
        "generated_at": moment.isoformat(),
        "plan_count": len(plans),
        "coordinator_count": sum(
            row["tier"] == TIER_COORDINATOR for row in plans.values()
        ),
        "plans": plans,
    }
