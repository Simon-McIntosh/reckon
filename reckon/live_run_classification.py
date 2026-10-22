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
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group run rows by the section each one sits on.

    The row is the run as one classification already gave it — the same row the
    plan-level views report — so a section row and the plan row above it name
    the same run with the same classification rather than each reading the
    pointer again. A run naming no section groups nowhere.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        section = str(row.get("section") or "")
        if section:
            grouped.setdefault(section, []).append(dict(row))
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


def held_path_blockers(claims: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The paths one run holds and another is waiting on, holder first.

    Two runs claiming one path are not the same condition twice: the holder's
    claim fences the path, and the waiting run's claim is the one that was
    disregarded, so the pair is reported in that order and a reader learns both
    who holds the path and which run cannot start. A path with one claim, or
    with no claim that still binds, holds nothing.
    """

    by_path: dict[str, dict[str, list[str]]] = {}
    for claim in claims:
        path = str(claim.get("path") or "")
        run_id = str(claim.get("run_id") or "")
        if not path or not run_id:
            continue
        side = "holders" if claim.get("binding", True) else "waiting"
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
