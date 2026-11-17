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
from reckon.crew.node import CrewError, TaskNode
from reckon.crew.refusals import format_refusal
from reckon.crew.routing import _context_contributors
from reckon.crew.run_paths import _utc_now, crew_home

RECORD_DIRECTORY = "refusal-traces"
CONTEXT_REFUSAL_REASON = "context-window-exceeded"

# A launch that outlived its bound is released by the admission pass, a
# separate process, so the release has to reach the launch itself. The launch
# reads this record rather than the claim, because the holder that published
# the claim may still be alive and blocked and can never observe that its own
# claim was taken away. The record outlives the run directory the release
# removes, which is why it is keyed on the run id and never read from inside
# that directory.
LAUNCH_REFUSAL_REASON = "launch-span-exceeded"
LAUNCH_REFUSAL_VERSION = 1

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


def launch_refusal_path(run_id: str, *, home: str | Path | None = None) -> Path:
    """Where the release of one run's launch span is recorded."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", str(run_id)).strip("-") or "run"
    return _home(home) / RECORD_DIRECTORY / f"launch-{readable}.json"


def record_launch_refusal(
    *,
    run_id: str,
    step: str,
    holder: Mapping[str, Any] | None = None,
    attempt: Mapping[str, Any] | None = None,
    home: str | Path | None = None,
) -> Path:
    """Record that a launch span was released, naming the step it reached.

    The step and the holder are what the released launch's own fence reports
    when it resumes: a span that stopped before the spawn and one that stopped
    just after are different failures, and only the step tells them apart.

    ``attempt`` names the launch attempt the release abandoned, so the fence
    holds against that attempt and no other. A request the release put back in
    the queue launches again under the same run id, and a fence keyed on the
    run alone refused the request's own next attempt for the whole hour.
    """
    payload = {
        "version": LAUNCH_REFUSAL_VERSION,
        "run_id": str(run_id),
        "reason": LAUNCH_REFUSAL_REASON,
        "recorded_at": _utc_now(),
        "step": str(step or ""),
        "holder": dict(holder or {}),
        "attempt": dict(attempt or {}),
    }
    path = launch_refusal_path(run_id, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(
        path, payload, indent=2, sort_keys=True, mode=0o600, ensure_ascii=True
    )
    return path


def mark_launch_requeued(run_id: str, *, home: str | Path | None = None) -> bool:
    """Record that the request behind this release is back in the queue.

    The release writes its record before it knows whether the launch came from a
    queued request, so the re-queue — which is what authorises a second attempt
    of that same request — marks the record once it lands. A release with the
    mark admits a later attempt of the run; one without it fences the run for
    the whole bound. The answer is whether a record was there to mark: a launch
    the release gave back had no request to re-queue and holds nothing.
    """
    path = launch_refusal_path(run_id, home=home)
    stored = launch_refusal(run_id, home=home)
    if stored is None:
        return False
    stored["requeued"] = True
    write_json_atomically(
        path, stored, indent=2, sort_keys=True, mode=0o600, ensure_ascii=True
    )
    return True


def launch_refusal(
    run_id: str, *, home: str | Path | None = None
) -> dict[str, Any] | None:
    """The release on record for this run, or None when no span was released."""
    try:
        stored = json.loads(launch_refusal_path(run_id, home=home).read_text("utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(stored, Mapping):
        return None
    if stored.get("version") != LAUNCH_REFUSAL_VERSION:
        return None
    if str(stored.get("run_id") or "") != str(run_id):
        return None
    return dict(stored)


# The claim fields that name one launch attempt of a run. A run id is not an
# attempt: a request the release put back in the queue launches again under the
# same id, and it publishes a claim of its own, registering it afresh, before it
# reaches the reservation the fence guards. So the release names the abandoned
# attempt by these fields, and a release that put its request back in the queue
# admits a claim of the run that names another one.
ATTEMPT_FIELDS = ("claim_registered_at", "holder_pid", "holder_host")

# How long a release fences the attempt it abandoned. A launch slowed by
# storage resumes within minutes of its release; a request the release put back
# in the queue launches again under the same run id, and is admitted as soon as
# its own attempt publishes a claim. The bound still applies to a release that
# names no attempt it can compare, and to a launch that resumes before any later
# attempt has published one.
RELEASED_LAUNCH_FENCE_SECONDS = 3600.0


def _release_has_lapsed(refusal: Mapping[str, Any]) -> bool:
    """Whether a recorded release is older than the fence it holds."""
    recorded = parse_utc(str(refusal.get("recorded_at") or ""))
    if recorded is None:
        return False
    age = (datetime.now(UTC) - recorded).total_seconds()
    return age > RELEASED_LAUNCH_FENCE_SECONDS


def _live_launch_claim(
    run_id: str, *, home: str | Path | None = None
) -> dict[str, Any]:
    """The claim the run's live claim file holds, or an empty record.

    Read through the claim module's own reader, so a claim belonging to another
    run and an unreadable file answer here as they do everywhere else, and the
    directory is resolved from the crew home this module was handed rather than
    from the process's own, so a reader pointed at one home never answers about
    a run of another.
    """
    from reckon.crew import dispatch_claims, runs

    directory = (
        runs.run_dir(run_id) if home is None else _home(home) / "runs" / str(run_id)
    )
    return dispatch_claims.read_claim(directory, str(run_id))


def _names_the_released_attempt(
    refusal: Mapping[str, Any], run_id: str, *, home: str | Path | None = None
) -> bool:
    """Whether the launch reaching its reservation now is the released attempt.

    A release names the attempt whose claim it took: its registration and the
    holder that published it. A different claim of the same run is a later
    attempt — the request the release put back in the queue, launching again —
    and is admitted, but only for a release that recorded that re-queue: the
    mark is what says a second attempt of this request was authorised, so a
    launch of a released run the port gave back still waits out the whole bound.
    No claim at all is the released attempt's own run: the requeue takes the
    claim with it and nothing else publishes one for that attempt, so a launch
    that resumes before a later attempt does is refused. A record written before
    the attempt was named cannot be compared and holds as it did before.
    """
    named = refusal.get("attempt")
    if not isinstance(named, Mapping) or not named:
        return True
    if refusal.get("requeued") is not True:
        return True
    claim = _live_launch_claim(run_id, home=home)
    if not claim:
        return True
    return all(
        str(claim.get(field) or "") == str(named.get(field) or "")
        for field in ATTEMPT_FIELDS
    )


def refuse_a_released_launch(
    run_id: str, *, started: bool = False, home: str | Path | None = None
) -> None:
    """Refuse a launch whose span the admission pass has already released.

    Read twice by the launch: before the spawn and again after it. The second
    read closes the window between the first check and the fork, so a launch
    that stalls there and resumes after its release does not spawn a worker for
    a run that no longer exists — and, having already spawned, hands the pid to
    the rollback that signals it.

    The refusal is the abandoned attempt's, not the run's: a request the release
    put back in the queue launches again under the same run id, and its next
    attempt is admitted on the next pass while the slow original is still
    refused at its reservation.
    """
    refusal = launch_refusal(run_id, home=home)
    if refusal is None:
        return
    if _release_has_lapsed(refusal):
        return
    if not _names_the_released_attempt(refusal, run_id, home=home):
        return
    step = str(refusal.get("step") or "an unrecorded step")
    when = (
        "while its worker was being started"
        if started
        else "before its worker was started"
    )
    raise CrewError(
        format_refusal(
            "D24",
            f"run {run_id!r} was released as a stranded launch at step {step} {when}; "
            "the launch is refused",
        )
    )
