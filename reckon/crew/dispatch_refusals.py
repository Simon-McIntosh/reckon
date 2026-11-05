"""Leave a trace when a refused node comes back with a shorter brief.

A context-fit refusal stops a node before a worktree exists, so a coordinator
who shortens the brief and dispatches the same node again leaves no record that
the cut ever happened. The cut is what made an oversized brief fit, and a
reader of the landed run cannot tell it from a brief that always fitted. So a
refusal writes what it measured under the crew home — never into the repository,
which has no business carrying a refusal that never produced a run — and the
next dispatch of that same node that fits records the reduction on the run.
Promotion rebuilds the committed row from the live pointer's node block, so the
record reaches the reader auditing landed work as well as the one watching the
dispatch.

The record is a flag with the two token figures rather than a stored diff: the
refused and accepted estimates, the repository paths the refused attempt charged
and this one does not, and how much node text went away. A run accepted on its
first attempt carries no marker at all, so the marker's presence means a
reduction happened.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon._store import write_json_atomically
from reckon._timestamps import parse_utc
from reckon.crew.node import TaskNode
from reckon.crew.routing import _context_contributors
from reckon.crew.run_paths import _utc_now, crew_home

RECORD_DIRECTORY = "refusal-traces"
CONTEXT_REFUSAL_REASON = "context-window-exceeded"

# The payload's shape, so a trace written by an earlier version is read as
# absent rather than misread: the paths were mappings once, and a reader that
# accepted them would report a dict repr as a file that left the brief.
TRACE_VERSION = 2

# The origins routing's contributor reader gives a repository input. A
# standing instruction file is charged against the estimate too, but it is not
# something a brief can cut, so it never appears on a record of what left one.
REPOSITORY_ORIGINS = frozenset({"declared write path", "mention"})

# A refusal quotes the few inputs that dominate its estimate; a trace and the
# reduction read back from it compare all of them, so the reader is asked for
# every contributor rather than for the three a refusal renders.
_EVERY_CONTRIBUTOR = 10_000

# A refusal is evidence about the brief a coordinator is about to cut, and that
# cut happens in the same sitting. A trace older than this describes a brief
# nobody is holding any more, and matching a later dispatch to it would report
# a reduction that never happened.
TRACE_MAX_AGE = timedelta(hours=24)


def _home(home: str | Path | None) -> Path:
    return Path(home) if home is not None else crew_home()


def record_path(
    node: TaskNode,
    *,
    project: str,
    home: str | Path | None = None,
) -> Path:
    """Where one attempt's refusal trace is kept, keyed by the node identity."""
    identity = f"{project}\x00{node.plan}\x00{node.section}\x00{node.id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", node.id).strip("-") or "node"
    return _home(home) / RECORD_DIRECTORY / f"{readable}-{digest}.json"


def is_context_refusal(verdict: Mapping[str, Any]) -> bool:
    """Whether a competence verdict refused on the node's context estimate."""
    if verdict.get("allowed"):
        return False
    return str(verdict.get("reason") or "").startswith(CONTEXT_REFUSAL_REASON)


def _charged_paths(context: Mapping[str, Any]) -> list[str]:
    """The repository inputs a context verdict charged, as routing names them.

    routing's contributor reader is the one place that knows the shape a
    context verdict carries its inputs in, so the trace and the reduction take
    its records rather than walking ``inputs['repository_files']`` a second
    time. Each record names the resolved path, which is what identifies one
    file across two attempts that may spell it differently.
    """
    return [
        str(record["path"])
        for record in _context_contributors(context, limit=_EVERY_CONTRIBUTOR)
        if record.get("origin") in REPOSITORY_ORIGINS
    ]


def _node_text_bytes(node: TaskNode) -> int:
    """The node's own text, measured so a reduction with no path change is visible."""
    return len(f"{node.goal}\n{node.done_when}\n{node.brief}".encode())


def record_context_refusal(
    *,
    node: TaskNode,
    project: str,
    verdict: Mapping[str, Any],
    home: str | Path | None = None,
) -> Path | None:
    """Write the trace of a context refusal under the crew home.

    A verdict refused for another reason writes nothing, so the caller can hand
    every refusal here rather than deciding which ones are a context refusal.
    A later refusal for the same node overwrites the earlier trace: the attempt
    that has to be cut is the one the accepted dispatch will be compared with.
    """
    if not is_context_refusal(verdict):
        return None
    context = verdict.get("context")
    context = context if isinstance(context, Mapping) else {}
    payload = {
        "version": TRACE_VERSION,
        "node": node.id,
        "project": project,
        "plan": node.plan,
        "section": node.section,
        "recorded_at": _utc_now(),
        "reason": CONTEXT_REFUSAL_REASON,
        "estimated_tokens": int(verdict.get("estimated_tokens") or 0),
        "window_tokens": int(verdict.get("window_tokens") or 0),
        "shortfall_tokens": int(verdict.get("shortfall_tokens") or 0),
        "text_bytes": _node_text_bytes(node),
        "paths": _charged_paths(context),
    }
    path = record_path(node, project=project, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(
        path, payload, indent=2, sort_keys=True, mode=0o600, ensure_ascii=True
    )
    return path


def brief_reduction(
    *,
    node: TaskNode,
    project: str,
    verdict: Mapping[str, Any],
    home: str | Path | None = None,
) -> dict[str, Any] | None:
    """The marker a run carries when its brief was cut after a refusal.

    Answers ``None`` unless a refusal is on record for this node, the accepted
    attempt estimates below the refused one, and something in the brief actually
    changed. The last condition is what keeps a lane change from reading as a
    reduction: a brief that is dispatched unchanged to a lane whose own agent
    layout measures differently has not been cut, and must not say it was.
    """
    path = record_path(node, project=project, home=home)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(stored, Mapping) or stored.get("version") != TRACE_VERSION:
        return None
    refused_at = parse_utc(stored.get("recorded_at"))
    if refused_at is None or datetime.now(tz=UTC) - refused_at > TRACE_MAX_AGE:
        return None
    context = verdict.get("context")
    context = context if isinstance(context, Mapping) else None
    if context is None:
        return None
    accepted_tokens = int(context.get("estimated_tokens") or 0)
    refused_tokens = int(stored.get("estimated_tokens") or 0)
    accepted_paths = set(_charged_paths(context))
    removed_paths = [
        str(entry)
        for entry in stored.get("paths") or ()
        if str(entry) not in accepted_paths
    ]
    removed_text_bytes = max(
        0, int(stored.get("text_bytes") or 0) - _node_text_bytes(node)
    )
    if accepted_tokens >= refused_tokens:
        return None
    if not removed_paths and removed_text_bytes == 0:
        return None
    return {
        "retried_after_refusal": True,
        "refused_estimated_tokens": refused_tokens,
        "accepted_estimated_tokens": accepted_tokens,
        "refused_window_tokens": int(stored.get("window_tokens") or 0),
        "removed_paths": removed_paths,
        "removed_text_bytes": removed_text_bytes,
        "refused_at": str(stored.get("recorded_at") or ""),
    }


def consume_context_refusal(
    *,
    node: TaskNode,
    project: str,
    home: str | Path | None = None,
) -> None:
    """Drop a trace once a run carrying it is published.

    The trace answers one question — was this run cut after a refusal — and the
    run that carried the answer is the only reader of it. Consuming it here
    keeps a later dispatch of the same node from inheriting an older reduction,
    so the marker on any run names a refusal for that dispatch and no other.
    """
    try:
        record_path(node, project=project, home=home).unlink()
    except FileNotFoundError:
        return
