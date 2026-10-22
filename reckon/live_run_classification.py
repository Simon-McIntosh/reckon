"""Resolve one live run's classification from the record that owns it.

Every reader of a run's state asks the same two questions and must not answer
them differently: which record classifies this run, and what does that record
say. The record is the run's latest watcher event when its stream carries one
and the pointer classifier otherwise, and the classification is the first key
that is set across ``recovery_classification``, ``classification`` and
``to_state``. Two readers disagreeing here is how a run reads as running in one
surface and as blocked or interrupted in another.

The module reads no project state of its own, so the roadmap's stuck signal and
the live-run partition that splits a project's runs can both import it without
either importing the other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

# The classification the shared classifier gives a run whose manifest reports a
# blocker.
BLOCKED_RUN_CLASSIFICATION = "blocked"

# The kinds of hold a section carries, and the shape each blocker row has. The
# four named by the work a section waits on are the kinds a reader sorts by, and
# two of them — a blocked run and a held path contribute — cannot be read from
# the plan at all: they belong to the run sitting on the section.
SECTION_DEPENDENCY_BLOCKER = "section-dependency"
GATE_BLOCKER = "gate"
DECISION_BLOCKER = "decision"
RUN_BLOCKED = "run-blocked"
HELD_PATH = "held-path"


def section_run_rows(
    runs_by_plan: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group the classified runs under the section each one sits on.

    The row is the run as one classification already gave it — the same row the
    plan-level views report — so a section row and the plan row above it name
    the same run with the same classification rather than each reading the
    pointer again. The key is the plan and the section together, because a
    section id names a section only within its own plan. A run naming no section
    groups nowhere.
    """

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for plan_ref, rows in runs_by_plan.items():
        for row in rows:
            section = str(row.get("section") or "")
            if section:
                grouped.setdefault((str(plan_ref), section), []).append(dict(row))
    return grouped


def run_blocker_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The run-blocked holds among the runs sitting on one section.

    A run whose manifest reports a blocker is a hold on its section, and the
    text that stopped the worker is what a reader has to act on, so it travels
    with the row. A run the classifier reads any other way holds nothing here.
    """

    return [
        {
            "kind": RUN_BLOCKED,
            "run_id": str(row.get("run_id") or ""),
            "section": str(row.get("section") or ""),
            "blocker": str(row.get("reason") or ""),
        }
        for row in rows
        if str(row.get("classification") or "") == BLOCKED_RUN_CLASSIFICATION
    ]


def held_path_blockers(
    claims: Iterable[Mapping[str, Any]],
    *,
    queued_runs: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The paths one run holds and another is waiting on, holder first.

    Two runs claiming one path are not the same condition twice: the holder's
    claim fences the path, and the queued run's claim is the one the dispatcher
    disregarded, so the pair is reported in that order and a reader learns both
    who holds the path and which run cannot start.

    A claim that does not bind is not one condition either, and only a queued
    one waits. The fence records a second reason a claim stops binding — a run
    that stopped and left no process and no unintegrated work at its paths — and
    that run holds nothing and waits on nothing, so it lists nobody. Reading
    every non-binding claim as waiting would put a stopped run on a section's
    row as a run queued behind a path it has in fact released.

    A path with no claim that still binds holds nothing.
    """

    queued = {str(run_id) for run_id in queued_runs}
    by_path: dict[str, dict[str, list[str]]] = {}
    for claim in claims:
        path = str(claim.get("path") or "")
        run_id = str(claim.get("run_id") or "")
        if not path or not run_id:
            continue
        if claim.get("binding", True):
            side = "holders"
        elif run_id in queued:
            side = "waiting"
        else:
            continue
        by_path.setdefault(path, {"holders": [], "waiting": []})[side].append(run_id)
    return [
        {
            "kind": HELD_PATH,
            "path": path,
            "holder": holder,
            "waiting": waiting,
        }
        for path, sides in sorted(by_path.items())
        for holder in sorted(set(sides["holders"]))
        for waiting in sorted(set(sides["waiting"]))
        if sides["holders"]
    ]


def held_path_rows(
    claims: Iterable[Mapping[str, Any]],
    *,
    run_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """The paths the named runs hold, one row per path and holding run.

    A section reports what the run on it has fenced, and that is a fact about
    one run: a path a single run holds is held whether or not a second run is
    waiting on it, and the holder-and-waiter blockers cannot say so because they
    need the waiter. The rows come from the same claims those blockers read, so
    a path reported as held here is one the dispatcher would refuse a second
    writer.

    A claim that does not bind holds nothing, and a run the caller did not name
    is not reported even where its claim does bind — a section reports its own
    runs' paths and no other run's.
    """

    wanted = {str(run_id) for run_id in run_ids}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for claim in claims:
        path = str(claim.get("path") or "")
        run_id = str(claim.get("run_id") or "")
        if not path or run_id not in wanted or not claim.get("binding", True):
            continue
        rows[(path, run_id)] = {"kind": HELD_PATH, "path": path, "run_id": run_id}
    return [rows[key] for key in sorted(rows)]


def recorded_live_run_classifications(project: str) -> dict[str, dict[str, Any]]:
    """The latest watcher event per run of one project, keyed by run id. One
    stream reduction serves every reader of a run's recorded state, so the
    readers act on the same event rather than on two parses that could
    disagree. A stream that cannot be read leaves every run to the pointer
    classifier.
    """

    from reckon.crew.runs import watch_stream_latest_events

    try:
        return watch_stream_latest_events(project)
    except OSError:
        return {}


def run_classification_record(
    pointer: Mapping[str, Any],
    *,
    classify: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    recorded: Mapping[str, Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """The record that classifies one run.

    The recorded watcher event leads and the classifier is the fallback. A
    classifier that refuses a pointer is no reading rather than a verdict, so
    it yields an empty record and the run is neither held nor counted as a
    death.
    """

    event = (recorded or {}).get(str(pointer.get("run_id") or ""))
    if event is not None:
        return event
    try:
        return classify(pointer)
    except Exception:  # noqa: BLE001 - a refusal is no reading, not a verdict
        return {}


def run_classification(record: Mapping[str, Any]) -> str:
    """One run's classification, from the record that classifies it.

    The three keys are one classification carried at different levels of the
    record, so the first one that is set answers for all of them. An empty
    answer means no reader has classified the run.
    """

    return str(
        record.get("recovery_classification")
        or record.get("classification")
        or record.get("to_state")
        or ""
    )
