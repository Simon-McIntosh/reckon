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

from collections.abc import Callable, Mapping
from typing import Any

# The classification the shared classifier gives a run whose manifest reports a
# blocker.
BLOCKED_RUN_CLASSIFICATION = "blocked"


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
