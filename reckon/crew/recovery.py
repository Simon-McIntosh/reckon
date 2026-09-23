from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import re
import shlex
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon.crew import metering, quota_weight, runs
from reckon.crew import review as review_module
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    read_pointer,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label

# The classifier reaches liveness through the module at the point of call, so
# replacing the definition on its owning module replaces what classification
# consults. This import-time snapshot of the same function stays on this module
# for callers that patch this namespace; classification itself reads the live
# module attribute.
process_alive = runs.process_alive


# ── Recovery: what an interrupted orchestrator left behind ───────────────────

# What a live pointer can be once nobody is watching it. Worker-reported
# blocked and failed outcomes remain distinct so neither can be mistaken for a
# completed delivery that is eligible for promotion. An unreadable manifest is
# its own outcome: a file exists but no reader can judge it, which is neither a
# delivered record (completed_unpromoted) nor an absence (abandoned). Paused is
# the wait that lifts itself: the run is waiting on time or on its own job, and
# nobody has to act, because whoever or whatever lifts the run is not a person.
# The discriminator is exactly that — who lifts it. A stop that needs a person
# or another session stays blocked; a stop whose own job, a window reset or a
# bounded wait ends it is paused. Blocked is alarming because it demands a
# reader; paused must therefore always name what will lift it, so it never
# becomes the bucket a forgotten run sits in.
RECOVERY_CLASSES = (
    "running",
    "waiting",
    "paused",
    "stopped",
    "completed_unpromoted",
    "blocked",
    "failed",
    "unreadable",
    "abandoned",
)

WAITING_STATUS = "waiting"
# The waiting family is the stop that lifts itself, an overdue wait included:
# a run whose declared external wait has aged past its expectation has not
# failed and nothing about it lifts by inspection, so it is still waiting — the
# fleet counts it here, never in the blocked tally. Its age is the news, and
# the news is carried by the action marker on its row, so the wait-aged state
# also sits in the action set while remaining a member of this family.
WAITING_STATES = frozenset({"waiting", "wait-aged", "paused"})
# The manifest status vocabulary — TERMINAL_MANIFEST_STATUSES,
# NON_TERMINAL_MANIFEST_STATUSES and manifest_status_is_template — is imported
# from reckon.crew.reports, which owns the single statement of it so the reader
# refusing an unrecognised word names the same set the classifier decides
# against.
WAIT_CONDITION_STATES = frozenset({"pending", "met", "unknown"})
WAIT_PROBE_TIMEOUT_SECONDS = 1.0

# This is the authoritative answer to "what should the coordinator do now?".
# The older classification remains a lifecycle grouping used by recovery and
# promotion, while this vocabulary names the cause whose remedy differs. A
# lane hold and a worker failure therefore cannot share an instruction even
# though both remain attention-worthy terminal-looking rows.
RECOVERY_VERBS = {
    "running": "observe",
    "waiting": "wait",
    "paused": "wait",
    "completed_unpromoted": "promote",
    "held": "resume",
    "needs-help": "answer",
    "failed": "redispatch",
    "stalled": "investigate",
    "blocked": "decide",
    "stopped": "inspect",
    "scoring": "review",
    "promotable": "promote",
    "unreadable": "repair",
    "unwritten": "resume",
    "ready": "resume",
    "abandoned": "recover",
    "refused-at-admission": "resume",
    "launch-failed": "resume",
    "wait-aged": "investigate",
    INTERRUPTED_RUN_PHASE: "redispatch",
}
RECOVERY_CLASSIFICATIONS = tuple(RECOVERY_VERBS)
ACTIONABLE_RECOVERY_CLASSIFICATIONS = frozenset(
    {
        "held",
        "needs-help",
        "failed",
        "stalled",
        "blocked",
        "stopped",
        "scoring",
        "unreadable",
        "unwritten",
        "ready",
        "abandoned",
        "refused-at-admission",
        "wait-aged",
        INTERRUPTED_RUN_PHASE,
        # A launch that never reached a model wants the coordinator to repair a
        # command or a PATH, which is work only a person can do; leaving it out
        # of the actionable count is how such a run reads as invisible while it
        # occupies a lane.
        "launch-failed",
    }
)


REVIEW_NODE_PREFIX = "review-of-"

# The dispatch role that produces reviews. A run carrying it is the reviewer,
# never the reviewed, so no classification may compose a review of it: the
# composed dispatch names its own source run, so reviewing a review spawns
# another review of the same shape without limit. The promotion boundary keys
# its own exemption on the same fact — a review run is never gated on a review
# of itself.
REVIEW_ROLE = "review"


def _is_review_node(record: Mapping[str, Any]) -> bool:
    """Whether a run was minted as some run's review.

    The node id is the identity the review dispatch names, so a pointer written
    before a pointer carried a role is still recognisable as the reviewer. The
    role is the primary key — it survives a renamed node id — and this is the
    second, because either alone leaves a run that composes a review of itself
    and each link of that chain is a real dispatch against a real member.
    """
    node = record.get("node") or {}
    return str(node.get("id") or "").startswith(REVIEW_NODE_PREFIX)


def _review_dispatch_fields(record: Mapping[str, Any]) -> dict[str, str]:
    """The facts a scoring run's review dispatch is built from.

    Composed from the run's own record so the command a reader may still retype
    and the command the reflex runs come from one source: two compositions of
    the same dispatch is how a displayed command and an executed one drift
    apart while each stays correct when read on its own.
    """
    node = record.get("node") or {}
    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    source_node = str(node.get("id") or run_id)
    return {
        "run_id": run_id,
        "project": project,
        "plan": str(node.get("plan") or ""),
        "section": str(node.get("section") or ""),
        "source_node": source_node,
        "node_id": f"{REVIEW_NODE_PREFIX}{source_node}",
        "session": str(record.get("session") or "<session>"),
        "time_budget": str(node.get("time_budget") or "20m"),
        "goal": f"attach an independent review to run {run_id}",
        "done_when": (
            f"the review for {run_id} stores a parsed record scoring all 5 "
            "dimensions in the range 0..20"
        ),
        "write_path": str(review_module.review_path(project, run_id)),
    }


def _review_dispatch_argv(record: Mapping[str, Any]) -> list[str]:
    """The review dispatch as an argument vector, ready to run or to print.

    The unreconciled-runs waiver is part of the composed command because a run
    awaiting review *is* an unreconciled run: past the grace window the fence
    refuses new dispatches for the whole project until the backlog is
    reconciled, and the reconciling action for each of those runs is exactly
    the review dispatch being refused. Without the waiver the composition is a
    command that cannot succeed on the runs it is composed for.
    """
    fields = _review_dispatch_fields(record)
    owning_backend = str(record.get("backend") or "").strip()
    # The lane the owning run recorded, so a reader retyping the printed command
    # composes it from the owning run rather than from whichever runtime happens
    # to be sweeping: a hardcoded local lane attributes the choice to the
    # sweeper, which is how a project-wide sweep placed reviews on a lane their
    # owner never chose. A pointer written before a run carried a backend has
    # none to name, and keeps the local-lane spelling.
    lane = ["--local"]
    if owning_backend:
        lane = ["--backend", owning_backend]
    return [
        "reckon",
        "crew",
        "dispatch",
        "--project",
        fields["project"],
        "--plan",
        fields["plan"],
        "--section",
        fields["section"],
        "--role",
        "review",
        "--spec-level",
        "exact",
        "--node",
        fields["node_id"],
        "--goal",
        fields["goal"],
        "--done-when",
        fields["done_when"],
        "--write-path",
        fields["write_path"],
        "--time-budget",
        fields["time_budget"],
        "--session",
        fields["session"],
        "--allow-unreconciled-runs",
        *lane,
    ]


def _review_dispatch_action(record: Mapping[str, Any]) -> str:
    """Return the review dispatch that advances one scoring run."""
    return " ".join(shlex.quote(part) for part in _review_dispatch_argv(record))


def _stored_review(record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Read one run's review without collapsing unreadable data into absence."""
    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    if not run_id or not project:
        return None, ""
    try:
        review = review_module.read_review(project, run_id)
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    if review is not None and not isinstance(review, dict):
        return {}, "stored review is not a JSON object"
    return review, ""


def _review_is_complete(review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review contains every independently scored dimension."""
    if not review or review.get("status") != "parsed":
        return False
    scores = review.get("scores")
    return (
        isinstance(scores, Mapping)
        and set(review_module.REVIEW_DIMENSIONS).issubset(scores)
        and not review.get("absent")
        and isinstance(review.get("total"), int)
        and not isinstance(review.get("total"), bool)
    )


# ── The review reflex: a scoring run runs the command it composed ───────────
# A run that reaches scoring has already had its whole review dispatch composed
# and returned as a string, and leaving it there is what left six runs waiting
# on one workstation at one moment and nine unreconciled across a working day.
# The reflex below runs that command instead of printing it. It is deliberately
# built on the same dispatch a coordinator would type, so every admission check
# — scope, member, follower, context fit, budget — decides the automatic path
# too: an automatic dispatch that bypasses admission is worse than a manual one
# that does not, because nobody is watching it.

# The pointer field recording what the reflex did, so a sweep can tell a review
# it already launched from one it has not, and so a reader can see why a run is
# still in scoring rather than guessing.
REVIEW_DISPATCH_FIELD = "review_dispatch"

# The flight key naming backends a review must never be composed onto. It is a
# routing rule rather than a preference: the composed lane is a fallback list,
# so an exclusion a fallback can step over cannot be honoured.
REVIEW_EXCLUDED_BACKENDS_KEY = "review_excluded_backends"


def _review_in_flight(record: Mapping[str, Any]) -> str:
    """The review run already standing for this scoring run, or empty.

    Two facts are consulted because the durable one fails soft. The dispatch
    record the reflex wrote is the precise answer, but a review launched by a
    coordinator by hand carries no such record; the deterministic node id the
    review dispatch names is, so a hand-launched review is found by identity.
    A recorded run whose pointer is gone is not in flight — the review died —
    and the reflex is free to dispatch again rather than wait on a run that no
    longer exists. A promoted or abandoned review leaves no live pointer, and a
    sweep that reads the missing one as a pointer to inspect raises out of the
    reflex rather than recomposing: the probe would fail on exactly the case it
    was written to route.
    """
    fields = _review_dispatch_fields(record)
    recorded = record.get(REVIEW_DISPATCH_FIELD)
    if isinstance(recorded, Mapping):
        run_id = str(recorded.get("run_id") or "")
        if run_id and runs.pointer_path(run_id).exists() and read_pointer(run_id):
            return run_id
    project = fields["project"]
    if not project:
        return ""
    for pointer in list_live(project=project):
        node = pointer.get("node") or {}
        if str(node.get("id") or "") == fields["node_id"]:
            return str(pointer.get("run_id") or "")
    return ""


def _record_review_dispatch(
    run_id: str,
    *,
    status: str,
    reason: str,
    review_run_id: str = "",
    backend: str = "",
) -> None:
    """Write the reflex's outcome onto the run it acted for.

    A skip is recorded as loudly as a dispatch: a review which ran and wrote
    nothing is indistinguishable from one that was never dispatched, and a
    reflex that fires into that ambiguity re-fires against the same run forever.

    The backend the attempt targeted is recorded beside its outcome, because it
    is the only durable fact that lets the next attempt know which lane already
    dropped this run. A recorded run_id does not carry that: once the review
    dies its pointer is gone, and the run goes back to looking unattempted.
    """
    if not run_id:
        return

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer[REVIEW_DISPATCH_FIELD] = {
            "status": status,
            "reason": reason,
            "run_id": review_run_id or None,
            "backend": backend or None,
            "at": _utc_now(),
            "attempt": int(
                (pointer.get(REVIEW_DISPATCH_FIELD) or {}).get("attempt") or 0
            )
            + 1,
        }
        return pointer

    _mutate_pointer(run_id, record)


def _failed_review_backend(record: Mapping[str, Any]) -> str:
    """The lane a run's most recent recorded attempt used, or empty.

    A recorded attempt that produced neither a stored nor an in-flight review
    has failed, and the caller reaches selection only when neither exists — so
    whatever backend the record names is one this run has already been dropped
    by, and recomposing onto it repeats the attempt rather than advancing it.
    """
    recorded = record.get(REVIEW_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return ""
    return str(recorded.get("backend") or "").strip()


def _review_excluded_backends(config: Mapping[str, Any]) -> set[str]:
    """Backends the flight configuration removes from review routing.

    Read as a rule about which lanes may carry a review rather than a
    preference: it is consulted before any ordering, so a fallback cannot walk
    around it.
    """
    raw = config.get(REVIEW_EXCLUDED_BACKENDS_KEY)
    return {str(name).strip() for name in raw or () if str(name).strip()}


def _review_lane_candidates(
    config: Mapping[str, Any], *, owning_backend: str = ""
) -> list[str]:
    """Configured backends a composed review may run on, in selection order.

    The owning run's recorded backend leads when it is known and not excluded,
    because the review is of that run and the lane that carried it is the one
    its coordinator chose; the locally served backend follows, then the rest in
    a stable alphabetical order. Excluded backends never appear: a coordinator
    that has removed a backend from review routing must not see a fallback land
    on it, or the exclusion is a note rather than a rule.
    """
    backends = config.get("backends") or {}
    excluded = _review_excluded_backends(config)
    names = [str(name) for name in sorted(backends) if str(name) not in excluded]
    local = str(config.get("local_backend") or "").strip()
    owning = str(owning_backend or "").strip()
    ordered: list[str] = []
    for preferred in (owning, local):
        if preferred in names and preferred not in ordered:
            ordered.append(preferred)
    ordered.extend(name for name in names if name not in ordered)
    return ordered


def _no_review_lane_reason(
    run_id: str, previous_lane: str, config: Mapping[str, Any]
) -> str:
    """Why a scoring run has no lane left, naming an exclusion when one applies.

    An exclusion is a rule, so it is worth naming on its own: a reader told only
    that no configured backend remains would look for a lane to add, when what
    the configuration actually says is that the lane is deliberately withheld.
    """
    parts: list[str] = []
    excluded = _review_excluded_backends(config)
    if excluded:
        parts.append(
            f"{REVIEW_EXCLUDED_BACKENDS_KEY} excludes "
            + ", ".join(sorted(excluded))
            + " from review routing"
        )
    if previous_lane:
        parts.append(f"backend {previous_lane!r} already dropped it")
    detail = "; ".join(parts)
    reason = f"the review for {run_id} has no eligible lane"
    return f"{reason} ({detail})" if detail else reason


def _resolved_review_config(
    project: str, config: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """The flight config a review dispatch resolves its local lane against."""
    if config is not None:
        return config
    from reckon import flight

    return flight.resolve(project=project).config


def dispatch_review_for_run(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    allow_unreconciled_runs: bool = True,
) -> dict[str, Any]:
    """Run the review dispatch a scoring run has already composed for itself.

    The return value is the reflex's own report, not a command: ``dispatched``
    says whether a review run is now in flight, ``run_id`` names it, and
    ``reason`` explains a false. Nothing here is raised for an ordinary refusal
    — a scope, member, follower, context-fit or budget refusal is *reported*
    and recorded against the run, because the caller is a sweep that must reach
    the rest of the fleet. The refusal itself still comes from dispatch, so the
    automatic path is refused exactly where a manual dispatch is rather than
    being waved through.

    ``allow_unreconciled_runs`` defaults on because a run awaiting review is
    itself an unreconciled run: past the grace window the fence refuses the
    review dispatch that is the only thing able to clear it, so the automatic
    path would deadlock on the runs it exists for. The waiver is recorded on
    the review run's own pointer by dispatch, naming the runs it waived, so the
    exception stays visible after the command that supplied it is gone.
    """
    run_id = str(record.get("run_id") or "")
    if _is_review_node(record):
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": (
                "the run is itself a review, so dispatching its review would "
                "compose a review of a review"
            ),
        }
    row = classify_pointer(record)
    if row["classification"] != "scoring":
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": f"the run is not awaiting review ({row['classification']})",
        }
    review, review_error = _stored_review(record)
    if review is not None or review_error:
        # A stored review is evidence, readable or not. Regenerating over an
        # unparseable one would discard what the reviewer actually wrote, so
        # the run is left for its coordinator with the reason named.
        return {
            "run_id": run_id,
            "dispatched": False,
            "review_status": "unreadable" if review_error else "present",
            "reason": (
                "a review is already stored for this run and is not a complete "
                "parse; repair or replace it rather than dispatching a second"
            ),
        }
    in_flight = _review_in_flight(record)
    if in_flight:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "a review is already in flight as a live run",
            "review_run_id": in_flight,
        }

    fields = _review_dispatch_fields(record)
    project = fields["project"]
    repo = str(record.get("repo") or "")
    if not project or not repo:
        reason = "the run records no project or repository to dispatch against"
        _record_review_dispatch(run_id, status="refused", reason=reason)
        return {"run_id": run_id, "dispatched": False, "reason": reason}

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    from reckon.crew.dispatch import BudgetHold
    from reckon.crew.node import TaskNode

    resolved = _resolved_review_config(project, config)
    try:
        from reckon import flight

        resolved = flight.select_local_backend(resolved)
    except Exception as exc:  # noqa: BLE001 - the configured lane is the reason
        reason = f"the local lane is unavailable: {exc}"
        _record_review_dispatch(run_id, status="awaiting-lane", reason=reason)
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "reason": reason,
        }

    # The lane is selected rather than asserted: the local one is the
    # preference, and it is dropped when this run already records a failed
    # attempt on it. Without that, a sweep that fires on every completion
    # recomposes the same review onto the lane that just dropped it, which the
    # reflex was measured doing twice in two minutes against a saturated pool.
    local_lane = str(resolved.get("local_backend") or "").strip()
    owning_lane = str(record.get("backend") or "").strip()
    previous_lane = _failed_review_backend(record)
    candidates = [
        name
        for name in _review_lane_candidates(resolved, owning_backend=owning_lane)
        if name != previous_lane
    ]
    if not candidates:
        reason = _no_review_lane_reason(run_id, previous_lane, resolved)
        _record_review_dispatch(
            run_id, status="awaiting-lane", reason=reason, backend=previous_lane
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "backend": previous_lane,
            "reason": reason,
        }
    backend = candidates[0]
    on_local_lane = backend == local_lane

    node = TaskNode(
        id=fields["node_id"],
        goal=fields["goal"],
        plan=fields["plan"],
        section=fields["section"],
        role="review",
        spec_level="exact",
        done_when=fields["done_when"],
        write_paths=[fields["write_path"]],
        time_budget=fields["time_budget"],
    )
    try:
        launched = dispatch_module.dispatch(
            node=node,
            project=project,
            repo=repo,
            config=resolved,
            session=fields["session"],
            launcher=launcher,
            watch_required=True,
            local=on_local_lane,
            backend_override=None if on_local_lane else backend,
            unreconciled_override=allow_unreconciled_runs,
        )
    except BudgetHold as exc:
        reason = f"the {backend} lane is unavailable: {exc}"
        _record_review_dispatch(
            run_id, status="awaiting-lane", reason=reason, backend=backend
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "backend": backend,
            "lane": getattr(exc, "verdict", None),
            "reason": reason,
        }
    except CrewError as exc:
        # Scope, member, follower, context-fit, plan visibility and competence
        # refusals all arrive here. The automatic path must not be the one place
        # they are skipped, so the refusal is recorded and reported rather than
        # caught and shrugged off.
        _record_review_dispatch(
            run_id, status="refused", reason=str(exc), backend=backend
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "refused": True,
            "backend": backend,
            "reason": str(exc),
        }

    review_run_id = str(launched.get("run_id") or "")
    _record_review_dispatch(
        run_id,
        status="dispatched",
        reason=f"the review dispatched automatically as run {review_run_id}",
        review_run_id=review_run_id,
        backend=backend,
    )
    return {
        "run_id": run_id,
        "dispatched": True,
        "backend": backend,
        "review_run_id": review_run_id,
        "reason": f"dispatched the composed review as run {review_run_id}",
    }


def _sweeping_session(project: str | None) -> str:
    """The session whose follower this process serves, or empty.

    A sweep runs inside one session's follower, and the review it composes for
    a run belongs to that run's owning session: composing one for another
    session's run attributes a lane and a member to a coordinator that did not
    choose either, which is a project-wide sweep placing runs under someone
    else's runtime. The follower's registration names the session and the
    process that wrote it, so a registration written by this process is the
    identity. A process holding no registration — a hand-run sweep — has no
    session to confine to and returns empty, which the caller reads as no
    filter.
    """
    if not project:
        return ""
    for row in runs.list_followers(project):
        follower = row.get("follower") or {}
        if follower.get("pid") == os.getpid():
            return str(row.get("session") or "")
    return ""


def dispatch_awaiting_reviews(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Dispatch the review every scoring run composes, and report each outcome.

    This is the reflex's entry point: called on a sweep, it is what makes a run
    entering scoring dispatch its own review without anyone issuing the
    ``reckon crew dispatch`` a coordinator would otherwise have to retype. A
    run whose review is already stored or already in flight is left alone, so
    the sweep is idempotent and the negative half of the property holds — a
    reflex that re-fires would manufacture runs rather than reviews.

    ``session`` names the sweeping session, and only runs whose pointer records
    that ownership: its lane and its member belong to the coordinator that
    chose them. Left unset it is resolved from the follower registration this
    process holds, so a sweep inside a follower is confined to its own session
    without the caller having to declare it.
    """
    reports: list[dict[str, Any]] = []
    dispatched: list[str] = []
    refused: list[dict[str, Any]] = []
    awaiting_lane: list[str] = []
    sweeping = session if session is not None else _sweeping_session(project)
    for pointer in list_live(project=project):
        if (
            str(pointer.get("project") or "")
            and project
            and str(pointer.get("project")) != project
        ):
            continue
        if sweeping and str(pointer.get("session") or "") != sweeping:
            # Another session's run: its review is that session's to compose,
            # because only that coordinator chose its lane and its member.
            continue
        # A review run is never its own source run: counting one as a run
        # awaiting review is what composes a review of a review, and each link
        # of that chain is a real dispatch against a real member.
        if _is_review_node(pointer):
            continue
        scan: dict[str, Any] | None = None
        try:
            scan = classify_pointer(pointer)
        except Exception:  # noqa: BLE001 - one unreadable run must not stop the sweep
            scan = None
        if scan is None or scan["classification"] != "scoring":
            continue
        report = dispatch_review_for_run(pointer, config=config, launcher=launcher)
        reports.append(report)
        if report.get("dispatched"):
            dispatched.append(str(report.get("review_run_id") or ""))
        elif report.get("awaiting_lane"):
            awaiting_lane.append(str(report.get("run_id") or ""))
        elif report.get("refused"):
            # The report itself, not a generator over it: a refusal list is
            # read and serialized by whoever consumes the sweep, and a
            # generator is neither readable nor JSON-serializable, so the
            # refusal would be lost at exactly the moment a reader needs to
            # know which lane was refused.
            refused.append(report)
    return {
        "reports": reports,
        "dispatched": dispatched,
        "refused": refused,
        "awaiting_lane": awaiting_lane,
    }


SELF_LIFTING_RECOVERY_CLASSIFICATIONS = frozenset({"waiting", "paused"})
DEFAULT_LIFTING_CONDITIONS = {
    "waiting": "the declared condition reaches one of its terminal states",
    "paused": "the condition named by the row ends",
}


def manifest_status_is_terminal(value: Any) -> bool:
    """Whether a worker supplied one exact terminal status value."""
    status = str(value or "").strip().lower()
    return not manifest_status_is_template(status) and (
        status in TERMINAL_MANIFEST_STATUSES
    )


def _stream_completion_stamp(record: Mapping[str, Any]) -> str | None:
    """The run's own finish stamp as its recorded stream dates it, else None.

    Promotion writes this same stream completion stamp to the ledger row, so
    the elapsed measure and the promoted record agree on when a run ended
    rather than each keeping its own idea of the finish. None for an in-harness
    run or a stream that was never written; the caller only resolves it once
    liveness says the process is gone, so a still-writing stream is never
    mistaken for a completion.
    """
    if record.get("launch") != "cli":
        return None
    from reckon.crew.promotion import _terminal_stream_data

    return _terminal_stream_data(record).completed_at


def _declared_token_budget(record: Mapping[str, Any]) -> int | None:
    """The run's token-denominated allowance, or None when none is set.

    The budget lives on the node block as dispatch resolves and records it;
    a top-level mirror is accepted as a fallback so a hand-built or imported
    record that carries the value at the pointer root still reads it. A value
    that does not coerce to a positive integer is treated as unset rather
    than as a charge surface, so a malformed declaration degrades to the
    wall-clock behaviour instead of refusing to measure.
    """
    node = record.get("node")
    value = node.get("token_budget") if isinstance(node, Mapping) else None
    if value is None:
        value = record.get("token_budget")
    if value is None or value == "":
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return None
    return budget if budget > 0 else None


def _generated_tokens(record: Mapping[str, Any]) -> int | None:
    """The run's recorded generated output tokens, or None when unmeasured.

    observe() folds the stream's measured throughput block into the pointer,
    so a run that has been observed carries its token total here. Absence is
    not a verdict: an unmeasured run is charged nothing, matching how a run
    with no stream is never called an overrun on elapsed either.
    """
    throughput = record.get("throughput")
    if not isinstance(throughput, Mapping):
        return None
    value = throughput.get("generated_tokens")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_budget_timing(
    token_budget: int,
    generated_tokens: int | None,
    *,
    budget_seconds: int | None,
    elapsed_seconds: int | None,
) -> dict[str, Any]:
    """Measure a run against a token budget, keeping the seconds ceiling.

    The worker's own budget is denominated in generated tokens — the quantity
    the same task needs regardless of what else the lane is doing — so a slow
    lane inside its token budget is not an overrun however long it took, and
    a lane that delivered more tokens than the allowance is charged for the
    work. Wall clock cannot bound a process that stopped producing, so the
    seconds allowance survives here under its own name as the ceiling that
    still refuses such a run; the two verdicts never share a name.
    """
    wall_overrun = (
        max(0, int(elapsed_seconds) - int(budget_seconds))
        if elapsed_seconds is not None and budget_seconds is not None
        else 0
    )
    if generated_tokens is None:
        token_overrun = 0
    else:
        token_overrun = max(0, generated_tokens - token_budget)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed_seconds,
        "budget_overrun": generated_tokens is not None and token_overrun > 0,
        "budget_overrun_seconds": wall_overrun,
        "budget_tokens": token_budget,
        "generated_tokens": generated_tokens,
        "budget_overrun_tokens": token_overrun,
        "hang_ceiling_seconds": budget_seconds,
        "ceiling_overrun": wall_overrun > 0,
    }


def _budget_timing(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> dict[str, Any]:
    """Measure one run against its declared allowance without mutating it.

    A run whose worker process is gone has finished, so its elapsed is measured
    to its own stream completion — the same stamp promotion records — rather
    than to the moment of reading. A still-running run measures to now, and the
    wall-clock ceiling that protects the fleet from a hang is untouched because
    a live process still anchors here. A reader resolving a run late therefore
    reports the worker's own time, not the coordinator's wait to promote it.

    When the run carries a token budget, the budget verdict is denominated in
    generated tokens (the worker is charged for the work, not the queue) and
    the wall-clock allowance becomes the separately named hang ceiling. Without
    one, the wall-clock overrun is the only verdict, unchanged.
    """
    node = record.get("node") or {}
    token_budget = _declared_token_budget(record)
    try:
        if "attempt_budget_seconds" in record:
            budget_seconds = int(record["attempt_budget_seconds"])
        else:
            budget_seconds = parse_duration(str(node.get("time_budget") or ""))
        started = datetime.fromisoformat(
            str(
                record.get("attempt_started_at") or record.get("created_at") or ""
            ).replace("Z", "+00:00")
        )
    except (CrewError, TypeError, ValueError):
        if token_budget is not None:
            return _token_budget_timing(
                token_budget,
                _generated_tokens(record),
                budget_seconds=None,
                elapsed_seconds=None,
            )
        return {
            "budget_seconds": None,
            "elapsed_seconds": None,
            "budget_overrun": False,
            "budget_overrun_seconds": 0,
        }
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    elapsed_to = None
    if record.get("process_alive") is False:
        try:
            completion = _stream_completion_stamp(record)
        except (CrewError, OSError):
            completion = None
        if isinstance(completion, str) and completion:
            try:
                finished = datetime.fromisoformat(completion)
            except ValueError:
                finished = None
            else:
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=UTC)
                elapsed_to = finished.timestamp()
    if elapsed_to is None:
        elapsed = max(0, int(moment - started.timestamp()))
    else:
        elapsed = max(0, int(elapsed_to - started.timestamp()))
    if token_budget is not None:
        return _token_budget_timing(
            token_budget,
            _generated_tokens(record),
            budget_seconds=budget_seconds,
            elapsed_seconds=elapsed,
        )
    overrun = max(0, elapsed - budget_seconds)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed,
        "budget_overrun": overrun > 0,
        "budget_overrun_seconds": overrun,
    }


def _apply_budget_watchdog(
    record: dict[str, Any], config: Mapping[str, Any] | None
) -> None:
    """Record deadline posture and optionally stop an over-grace CLI worker."""
    timing = _budget_timing(record)
    record.update(timing)
    fences = (config or {}).get("fences") or {}
    if not fences.get("enforce_budget_watchdog"):
        return
    budget_seconds = timing["budget_seconds"]
    elapsed_seconds = timing["elapsed_seconds"]
    try:
        grace = float(fences.get("budget_grace_multiple", 1.0))
    except (TypeError, ValueError):
        return
    if (
        budget_seconds is None
        or elapsed_seconds is None
        or elapsed_seconds <= budget_seconds * grace
        or record.get("launch") != "cli"
        or record.get("phase") in _TERMINAL_RUN_PHASES
        or record.get("process_alive") is not True
    ):
        return
    pid = record.get("pid")
    try:
        _signal_process_group(int(pid), record.get("pid_start_time"))
    except (
        CrewError,
        ProcessLookupError,
        PermissionError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        record["watchdog_detail"] = f"budget watchdog could not stop pid {pid}: {exc}"
        return
    record["phase"] = "stopped"
    record["stopped_at"] = _utc_now()
    record["watchdog_enforced"] = True
    record["detail"] = (
        f"budget watchdog stopped pid {pid} after {elapsed_seconds}s "
        f"against {budget_seconds}s with {grace:g}x grace"
    )


def _refusal_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalise a refusal budget block into the fields a blocked reason needs."""
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "quota"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _harness_command(record: Mapping[str, Any], argv: Any) -> str | None:
    """The command that names a cli run's harness, for a stream translation.

    A placed launch prefixes its resolved argv with the scheduler invocation, so
    ``argv[0]`` on such a record names the scheduler rather than the harness and
    a translation built from it fails. The record carries the harness the launch
    resolved under ``command``, captured before the placement wrapped the plan,
    so that field is taken first and ``argv[0]`` is the fallback for a record
    written before the field existed.
    """
    command = record.get("command")
    if command:
        return str(command)
    if isinstance(argv, list) and argv:
        return str(argv[0])
    dialect = record.get("dialect")
    return str(dialect) if dialect else None


def _stream_budget(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The budget block a cli run's stream records, folded in or read fresh.

    Shared by the refusal and retry-shape gates so the stream is parsed once
    even when both are consulted for the same run. observe() folds the stream's
    budget into the pointer, while the ticker reads raw pointers that have not
    been through observe; both paths resolve through the same backend
    translation, so they reach the same block and a ticker reading a raw
    pointer cannot disagree with observe's phase.
    """
    budget = record.get("budget")
    if isinstance(budget, Mapping) and budget.get("refusal"):
        return budget
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    argv = record.get("argv")
    command = _harness_command(record, argv)
    if not command:
        return None
    from reckon import _backends

    try:
        observation = _backends.observe_log(
            backend_name=str(record.get("backend") or ""),
            backend={"command": command},
            log_path=log,
        )
    except (_backends.BackendError, CrewError, OSError, ValueError):
        # An unreadable or untranslatable stream carries no readable budget;
        # the manifest and liveness paths still classify the run.
        return None
    observed = observation.as_dict().get("budget") or {}
    return observed or None


def _stream_refusal_block(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider refusal a cli run's stream records, folded in or read fresh.

    A spend or usage refusal is a block, not an abandonment: the account is not
    broken, only spent until a moment the refusal names. The block comes from
    the same budget the retry-shape gate reads, so the two dead-lane readings
    agree on one stream rather than each owning a separate translation.

    Declining is the load-bearing half. A stream that reports an ordinary
    failed turn — a bad model id, a lost stream, a context overflow — carries
    none of the recognised limit phrases and returns None, so a crash is never
    mistaken for a block.
    """
    budget = _stream_budget(record)
    if budget is not None and budget.get("refusal"):
        return _refusal_block(record, budget)
    return None


# A spent local lane's mid-flight shape, folded from the budget block the
# stream observer wrote: rate-limit retries counted with no terminal result, so
# the number is a magnitude and liveness is the verdict. The exhaustion shape
# (retries ended in a terminal error result) reads as a refusal instead, so the
# two dead-lane readings never overlap.
_RATE_LIMIT_RETRY_RE = re.compile(r"after (\d+) rate-limit retries")

# An exhausted unmetered lane folds no refusal at all: the observer writes
# refusal false with lane_backpressure true and a detail naming the retry count
# ("run died after N consumer-queue retries ...; the lane refused"), because a
# lane without a budget has nothing to refuse from. The marker is what a run
# that retried and recovered never carries, so it discriminates terminal
# exhaustion from routine retries.
_BACKPRESSURE_RETRY_RE = re.compile(r"run died after (\d+) consumer-queue retries")


def _stream_exhaustion_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Terminal retry exhaustion on an unmetered lane, as a refusal block.

    A spent unmetered consumer ends its retries in an error result, and the
    budget observer surfaces that terminal shape as ``lane_backpressure`` true
    with the retry count in the detail — not as a budget refusal, because the
    lane's budget is not what was spent. The marker is absent on a run that
    retried and recovered, so no block is reached for a live or successful run;
    only classify_pointer's dead-process hand joins the marker into a blocked
    reading, so the row names the lane and offers resume. ``budget`` is the
    block :func:`_stream_budget` already resolved, so the stream is parsed once
    regardless of which gates consult it.
    """
    if budget.get("refusal"):
        return None
    if not budget.get("lane_backpressure"):
        return None
    detail = str(budget.get("detail") or "")
    match = _BACKPRESSURE_RETRY_RE.search(detail)
    if match is None:
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": "rate-limit",
        "resets_at": None,
        "retries": int(match.group(1)),
    }


def _stream_retry_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The mid-flight rate-limit retry shape budget carries, else None.

    The local lane reports a spent consumer as rate-limit ``api_retry`` records,
    and the budget observer surfaces the count in the block's detail with no
    terminal result ("no terminal result yet"). From the stream alone that
    shape is indistinguishable from a live worker mid-retry-burst, so no verdict
    is reached here: the block names the lane and the count, and only
    classify_pointer's dead-process hand joins it into a blocked reading. An
    alive worker mid-retry-burst (measured completing with seven retries) reads
    running, not blocked. ``budget`` is the block :func:`_stream_budget` already
    resolved, so the stream is parsed once regardless of which gates consult it.
    """
    if budget.get("refusal"):
        return None
    detail = str(budget.get("detail") or "")
    if "no terminal result yet" not in detail:
        return None
    match = _RATE_LIMIT_RETRY_RE.search(detail)
    if match is None:
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "rate-limit"),
        "retries": int(match.group(1)),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


# The client substitutes this exact string into a message's model field when no
# model served the turn, so it is a marker rather than a model name.
_SYNTHETIC_MODEL = "<synthetic>"


def _assistant_refusal_text(message: Mapping[str, Any]) -> str:
    """The prose of a synthetic assistant message, or the empty string."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return " ".join(parts).strip()


def _result_turned_no_tokens(event: Mapping[str, Any]) -> bool:
    """Whether a result record reports an error turn that generated nothing.

    Every token counter zero and a zero API duration are what separate a turn
    the client refused before dispatching from one that ran and then failed —
    a failed turn still reports the tokens it spent and the API duration it
    waited on.
    """
    if event.get("is_error") is not True:
        return False
    try:
        if int(event.get("duration_api_ms") or 0) != 0:
            return False
        if int(event.get("num_turns") or 0) > 1:
            return False
    except (TypeError, ValueError):
        return False
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return False
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        try:
            if int(usage.get(key) or 0) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _admission_refusal(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The marks of a run the backend refused before serving its first turn.

    A refusal at admission ends the run in three lines: an assistant record
    whose model is the client's substitution for "no model served this turn"
    (the literal ``<synthetic>``), carrying ``error: invalid_request`` with the
    reason it refused; and a result record whose terminal reason is
    ``blocking_limit`` with a zero API duration and every token counter zero.
    Together they say no model was reached at all, which is a different stop
    from a worker whose process died mid-turn. The generic dead-process
    classification cannot say which happened, so this one names it and carries
    the paths a reader acts on.

    Requiring the zero-token result beside the synthetic message is deliberate:
    a stream that merely mentions the same words while doing real work returns
    None, and an ordinary failed turn — which reports the tokens it spent —
    cannot reach this reading. None means the ordinary dead-process arms
    classify the run, so this gate never widens them.
    """
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    refusal_reason = ""
    terminal_reason = ""
    zero_token_error = False
    try:
        with log.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                kind = str(event.get("type") or "")
                if kind == "assistant":
                    message = event.get("message")
                    if not isinstance(message, Mapping):
                        continue
                    if str(message.get("model") or "") != _SYNTHETIC_MODEL:
                        continue
                    if str(event.get("error") or "") != "invalid_request":
                        continue
                    text = _assistant_refusal_text(message)
                    if text:
                        refusal_reason = text
                elif kind == "result":
                    terminal_reason = str(event.get("terminal_reason") or "")
                    if _result_turned_no_tokens(event):
                        zero_token_error = True
    except OSError:
        return None
    if (
        not refusal_reason
        or terminal_reason != "blocking_limit"
        or not zero_token_error
    ):
        return None
    return {
        "reason": refusal_reason,
        "terminal_reason": terminal_reason,
    }


def _budget_hold_block(
    record: Mapping[str, Any], budget: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A rate-limit event that rejected the turn, as a hold that ages out.

    A metered harness reports a spent window as ``rate_limit_event`` with
    ``status: rejected`` long before any prose refusal appears: the request was
    refused, the window names itself, and its reset is the moment time lifts the
    hold. This is distinct from :func:`_stream_refusal_block`, which reads a
    terminal prose or retry-exhaustion refusal, so the two never compete for the
    same run — a rejected window carries ``refusal`` false and reaches only
    this gate, while a refusal block is read through the other. ``budget`` is
    the block :func:`_stream_budget` already resolved.
    """
    if budget is None or budget.get("refusal"):
        return None
    if str(budget.get("threshold_status") or "").casefold() != "rejected":
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "rate-limit"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _blocked_session_resolution(
    record: Mapping[str, Any], run_id: str
) -> dict[str, Any]:
    """Resolve a blocked run's session without changing its evidence.

    Session resolution already has one ordered authority spanning the live
    pointer, the run's stream, and its promoted ledger row. Importing it only
    when a block needs a session answer avoids making routine classification consult
    durable history, while keeping this read pure: neither the pointer nor any
    of its evidence is rewritten here.
    """
    from reckon.crew.resumption import resolve_session

    return resolve_session(
        run_id,
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )


def _resume_remedy(resolution: Mapping[str, Any], run_id: str) -> dict[str, str] | None:
    """Return an executable recovery command when session evidence exists."""
    if not resolution.get("resolved"):
        return None
    return {
        "command": (f"reckon crew resume --run {run_id} --advice continue"),
        "session_id": str(resolution["session_id"]),
        "source": str(resolution["source"]),
    }


# A print-mode invocation makes exactly one turn and exits when it ends, so a
# worker still waiting on a background task at that moment leaves one of two
# traces rather than a clean result. The ceiling message is the harness's own
# stderr line when it gave up waiting and terminated the task itself. The
# duration is read from the environment and varies, so only the sentence
# around it is fixed.
_BACKGROUND_WAIT_CEILING_RE = re.compile(
    r"Background tasks still running after \d+s; terminating\.\s*"
    r"Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely\.",
)
# The agent's own last words when its turn ended before the background work
# it was waiting on did. Matched loosely around the fixed clause so a run
# naming a different suite or task still recognises the same shape.
_BACKGROUND_WAIT_FINAL_MESSAGE_RE = re.compile(
    r"waiting for (the )?background .+? before finalizing the manifest",
    re.IGNORECASE | re.DOTALL,
)


def _background_wait_signal(record: Mapping[str, Any]) -> str | None:
    """The one sentence proving a vanished process was waiting on background work.

    A dead process with no complete manifest is indistinguishable from one
    that simply crashed, unless the run directory itself says otherwise. Two
    traces say otherwise: the harness's own ceiling message on stderr, or the
    agent's last turn stating in its own words that it was waiting on
    background work before finalizing the manifest — with nothing after that
    turn because a print-mode invocation has no next one to write. Neither is
    a crash; both name a run whose session is intact and whose only
    outstanding step is a resume long enough to collect the manifest it was
    already about to write.
    """
    stderr_path = record.get("stderr_path")
    if stderr_path:
        try:
            stderr_text = Path(str(stderr_path)).read_text()
        except OSError:
            stderr_text = ""
        if _BACKGROUND_WAIT_CEILING_RE.search(stderr_text):
            return (
                "the worker's stderr recorded the background-wait ceiling "
                "before the process terminated"
            )

    final_message = str(record.get("final_message") or "")
    if not final_message and record.get("launch") == "cli":
        # observe() folds the stream's final message onto the pointer, but a
        # caller reading the raw pointer — the watch producer's path — has
        # none of it cached yet. Reading the log directly keeps that path
        # answering the same question the folded record would.
        log = Path(str(record.get("log_path") or ""))
        if log.is_file():
            argv = record.get("argv")
            command = _harness_command(record, argv)
            if command:
                from reckon import _backends

                try:
                    observation = _backends.observe_log(
                        backend_name=str(record.get("backend") or ""),
                        backend={"command": command},
                        log_path=log,
                    )
                except (_backends.BackendError, CrewError, OSError, ValueError):
                    observation = None
                if observation is not None:
                    final_message = str(
                        observation.as_dict().get("final_message") or ""
                    )

    if final_message and _BACKGROUND_WAIT_FINAL_MESSAGE_RE.search(final_message):
        return (
            "the worker's last turn reported waiting on background work "
            f"before finalizing the manifest: {final_message.strip()}"
        )
    return None


# A tool call whose own contract ends the wait. A quiet stream is read as a hang
# unless the last thing the worker asked for was something that ends on its own:
# a bounded sleep, the peer channel's bounded read, or a task wait that cannot
# outlive its window. Only the last assistant turn is consulted, so a hang that
# follows an earlier sleep still reads as a hang.
_BOUNDED_WAIT_TOOL_NAMES = frozenset({"TaskOutput", "TaskOutputFull", "ScheduleWakeup"})
# The double dash takes no leading word boundary — ``--wait`` follows a space,
# and neither is a word character — so only the trailing boundary is anchored.
_PEER_CHANNEL_WAIT_RE = re.compile(r"peer-read[^\n]*--wait\b", re.IGNORECASE)
_SLEEP_RE = re.compile(r"(?:\btime\.)?\bsleep\s+(\d+)", re.IGNORECASE)


def _last_bounded_wait(record: Mapping[str, Any]) -> str | None:
    """The last tool call's bounded wait, named, or None when there is none.

    Reads only the tool call the worker most recently started, because that is
    the call a quiet stream is currently sitting in. A poll loop that sleeps is
    bounded by its own sleeps; a peer-channel read with a ``--wait`` is bounded
    by that duration; a task wait is bounded by its own contract. None of these
    need a person — each wakes itself, which is what separates them from a hang.
    """
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    try:
        with log.open(encoding="utf-8", errors="replace") as handle:
            last_name = ""
            last_command = ""
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                message = event.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        not isinstance(block, Mapping)
                        or block.get("type") != "tool_use"
                    ):
                        continue
                    name = str(block.get("name") or "")
                    command = ""
                    if isinstance(block.get("input"), Mapping):
                        command = str(
                            block["input"].get("command")
                            or block["input"].get("prompt")
                            or ""
                        )
                    last_name, last_command = name, command
    except OSError:
        return None
    if not last_name and not last_command:
        return None
    if last_name in _BOUNDED_WAIT_TOOL_NAMES:
        return f"a {last_name} task wait"
    if _PEER_CHANNEL_WAIT_RE.search(last_command):
        return "a peer-channel read with a bounded wait"
    match = _SLEEP_RE.search(last_command)
    if match is not None:
        return f"a {match.group(1)}s sleep"
    return None


def _stall_wait_reason(record: Mapping[str, Any]) -> str | None:
    """Why a quiet, alive run is paused rather than hung, or None.

    Three shapes turn a quiet stream into a wait instead of a stall: the worker
    is mid-retry on a rate limit (the lane's window resets on its own), its
    last request was refused on a rate-limit window that resets, or its last
    tool call was a bounded wait (it wakes itself). A run with none of these is
    genuinely hung and must stay stalled, and a live rate-limit retry loop that
    is still emitting keeps reading as working — only a quiet one is arbitrated
    here.
    """
    budget = _stream_budget(record)
    if budget is not None and not budget.get("refusal"):
        retry = _stream_retry_block(record, budget)
        if retry is not None:
            return (
                f"a rate-limit retry loop ({retry['retries']} retries); "
                "the lane's window resets and the loop keeps the session alive"
            )
        hold = _budget_hold_block(record, budget)
        if hold is not None:
            return (
                f"a rejected {hold['limit_kind']} window that resets "
                f"{hold['resets_at']}"
            )
    return _last_bounded_wait(record)


def _wait_probe(value: Any) -> list[str]:
    """Read a shell-free argument vector from a waiting manifest."""
    if isinstance(value, list):
        probe = value
    else:
        try:
            probe = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(probe, list) or not probe:
        return []
    if any(not isinstance(item, str) or not item.strip() for item in probe):
        return []
    return [item.strip() for item in probe]


# The shapes a wait declaration's condition can take, named wherever one is
# refused. Three workers wrote the wait block three wrong ways in one hour, each
# with the right key names and a value the reader discarded: the run then read
# to a coordinator as a worker that had declared nothing, so the repair could
# not be made from the row. A reader that silently reduces an unrecognised shape
# to the absence of a declaration is the defect; naming what it does accept in
# the refusal is what removes it.
_WAIT_ACCEPTED_SHAPES = (
    "the reader accepts a shell-free argument vector whose first element is a "
    "bare program name, or a file condition declared as wait_file with one "
    "path or a JSON array of paths"
)


def _wait_file_paths(value: Any) -> list[str]:
    """Read the paths a file condition names.

    One path is a plain scalar and several are a JSON array, the same pair of
    forms the terminal list accepts -- except that a scalar is never
    comma-split here, because a comma inside a path is part of the path and a
    reader that split it would invent two paths that do not exist. A value that
    is present and is neither of those forms reads as no paths, so a caller
    telling the shapes apart can refuse it rather than read it as an undeclared
    condition.
    """
    if isinstance(value, list):
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return []
        return [item.strip() for item in value]
    text = str(value or "").strip()
    if not text:
        return []
    if not text.lstrip().startswith("["):
        return [text]
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        return []
    return [item.strip() for item in parsed]


def _wait_probe_shape_refusal(value: Any) -> str:
    """The reason a declared probe is not a shape the reader can run, or "".

    Absence is not a refusal: a declaration carrying no probe is incomplete,
    which the reader reports by naming the missing field, and the two outcomes
    stay distinguishable. Refused is a probe that is present and unreadable,
    because that is the shape that used to reduce silently to the absence of a
    probe -- and a worker reading its own row was then told it had declared
    nothing when it had declared something.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    candidate: Any = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return (
                "wait_probe is a single value the reader cannot parse as a "
                f"JSON array; {_WAIT_ACCEPTED_SHAPES}"
            )
    if not isinstance(candidate, list):
        return f"wait_probe is a scalar rather than a list; {_WAIT_ACCEPTED_SHAPES}"
    if not candidate:
        return ""
    if any(not isinstance(item, str) or not item.strip() for item in candidate):
        return (
            "wait_probe is a list of mappings rather than of program "
            f"arguments; {_WAIT_ACCEPTED_SHAPES}"
        )
    first = candidate[0].strip()
    if not first or " " in first or "\t" in first:
        return (
            f"wait_probe starts with {first!r}, a whole command line rather "
            f"than a program name, so nothing can exec it; {_WAIT_ACCEPTED_SHAPES}"
        )
    return ""


def _wait_file_shape_refusal(value: Any) -> str:
    """The reason a declared file condition is not a shape the reader reads."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return (
                "wait_file is a list of mappings rather than of paths; "
                f"{_WAIT_ACCEPTED_SHAPES}"
            )
        return ""
    if isinstance(value, str):
        if not value.lstrip().startswith("["):
            return ""
        if not _wait_file_paths(value):
            return (
                "wait_file opens with a bracket but does not parse as a JSON "
                f"array of paths; {_WAIT_ACCEPTED_SHAPES}"
            )
        return ""
    return f"wait_file is neither a path nor a list of paths; {_WAIT_ACCEPTED_SHAPES}"


# A terminal value names a state the probe prints. The exit-code sentinel is
# not one: the observation the reader matches against is the probe's own output,
# so a declaration whose terminal is an exit status reads as pending on every
# sweep of a job that has already ended, and the run never lifts.
_WAIT_EXIT_SENTINEL = re.compile(r"exit:\s*\d+\s*$", re.IGNORECASE)


def _wait_terminal_names_no_probe_state(terminal: Sequence[str]) -> str:
    """Name a terminal value that is not a state any probe prints, or ""."""
    for value in terminal:
        spelled = str(value).strip()
        if spelled and _WAIT_EXIT_SENTINEL.match(spelled):
            return spelled
    return ""


def _wait_file_probe(files: Sequence[str]) -> list[str]:
    """The shell-free argument vector a file condition derives.

    The vector is the shape a file condition takes so it reads like every other
    wait: ``test -e`` per path joined by ``-a`` exits 0 exactly when every
    declared path exists, and the terminal the declaration needs is the
    ``exit:0`` sentinel that exit status prints.

    Two readers exist, and only one of them runs this vector. The sweep's
    reader in ``reckon/crew/resumption.py`` executes it and is what decides a
    lift, so this vector is the load-bearing half for a park. The classifier's
    reader -- ``_run_wait_condition_probe`` below -- answers a file condition by
    looking for the paths themselves and returns before any vector runs, so a
    row can name which paths are still missing. The two agree on the answer and
    not on the mechanism; nothing here is run by the classifier.
    """
    argv = ["test"]
    for index, path in enumerate(files):
        if index:
            argv.append("-a")
        argv.extend(["-e", path])
    return argv


def _wait_terminal_values(value: Any) -> list[str]:
    """Read the external states that mean a condition has terminated.

    A terminal-state list is read from the same forms the probe accepts: a
    list, or a JSON array written as a string, each validated the same way --
    a list whose members are all non-empty strings. A value whose first
    non-space character is an opening square bracket is read as JSON only:
    when it parses to a list of non-empty strings those strings are the
    states, and when it does not parse the result is no states at all, so a
    manifest written that way is reported as an incomplete wait declaration
    rather than honoured with state names carrying JSON punctuation. The
    comma-separated form keeps working for briefs and manifests in flight,
    and is never applied to a value that opens with a bracket: comma-splitting
    a malformed array is what produces a state name containing a bracket.
    """
    if isinstance(value, list):
        if any(not isinstance(i, str) or not i.strip() for i in value):
            return []
        states = value
    else:
        text = str(value or "")
        if text.lstrip().startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return []
            if not isinstance(parsed, list) or any(
                not isinstance(i, str) or not i.strip() for i in parsed
            ):
                return []
            states = parsed
        else:
            states = text.split(",")
    return [str(item).strip() for item in states if str(item).strip()]


def _wait_condition_observation(
    value: Any, *, terminal_values: list[str]
) -> dict[str, str]:
    """Normalise a probe result into pending, met, or unknown."""
    if isinstance(value, bool):
        return {
            "state": "met" if value else "pending",
            "observed": "terminal" if value else "pending",
            "detail": "condition test returned a boolean verdict",
        }
    if isinstance(value, Mapping):
        state = str(value.get("state") or "").strip().lower()
        if state not in WAIT_CONDITION_STATES:
            terminal = value.get("terminal")
            state = (
                "met"
                if terminal is True
                else "pending"
                if terminal is False
                else "unknown"
            )
        return {
            "state": state,
            "observed": str(value.get("observed") or state),
            "detail": str(value.get("detail") or "condition test returned a verdict"),
        }
    observed = str(value or "").strip()
    terminal = {item.casefold() for item in terminal_values}
    return {
        "state": "met" if observed.casefold() in terminal else "unknown",
        "observed": observed or "unavailable",
        "detail": "condition test returned an unstructured observation",
    }


def _wait_file_condition_observation(
    record: Mapping[str, Any], files: Sequence[str]
) -> dict[str, str]:
    """Read a file condition by looking for the paths it declares.

    The condition is met when every path exists, and the paths still missing
    are named, so a row says which one the wait is on rather than only that
    something is absent. A relative path resolves against the run's worktree,
    which is where the worker that declared it was running.
    """
    worktree = Path(str(record.get("worktree") or "."))
    root = worktree if worktree.is_dir() else Path(".")

    def _resolved(path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else root / candidate

    missing = [path for path in files if not _resolved(path).exists()]
    if missing:
        return {
            "state": "pending",
            "observed": "absent",
            "detail": (
                f"{len(missing)} of {len(files)} declared paths are not there "
                f"yet: {', '.join(missing)}"
            ),
        }
    return {
        "state": "met",
        "observed": "present",
        "detail": f"all {len(files)} declared paths exist",
    }


def _run_wait_condition_probe(
    record: Mapping[str, Any], wait: Mapping[str, Any]
) -> dict[str, str]:
    """Answer a declared condition for a classifier row, as a tri-state.

    This is the classifier's reader, not the sweep's. Two things separate them,
    and both are deliberate:

    * A file condition is answered here by looking for each declared path, so
      the row can name the ones still missing, and this function returns before
      any argument vector runs. The vector the declaration derives is run by
      the sweep's reader in ``reckon/crew/resumption.py`` -- the reader that
      decides whether a park lifts -- and not here.
    * A vector that prints nothing and exits is a terminal state only for the
      sweep's reader, which falls back to ``exit:<code>`` as the worker
      protocol documents. Here an empty answer is ``unknown``: a classifier row
      is read by a person, and reporting a state the probe never printed would
      have the row assert more than the probe said.

    The two are therefore not one implementation under two names, and a caller
    must not treat them as interchangeable: a verdict from here reaches a
    reader, a verdict from the sweep's reader lifts a run.
    """
    files = [str(path) for path in (wait.get("files") or ())]
    if files:
        return _wait_file_condition_observation(record, files)
    worktree = Path(str(record.get("worktree") or "."))
    try:
        completed = subprocess.run(
            list(wait.get("probe") or ()),
            cwd=worktree if worktree.is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=WAIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "state": "unknown",
            "observed": "unavailable",
            "detail": f"condition probe could not answer: {exc}",
        }
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    observed = lines[-1] if lines else "unavailable"
    if completed.returncode != 0 or not lines:
        return {
            "state": "unknown",
            "observed": observed,
            "detail": (
                f"condition probe did not answer successfully; exit "
                f"{completed.returncode}"
            ),
        }
    candidates = {observed.casefold()}
    candidates.update(
        line.split(maxsplit=1)[0].rstrip("+").casefold() for line in lines
    )
    terminal = {str(value).strip().casefold() for value in wait.get("terminal") or ()}
    if candidates & terminal:
        return {
            "state": "met",
            "observed": observed,
            "detail": f"condition probe reported terminal state {observed!r}",
        }
    return {
        "state": "unknown",
        "observed": observed,
        "detail": (
            f"condition probe reported {observed!r}, which matches no declared "
            "terminal state"
        ),
    }


_WAIT_HORIZON_FIELDS = (
    "wait_expected_seconds",
    "wait_expected",
    "wait_horizon",
)
_WAIT_DECLARATION_SCALAR_FIELDS = (
    *_WAIT_HORIZON_FIELDS,
    "wait_started_at",
)


def _unquote_wait_declaration_scalar(value: Any) -> str:
    """Return a scalar value after removing one matched pair of quotes."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _wait_expected_seconds(
    manifest_data: Mapping[str, Any], *, default_seconds: int
) -> tuple[int, str]:
    """Read a declared wait horizon, retaining the existing default if absent."""
    field = ""
    value: Any = None
    for candidate in _WAIT_HORIZON_FIELDS:
        if candidate in manifest_data:
            field = candidate
            value = manifest_data.get(candidate)
            break
    if not field:
        return int(default_seconds), ""
    try:
        if field == "wait_expected_seconds" and not isinstance(value, str):
            seconds = int(value)
        else:
            seconds = parse_duration(_unquote_wait_declaration_scalar(value))
    except (CrewError, TypeError, ValueError):
        return int(default_seconds), f"readable positive {field}"
    if seconds <= 0:
        return int(default_seconds), f"positive {field}"
    return seconds, ""


def _wait_condition_declares_no_wait(condition: str) -> bool:
    """True when the condition's own prose says there is nothing to wait on.

    A worker told to write its manifest before starting long output writes its
    first one at orientation, and a healthy worker offered wait fields at that
    moment fills them with a note about where it is rather than what is awaited.
    The same happens later and on purpose: a worker recording a durable
    checkpoint before a long compose step writes the fields deliberately and
    says so in the condition. Recorded notes read: a condition opening with the
    word ``none`` ("none - this is an interim checkpoint, not a held wait"),
    read here exactly as the changed-paths prose-none rule reads that word — the
    sentence must *open* with it, so a real condition that merely mentions
    ``none`` later is left alone; the phrase written while the condition is
    still unestablished ("exploring; not yet set"); and a sentence stating in
    plain English that nothing is being awaited, in either wording a worker
    reached for: "no external condition is awaited; this is an interim progress
    record written before the report is composed", and the ledger's own
    "no external resource is awaited - this first write records orientation
    before the first edit". Every one of those declared its own absence, and
    each was escalated anyway on the presence of the field alone.
    """
    text = condition.strip()
    if re.match(r"none(?:\s|$)", text, re.IGNORECASE):
        return True
    if re.search(r"\bnot yet set\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bno\s+external\b[^.;]{0,60}\bawaited\b", text, re.IGNORECASE):
        return True
    return bool(
        re.search(r"\bnothing\s+(?:is\s+|to\s+be\s+)?awaited\b", text, re.IGNORECASE)
    )


# A probe that cannot report a pending state is not a probe: the null command
# succeeds whatever is happening and prints nothing, so a declaration resting on
# it reads terminal on every sweep, however completely the other fields are
# filled in.
_WAIT_PROBE_NO_OP_COMMANDS = frozenset({"true", ":", "exit"})


def _wait_probe_is_a_no_op(probe: Sequence[str]) -> bool:
    """True when a present probe can report nothing but success.

    Only a probe that is actually present is read this way: an absent one is
    the incomplete-declaration case the reader already reports, so the two
    outcomes stay distinguishable.
    """
    if not probe:
        return False
    return Path(str(probe[0])).name in _WAIT_PROBE_NO_OP_COMMANDS


# A state that means the awaited work has not finished is never a terminal
# state. A job scheduler spells three of them, and a probe may invent its own
# wording for the same situation on a branch it takes only while the job is
# still in the queue — which is how a wait declaring RUNNING terminal reads as
# satisfied on every sweep of a job that has not started. The fixed spellings
# are refused outright; the probe's own live branch is read out of its text,
# because a renamed live state is exactly what the fixed spellings cannot see.
_WAIT_LIVE_STATE_TOKENS = frozenset({"running", "pending", "waiting"})

# The variable a probe fills from `squeue`, whose non-empty test guards the
# branch it takes while the job is still in the queue.
_SQUEUE_GUARDED_VAR = re.compile(
    r"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*\$\(\s*squeue\b"
)
_WAIT_BRANCH_STOP = re.compile(r"\b(?:elif|else|fi)\b")


def _emitted_tokens(branch: str) -> list[str]:
    """Bare word tokens a shell branch prints, one statement at a time."""
    tokens: list[str] = []
    for statement in re.split(r"[;\n]|&&|\|\||\b(?:then|do)\b", branch):
        words = [word.strip("\"'") for word in statement.split()]
        if not words or words[0] not in {"echo", "printf"}:
            continue
        tokens.extend(word for word in words[1:] if word and not word.startswith("-"))
    return tokens


def _probe_live_state_tokens(probe: Sequence[str]) -> list[str]:
    """Tokens a probe prints from a branch guarded by a non-empty squeue result.

    Those tokens describe a job that is still in the queue whatever the wait
    declaration calls the state, so any of them listed as terminal is the
    declaration contradicting its own probe.
    """
    text = " ".join(str(item) for item in probe)
    tokens: list[str] = []
    for assignment in _SQUEUE_GUARDED_VAR.finditer(text):
        var = re.escape(assignment.group("var"))
        guard = re.search(
            r"\[\s*-n\s+\"?(?:\$\{?" + var + r"\}?|\$\{\s*" + var + r"\s*\})\"?\s*\]"
            r"|\[\s*\"?\$\{?" + var + r"\}?\"?\s*\]",
            text,
        )
        if guard is None:
            continue
        branch = text[guard.end() :]
        stop = _WAIT_BRANCH_STOP.search(branch)
        if stop:
            branch = branch[: stop.start()]
        tokens.extend(_emitted_tokens(branch))
    return tokens


def _wait_terminal_names_a_live_state(
    terminal: Sequence[str], probe: Sequence[str]
) -> str:
    """Name the terminal value the probe reports while the awaited job is live.

    Empty when nothing in the terminal list names a live state, which is the
    only case a wait declaration is read at all.
    """
    if not terminal or not probe or _wait_probe_is_a_no_op(probe):
        return ""
    emitted = _probe_live_state_tokens(probe)
    for value in terminal:
        spelled = str(value).strip()
        if spelled.casefold() in _WAIT_LIVE_STATE_TOKENS:
            return spelled
        if spelled and spelled in emitted:
            return spelled
    return ""


def _wait_declaration_signature(
    condition: str,
    probe: Sequence[str],
    terminal: Sequence[str],
    resume_brief: str,
) -> str:
    """Identity of a wait declaration, without its file's modification time.

    A lift is keyed to what the declaration asks for, not to when the file was
    written. A worker that re-parks rewrites its manifest and so advances the
    mtime, which made every re-park a brand-new condition and re-lifted a wait
    whose terminal state had not actually ended anything — the loop that
    resumed one run thirty times. The same declaration arriving twice is the
    same condition; only an edit to it is a new one.
    """
    material = json.dumps(
        {
            "condition": condition,
            "probe": [str(item) for item in probe],
            "terminal": [str(item) for item in terminal],
            "resume_brief": resume_brief,
        },
        sort_keys=True,
    )
    return "wait:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _run_stream_mtime(record: Mapping[str, Any]) -> float | None:
    """The newest write to the run's stream, or None when there is none.

    The stream is where an engine's own output lands, so its mtime is the one
    fact about a run that says it is producing something right now. Absent or
    unreadable is None rather than a zero: a run with no stream has taken no
    measurement, and a missing file must not read as infinitely stale.
    """
    stream = Path(str(record.get("log_path") or ""))
    try:
        if stream.is_file():
            return stream.stat().st_mtime
    except OSError:
        return None
    return None


def _declared_wait_age_seconds(
    *,
    started_seconds: float,
    stream_mtime: float | None,
    now_seconds: float,
) -> int:
    """Age a declared wait, without counting time the run was writing output.

    A worker that is producing output is not parked, whatever its manifest
    declares. Measured 2026-09-18: a run was escalated from waiting to
    wait-aged at 1804 seconds while its newest stream file was zero minutes
    old and still growing, because the age was read from the declaration and
    nothing compared it against the run's own output. The clock that matters
    is therefore the later of the two, so a declaration sitting above a live
    stream stays young until the output actually stops — which is what makes
    wait-aged usable as a recovery trigger rather than a reading a coordinator
    has to take a second measurement to disbelieve.
    """
    latest = started_seconds
    if stream_mtime is not None and stream_mtime > latest:
        latest = stream_mtime
    return max(0, int(now_seconds - latest))


def _manifest_wait(
    manifest_data: Mapping[str, Any],
    manifest: Path,
    *,
    now_seconds: float,
    stale_after_seconds: int,
    stream_mtime: float | None = None,
    previous_lift: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the external-wait declaration a manifest actually holds.

    None means the manifest holds no wait — either it is not waiting at all, or
    it carries the four wait fields without a wait in them. A worker recording
    where it stands at orientation is not a parked run during an orientation:
    reading the mere presence of the fields as a declaration put healthy
    workers in the waiting column, aged them into wait-aged, and offered them
    to the resume sweep, which resumed them on a probe that was trivially
    true. A declaration whose condition names no wait, or whose probe cannot
    report a pending state, is therefore no declaration at all.

    A declaration that survives both readings still ages against the run's own
    output: a worker whose stream is still being written is producing, not
    parked, so the age of its wait is measured from the newer of the wait's
    declaration and the last stream write.

    A declaration whose terminal list names a state its own probe reports while
    the awaited job is still live is refused rather than honoured: it can never
    report a pending state, so it reads satisfied on every sweep and offers its
    run to the resume loop forever. The offending token is named in the refusal
    so the repair is a one-line edit rather than a reread of the probe.

    A condition takes one of two shapes. An argument vector is the one a
    scheduler query needs; ``wait_file`` is the ordinary one, because a worker
    waits for a job's log far more often than for a scheduler to report that
    the job left the queue. It names one path or an array of paths, and its
    probe and terminal are derived from those paths rather than declared, so
    the declaration reads as a wait in the same shape as any other. Two
    readers then answer it, and they are not one implementation: the sweep's
    reader in ``reckon/crew/resumption.py`` runs the derived vector and its
    ``exit:0`` sentinel and is the one that lifts a park, while
    ``_run_wait_condition_probe`` below looks for the paths directly and
    returns a row naming the ones still missing. Both must read the same
    declaration, and a case in ``tests/test_wait_shapes.py`` pins the lift
    through the sweep because a case that stops at the classifier's reader
    cannot show a run is ever resumed.

    A probe that is present but unreadable is refused by naming the shapes the
    reader does accept, and never reduced to the absence of a probe: a worker
    whose declaration was discarded reported to a coordinator as a worker that
    had declared nothing, which is a failure invisible at the moment it could
    still be repaired.

    ``previous_lift`` is the pointer's record of the last condition that lifted
    this run. A declaration identical to the one already lifted, arriving again
    after the worker re-parked, is the same condition reporting terminal a
    second time without ending: a wait-key defect, marked on the wait so the
    reader sees why the lift loop is stopped instead of watching it repeat.
    """
    if str(manifest_data.get("status") or "").strip().lower() != WAITING_STATUS:
        return None
    condition = str(manifest_data.get("wait_condition") or "").strip()
    declared_probe = _wait_probe(manifest_data.get("wait_probe"))
    files = _wait_file_paths(manifest_data.get("wait_file"))
    declared_terminal = _wait_terminal_values(manifest_data.get("wait_terminal"))
    if _wait_condition_declares_no_wait(condition):
        return None
    if _wait_probe_is_a_no_op(declared_probe):
        return None
    # A file condition's end is its paths' existence, so the terminal the
    # reader matches is derived rather than declared: one probe path answers
    # both shapes, and a file condition needs no exit-code sentinel written by
    # hand for the sweep that lifts it to read.
    terminal = declared_terminal or (["exit:0"] if files else [])
    probe = _wait_file_probe(files) if files else declared_probe
    resume_brief = str(manifest_data.get("resume_brief") or "").strip()
    missing = [
        name
        for name, value in (
            ("wait_condition", condition),
            ("wait_probe", probe),
            ("wait_terminal", terminal),
            ("resume_brief", resume_brief),
        )
        if not value
    ]
    # A shape the reader does not understand is refused by naming what it does
    # accept, so the declaration reaches the follower as something to repair
    # rather than as a worker that declared nothing.
    missing.extend(
        reason
        for reason in (
            _wait_probe_shape_refusal(manifest_data.get("wait_probe")),
            _wait_file_shape_refusal(manifest_data.get("wait_file")),
        )
        if reason
    )
    if files and declared_probe:
        missing.append(
            "either wait_probe or wait_file, not both: a wait has one shape"
        )
    if files and declared_terminal:
        missing.append(
            "wait_terminal alongside wait_file, where the condition ends when "
            "its paths exist"
        )
    unemitted = _wait_terminal_names_no_probe_state(declared_terminal)
    if unemitted:
        missing.append(
            f"wait_terminal listing {unemitted!r}, an exit-code sentinel "
            f"rather than a state the probe prints; {_WAIT_ACCEPTED_SHAPES}"
        )
    live_token = _wait_terminal_names_a_live_state(terminal, probe)
    if live_token:
        missing.append(
            f"wait_terminal listing {live_token!r}, a state the probe reports "
            "while the awaited job is still live"
        )
    started = None
    started_value = str(manifest_data.get("wait_started_at") or "").strip()
    if started_value:
        timestamp_value = _unquote_wait_declaration_scalar(started_value)
        try:
            started = datetime.fromisoformat(timestamp_value)
        except ValueError:
            missing.append("readable wait_started_at")
        else:
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
    if started is None:
        try:
            started_seconds = manifest.stat().st_mtime
        except OSError:
            started_seconds = now_seconds
    else:
        started_seconds = started.timestamp()
    age_seconds = _declared_wait_age_seconds(
        started_seconds=started_seconds,
        stream_mtime=stream_mtime,
        now_seconds=now_seconds,
    )
    expected_seconds, expected_error = _wait_expected_seconds(
        manifest_data, default_seconds=stale_after_seconds
    )
    if expected_error:
        missing.append(expected_error)
    signature = _wait_declaration_signature(condition, probe, terminal, resume_brief)
    wait_key_defect = ""
    if isinstance(previous_lift, Mapping) and previous_lift.get("trigger") == signature:
        wait_key_defect = (
            "wait-key defect: this declaration already lifted the run and has "
            "come back unchanged, so its terminal state "
            f"({', '.join(terminal) or 'unset'}) did not end the wait; the "
            "lift loop stays stopped until the declaration changes"
        )
    return {
        "condition": condition,
        "probe": probe,
        "terminal": terminal,
        "files": files,
        "resume_brief": resume_brief,
        "started_at": started_value
        or datetime.fromtimestamp(started_seconds, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "age_seconds": age_seconds,
        "expected_horizon_seconds": expected_seconds,
        "overdue": age_seconds > expected_seconds,
        "signature": signature,
        "valid": not missing,
        "error": "missing or invalid " + ", ".join(missing) if missing else "",
        "wait_key_defect": wait_key_defect,
    }


def external_wait(
    record: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
) -> dict[str, Any] | None:
    """Read a fresh external-wait declaration from one live pointer."""
    manifest = Path(str(record.get("manifest_path") or ""))
    _present, fresh = _run_chain_manifest_freshness(record)
    if not fresh:
        return None
    try:
        data = parse_manifest(manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestParseError):
        return None
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    return _manifest_wait(
        data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
        stream_mtime=_run_stream_mtime(record),
        previous_lift=record.get("auto_resume"),
    )


def _reading_host() -> str:
    """The host this reader runs on, for gating process-table lookups."""
    return socket.gethostname()


def _run_chain_manifest_freshness(record: Mapping[str, Any]) -> tuple[bool, bool]:
    """Judge delivery against the first dispatch across the attempt chain."""
    try:
        attempt = int(record.get("attempt") or 1)
        attempt_baseline = int(record["manifest_baseline_mtime_ns"])
        first_dispatch = datetime.fromisoformat(str(record.get("created_at") or ""))
    except (KeyError, TypeError, ValueError):
        return _manifest_freshness(record)
    if attempt <= 1:
        return _manifest_freshness(record)
    if first_dispatch.tzinfo is None:
        first_dispatch = first_dispatch.replace(tzinfo=UTC)
    first_dispatch_ns = (
        int(first_dispatch.timestamp()) * 1_000_000_000
        + first_dispatch.microsecond * 1_000
    )
    if attempt_baseline <= first_dispatch_ns:
        return _manifest_freshness(record)

    # Every attempt in one run shares the first dispatch as its time boundary.
    # A manifest written by any attempt is newer than that boundary and remains
    # readable as a handover, whatever status it carries. A manifest predating
    # the run stays older, so the freshness gate still rejects an unrelated
    # artifact instead of crediting it as this run's outcome. The resumed
    # attempt's own start time cannot serve here: the handover necessarily
    # predates the attempt that inherits it.
    chain_record = dict(record)
    chain_record["manifest_baseline_mtime_ns"] = first_dispatch_ns
    return _manifest_freshness(chain_record)


def _interruption_evidence(
    record: Mapping[str, Any], *, phase: str, process_alive: bool | None
) -> tuple[dict[str, Any] | None, int]:
    """Return why unfinished work stopped involuntarily, plus retained commits.

    A launcher's wait status is direct evidence that a signal ended the worker.
    Where no exit was recorded, death alone is ambiguous: an orphaned pointer
    already records that no terminal event arrived, while commits beyond the
    dispatch base prove an apparently working run left recoverable work behind.
    A deliberate stop or a recorded completion/promotion always outranks either
    inference.
    """
    if phase in {"complete", "promoted", "stopped"} or record.get("promoted_at"):
        return None, 0

    wait_status = record.get("wait_status")
    if isinstance(wait_status, Mapping) and wait_status.get("signal") is not None:
        signal_number = wait_status.get("signal")
        signal_name = str(wait_status.get("signal_name") or f"signal {signal_number}")
        return (
            {
                "reason": "signal",
                "signal": signal_number,
                "signal_name": signal_name,
                "exit_code": wait_status.get("exit_code"),
            },
            0,
        )

    if process_alive is not False:
        return None, 0
    if phase == "orphaned":
        return (
            {
                "reason": "dead-pid-no-exit",
                "signal": None,
                "signal_name": None,
                "exit_code": None,
            },
            0,
        )

    commits = _commits_beyond_base(record)
    if commits:
        return (
            {
                "reason": "dead-pid-with-retained-work",
                "signal": None,
                "signal_name": None,
                "exit_code": None,
            },
            commits,
        )
    return None, 0


def classify_pointer(
    record: Mapping[str, Any],
    *,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
    now_seconds: float | None = None,
    condition_test: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Classify one live pointer, without touching it.

    Pure and read-only, so the same judgement serves an MCP read and
    :func:`recover`. Liveness is established at the moment of use: when the
    record's launching host is this host the process table is asked now, and
    otherwise the stored answer is carried and marked unproven. The recorded
    launching host is the pointer's ``launcher_host`` field, spelled with
    ``socket.gethostname()`` on the machine that launched the run. Delivery
    comes from the manifest's status, because a terminal stream event only says
    the worker's turn ended. It does not say the node completed successfully.
    """
    run_id = str(record.get("run_id") or "")
    phase = str(record.get("phase") or "")
    manifest = Path(str(record.get("manifest_path") or ""))
    manifest_file_present, manifest_present = _run_chain_manifest_freshness(record)
    manifest_data: dict[str, Any] = {}
    manifest_error = ""
    manifest_digest: str | None = None
    if manifest_present:
        try:
            manifest_text = manifest.read_text()
            manifest_data = parse_manifest(manifest_text)
            # A content digest lets a watcher tell a rewrite that changed
            # something from a touch that did not. Computed from the same read
            # that parsed the status, so the digest and the verdict can never
            # describe different versions of the file.
            manifest_digest = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
        except (OSError, ManifestParseError) as exc:
            # The file exists but no reader can judge it: an unreadable file is
            # a condition of the delivery, not an exception in the classifier.
            # Collecting it here keeps the refusal text (the parse error) in a
            # channel the classification branches read, so a manifest that
            # declares a format and is not readable degrades to its own outcome
            # rather than escaping this function and failing every ticker
            # refresh for every session.
            manifest_error = str(exc)
    manifest_reported_status = str(manifest_data.get("status") or "").strip().lower()
    manifest_unwritten = manifest_status_is_template(manifest_reported_status)
    # The dispatch contract prints all terminal choices as a placeholder. It
    # is evidence that the worker never wrote a verdict, not a fourth spelling
    # of one, so no terminal predicate may see it as delivered state.
    manifest_status = "" if manifest_unwritten else manifest_reported_status
    manifest_derived = str(manifest_data.get("derived") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if manifest_derived:
        # A recovery artifact preserves evidence; it is not delivery by the
        # worker and therefore cannot satisfy the promotion precondition.
        manifest_present = False
        manifest_digest = None
    manifest_commits = list(manifest_data.get("commits") or [])
    manifest_blockers = list(manifest_data.get("blockers") or [])
    needs_help = manifest_data.get("needs_help")
    # Populated only while a dead unfinished run is being distinguished as an
    # interruption with retained work or as an abandonment with nothing left:
    # asking git costs a subprocess, so live and settled delivery paths never pay.
    commits_beyond_base = 0
    # Liveness is read at the moment it is used, not carried from the fleet
    # read that loaded the pointer. The process table answers only when the
    # record's launching host is the reading host: a pid is meaningful only on
    # the machine that issued it, and the crew home is shared across login
    # nodes, so asking a foreign pid table fabricates a verdict in both
    # directions. Where the launching host cannot be shown to be this host the
    # stored answer is kept and the row carries that it is unproven — an
    # unprovable answer is not proof of death.
    stored_alive = record.get("process_alive")
    if (
        record.get("launcher_host") is not None
        and str(record.get("launcher_host")) == _reading_host()
        and record.get("pid")
    ):
        # The run was launched here: the launched pid's kernel state is the
        # authority at this instant, and the recorded start tick rules out a
        # reused pid — the same reading ``list_live`` produces for a fleet
        # view. A zombie entry answers not alive, composing with the narrowed
        # probe rather than reviving the old answer.
        alive = runs.record_process_alive(record)
        expected_start = record.get("pid_start_time")
        if alive is True and expected_start is not None:
            alive = _process_start_time(record.get("pid")) == expected_start
        liveness_proven = True
    else:
        alive = stored_alive
        liveness_proven = False
    log = Path(str(record.get("log_path") or ""))
    age = None
    if log.is_file():
        age = max(0, int(_utc_seconds() - log.stat().st_mtime))
    # Superseded-by-newer-activity applies to an ordinary non-terminal report
    # that is not yet a verdict. A declared wait is different: the manifest is
    # the authority for what the worker is parked on, and its process may stay
    # alive briefly or exit immediately without changing that condition.
    # Terminal-looking reports are handled below: the live process outranks
    # every worker-reported outcome regardless of file recency, and the
    # manifest becomes authoritative when that process exits.
    if (
        manifest_status
        and manifest_status not in TERMINAL_MANIFEST_STATUSES
        and manifest_status != WAITING_STATUS
        and alive is True
        and log.is_file()
        and manifest.is_file()
        and log.stat().st_mtime_ns > manifest.stat().st_mtime_ns
    ):
        manifest_present = False
        manifest_data = {}
        manifest_digest = None
        manifest_status = ""
        manifest_commits = []
        manifest_blockers = []
        needs_help = None
    # A provider refusal makes an otherwise-abandoned run a block: the process
    # is gone but the stop is triageable (a named backend, limit and reset) and
    # resumable once the limit lifts. Detected from the same stream observe
    # reads, so the two paths agree.
    budget = _stream_budget(record)
    refusal_block = (
        _refusal_block(record, budget)
        if budget is not None and budget.get("refusal")
        else None
    )
    # A spent lane writes retries, not a refusal event; its mid-flight shape is
    # read alongside the refusal and only when no refusal already explains the
    # stop, so the two dead-lane readings never compete for the same run. The
    # budget is resolved once above, so both gates share a single stream read. A
    # background wait is checked only when neither already explains the stop:
    # all three name a process that is gone but resumable, and the lane reason
    # is the most triageable of the three when more than one is present.
    retry_block = (
        _stream_retry_block(record, budget)
        if budget is not None and not budget.get("refusal")
        else None
    )
    # Terminal retry exhaustion on an unmetered lane: the budget block carries
    # lane_backpressure and a retry count where the metered exhaustion carries
    # a refusal, so the two dead-lane readings stay on their own gates and a
    # live or recovered run never reaches a block through either.
    exhaustion_block = (
        _stream_exhaustion_block(record, budget)
        if budget is not None and not budget.get("refusal")
        else None
    )
    # A rejected rate-limit window is a hold time lifts, not a refusal a person
    # resolves: the event names the window and its reset, so the run pauses
    # until the window turns over rather than blocking for a coordinator.
    budget_hold = _budget_hold_block(record, budget)
    background_wait = (
        None
        if (refusal_block or retry_block or budget_hold)
        else _background_wait_signal(record)
    )
    # A refusal at admission is read from the stream's own marks, not from the
    # budget block: it is not a spend refusal — nothing was requested — and the
    # block carries no budget to refuse from. It is resolved here so the
    # dead-process chain consults the stream once for the shape.
    admission_refusal = (
        None
        if (refusal_block or retry_block or exhaustion_block or budget_hold)
        else _admission_refusal(record)
    )
    terminal = phase in ("complete", "failed")
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    wait = _manifest_wait(
        manifest_data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
        stream_mtime=_run_stream_mtime(record),
        previous_lift=record.get("auto_resume"),
    )
    if wait is not None and not wait["valid"]:
        # An incomplete wait declaration is a reading failure carried on the
        # row whatever the process state: a gone run reads unreadable from it,
        # a live run reads running from liveness with the same text beside it,
        # so both readings share one refusal instead of each arm re-deriving it.
        manifest_error = str(wait["error"])
    wait_observation: dict[str, str] | None = None
    if wait is not None and wait["valid"]:
        observe_condition = (
            _run_wait_condition_probe if condition_test is None else condition_test
        )
        try:
            wait_observation = _wait_condition_observation(
                observe_condition(record, wait),
                terminal_values=list(wait["terminal"]),
            )
        # A probe is untrusted external input. Any ordinary fault says nothing
        # about either the condition or the worker, so it becomes unknown and
        # the run stays waiting; abandoned remains reserved for proof of death.
        except Exception as exc:  # noqa: BLE001
            wait_observation = {
                "state": "unknown",
                "observed": "unavailable",
                "detail": f"condition probe could not answer: {exc}",
            }
    terminal_at = None
    terminal_age_seconds = None
    deferred_outcome = alive is True and manifest_status in TERMINAL_MANIFEST_STATUSES
    interruption = None
    interruption_commits = 0
    if manifest_status not in TERMINAL_MANIFEST_STATUSES:
        interruption, interruption_commits = _interruption_evidence(
            record, phase=phase, process_alive=alive
        )
        commits_beyond_base = interruption_commits
    review: dict[str, Any] | None = None
    review_error = ""
    if manifest_status == "complete" and not deferred_outcome:
        review, review_error = _stored_review(record)
    review_complete = _review_is_complete(review)
    if manifest_status in TERMINAL_MANIFEST_STATUSES and not deferred_outcome:
        terminal_seconds = manifest.stat().st_mtime
        terminal_at = (
            datetime.fromtimestamp(terminal_seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        terminal_age_seconds = max(0, int(moment - terminal_seconds))

    # Classification order: the process is consulted before the manifest
    # reading. A worker whose process is alive is classified from that life and
    # never as unreadable, so a strictness added for manifests at rest cannot
    # misreport work in progress; the manifest and its status become
    # authoritative only once the process is gone.
    #
    # The liveness test itself is hoisted above the chain as one verdict, and
    # every reading that could call a run unreadable — a present-but-unparseable
    # manifest or an incomplete wait declaration — consults it here rather than
    # testing liveness for itself, so the guarantee cannot decay into per-arm
    # guards as manifest readings are added.
    process_gone = alive is not True
    marker = None
    needs_help_complete_value = None
    if interruption is not None:
        classification = INTERRUPTED_RUN_PHASE
        signal_name = interruption.get("signal_name")
        if signal_name:
            detail = (
                f"the worker process ended by {signal_name} "
                f"(signal {interruption['signal']}) before the run completed"
            )
        elif interruption["reason"] == "dead-pid-with-retained-work":
            detail = (
                "the worker process is gone with no recorded exit and the "
                f"worktree carries {interruption_commits} commit"
                f"{'s' if interruption_commits != 1 else ''} beyond the dispatch base"
            )
        else:
            detail = (
                "the worker process is gone with no recorded exit; its pointer "
                "had already recorded that no terminal event arrived"
            )
        action = "resolve the surviving session before choosing a recovery"
    elif manifest_unwritten:
        classification = "running"
        detail = (
            f"the manifest template at {manifest} is present but its status "
            "placeholder was never replaced"
        )
        action = (
            f"reckon crew resume --run {run_id} --advice "
            "write the manifest's current status before continuing"
        )
    elif deferred_outcome:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif manifest_status == "complete":
        if _pointer_role(record) == REVIEW_ROLE:
            # A review run's deliverable is the review it wrote for another
            # run, so it is not itself awaiting review. The exemption is the
            # role the run carried, not the presence of a stored review: a run
            # that never had a review attached still reads as scoring when its
            # role could have had one, and the reflex keeps dispatching for it.
            # Without this arm the scoring branch composes a dispatch whose
            # source node is this run, whose review run completes and scores in
            # turn — an unbounded chain of reviews reviewing reviews, each one
            # a real dispatch against a real member.
            classification = "promotable"
            detail = (
                "the worker manifest reports completion; the run is the "
                f"{REVIEW_ROLE} it dispatched with, so the review it wrote is "
                "its deliverable and no review of this run is required"
            )
            action = (
                f"promote the completed {REVIEW_ROLE} run once its verdict is "
                "read; the run is not itself reviewed"
            )
        elif review_complete:
            classification = "promotable"
            detail = (
                "the worker manifest reports completion and an independent "
                "parsed review is attached; the run is ready for promotion"
            )
            action = f"reckon crew complete --run {run_id} --gate <verdict>"
            action += "".join(f" --commit {commit}" for commit in manifest_commits)
        else:
            classification = "scoring"
            if review_error:
                review_detail = f"the stored review could not be read: {review_error}"
            elif review is None:
                review_detail = "no independent review is attached"
            else:
                review_detail = (
                    f"the attached review is {review.get('status') or 'incomplete'}"
                )
            detail = (
                "the worker manifest reports completion, but "
                f"{review_detail}; an independent review must be produced before promotion"
            )
            action = _review_dispatch_action(record)
    elif manifest_status == "blocked":
        classification = "blocked"
        # A blocked transition explains itself from the best source available,
        # in order: the worker's own escape-hatch question (already parsed and
        # complete — the sentence a coordinator can answer in one turn), then
        # the manifest's blockers, then a generic fallback. A bare-punctuation
        # result (a block-scalar indicator misread as its value, upstream)
        # explains nothing, so it is treated as absent too.
        needs_help_complete = isinstance(needs_help, Mapping) and bool(
            needs_help.get("complete")
        )
        headline = str(needs_help.get("headline") or "") if needs_help_complete else ""
        blocker = "; ".join(manifest_blockers)
        reason_text = headline or blocker or "the manifest reports a blocker"
        if not re.search(r"[A-Za-z0-9]", reason_text):
            reason_text = "the manifest reports a blocker"
        needs_help_complete_value = needs_help_complete
        detail = f"the worker manifest reports blocked: {reason_text}"
        # The manifest says what the worker was doing when it stopped; a
        # provider refusal says when anything can be attempted at all. When
        # both are present the manifest arm must not crowd the refusal out:
        # the refusal names the condition that gates recovery, so it is added
        # with its reset and the reader is told which must clear first. The
        # classification and the manifest reason both stay — a NEEDS-HELP
        # question on a spent lane still needs its answer, and an operator
        # simply cannot act on it until the lane clears.
        if refusal_block:
            lane = (
                f"backend {refusal_block['backend']!r} refused the turn on a "
                f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
            )
            detail += (
                f"; the provider refusal must clear first — {lane} — no resume "
                "may be attempted before it does"
            )
        if needs_help_complete:
            marker = "?"
            action = f"reckon crew resume --run {run_id} --advice <answer>"
        else:
            marker = "!"
            action = f"read {manifest}; resolve the blocker before resuming the run"
        if refusal_block:
            # The lane, not the worker, owns the stop: a resume attempted before
            # the reset is refused on budget, so the offered resume is gated on
            # the lane clearing rather than proposed as work the operator can do
            # today. The recovery sweep resumes blocked runs, so the same command
            # stays the correct next action under that gate.
            action += " once the lane clears"
    elif manifest_status == "failed":
        classification = "failed"
        failure = "; ".join(manifest_blockers) or "the worker manifest reports failure"
        detail = f"the worker manifest reports failed: {failure}"
        action = (
            f"read {manifest} and launch log {record.get('stderr_path')}; "
            "repair or redispatch the run"
        )
    elif wait is not None and wait["valid"]:
        classification = WAITING_STATUS
        if wait_observation and wait_observation["state"] == "met":
            detail = (
                f"ready to resume: {wait['condition']} reported "
                f"{wait_observation['observed']!r}, a declared terminal state"
            )
            action = f"reckon crew resume --run {run_id} --advice continue"
        else:
            observation_detail = (
                wait_observation["detail"]
                if wait_observation is not None
                else "the condition probe did not answer"
            )
            detail = (
                f"waiting {wait['age_seconds']}s on {wait['condition']}; "
                f"{observation_detail}; terminal when the probe reports "
                f"{', '.join(wait['terminal'])}"
            )
            action = (
                f"the recovery sweep will resume run {run_id} when the condition "
                "test reports a terminal state"
            )
        if wait.get("wait_key_defect"):
            # The declaration lifted this run once already and came back
            # unchanged, so its terminal state is not ending anything. The
            # sweep already refuses a second lift for the same declaration; the
            # row says why rather than reading as an ordinary pending wait.
            detail = (
                f"{wait['wait_key_defect']} (waiting {wait['age_seconds']}s on "
                f"{wait['condition']})"
            )
            action = (
                f"edit the wait declaration in {manifest} so its terminal list "
                "names a state the probe cannot report while the job is live"
            )
    elif wait is not None and process_gone:
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} declares an external wait but is "
            f"incomplete: {manifest_error}"
        )
        action = f"repair the waiting declaration in {manifest} before resuming"
    elif phase == "stopped":
        classification = "stopped"
        detail = "the run was intentionally stopped"
        action = (
            f"inspect the worktree at {record.get('worktree')} and discard when safe"
        )
    elif budget_hold and alive is not True:
        # A rate-limit window that rejected the turn is the clearest case of
        # the who-lifts-it rule: the request was refused on a window the
        # provider resets on its own cadence, so time lifts the hold and no
        # person is needed. The paused verdict names the reset as its wake.
        # Deliberately not routed through the sweep claim the refusal arm makes:
        # a rejected window is not a formal refusal, so the sweep is not the
        # mechanism that lifts it — the reset is, and that is what is named.
        # A live process is never classified paused on this signal: it is still
        # running, and if it goes quiet the stall gate names the rejected
        # window as a wait rather than a hang. This arm owns the dead-process
        # reading, where a vanished run's last word was the rejection.
        classification = "paused"
        hold = (
            f"{budget_hold['limit_kind']} window refusals on backend "
            f"{budget_hold['backend']!r} reset {budget_hold['resets_at']}"
        )
        detail = (
            f"paused: {hold}; the hold ages out when the window resets and "
            "the run proceeds from there"
        )
        action = (
            f"resume run {run_id} once the window reset at "
            f"{budget_hold['resets_at']} lifts the hold"
        )
    elif refusal_block:
        # A refusal stays blocked rather than paused even when the limit has a
        # reset: the recovery sweep auto-resumes only runs classified blocked
        # (resumption gates on it), so a paused refusal would wait for a reset
        # nothing acts on. The who-lifts-it rule is therefore applied to the
        # window hold that carries its own expiry — the budget_hold arm above —
        # while a prose or retry exhaustion refusal remains a decision the
        # coordinator must make: it is not a wait that lifts itself.
        classification = "blocked"
        block = (
            f"backend {refusal_block['backend']!r} refused the turn on a "
            f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
        )
        # The block states what was delivered so a reader does not conclude
        # nothing happened. A run killed with no manifest has nothing to show;
        # one whose manifest never reached a verdict still names its delivery
        # in the file, and pointing at it is the difference between a blocked
        # run and a vanished one.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the limit lifts"
    elif alive is False and exhaustion_block:
        # A dead run whose unmetered lane ended its retries in refusal is the
        # same stop as a metered budget refusal: the lane owns it, so the row
        # names the lane and offers resume rather than reading as abandonment.
        # The terminal-error shape sets it apart from the mid-flight retry arm
        # below, which names the retry count of a run still in flight when it
        # died — the exhausted run's own lane already refused, so the reading
        # keeps the refusal phrasing instead.
        classification = "blocked"
        block = (
            f"backend {exhaustion_block['backend']!r} refused the turn on a "
            f"{exhaustion_block['limit_kind']} (consumer queue backpressure) "
            f"after {exhaustion_block['retries']} retries"
        )
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the lane recovers"
    elif alive is False and retry_block:
        # A dead process whose stream ended mid-retry is a lane kill, not a
        # vanished worker: the budget block names the retry count, the process
        # table says the worker is gone, and the lane that refused is the most
        # triageable stop a fleet can suffer. Liveness is the verdict, never the
        # count alone — a live worker mid-retry-burst is exactly the
        # two-runs-that-succeeded case and reads running, not blocked — and
        # phase is not consulted, because a finished or killed run can still
        # carry a starting phase in its pointer. The next action offers resume
        # rather than discard because the lane, not the worker, owns the stop.
        classification = "blocked"
        block = (
            f"backend {retry_block['backend']!r} rate-limited the run "
            f"{retry_block['retries']} times and its process died mid-retry "
            f"({retry_block['limit_kind']})"
        )
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the lane recovers"
    elif background_wait:
        # A vanished process is not the same fact as a crashed one: the run
        # directory itself says it was waiting on background work when it
        # ended, so it resumes rather than reading as abandoned and inviting a
        # redispatch that throws away an intact session. Whether it blocks or
        # pauses is the who-lifts-it rule: a run whose in-progress manifest
        # names committed work is parked on its own job and resumes when that
        # job ends, so it pauses; one with nothing committed needs a reader to
        # decide, so it stays blocked.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        if manifest_commits:
            # The who-lifts-it rule, on the parked case: the run is waiting on
            # its own background job, its committed work is safe in the tree,
            # and the job ends on its own — so it pauses and names the end of
            # that work as the wake. The resume action is the follow-through
            # once the job ends, not the reason it paused.
            classification = "paused"
            detail = (
                f"paused: {background_wait}; the committed work is safe and the "
                "run resumes when the background work it was waiting on ends"
            )
            action = (
                f"resume run {run_id} when the background work it was waiting on ends"
            )
        else:
            classification = "blocked"
            detail = f"blocked: {background_wait}; {delivery}"
            action = f"reckon crew resume --run {run_id}"
    elif manifest_error and manifest_present and process_gone:
        # The third manifest outcome next to absent and readable-and-terminal:
        # a file that is present but that no supported reader can parse is
        # neither a delivered record nor an absence. The name states what the
        # reader is to do, and the refusal text (the parse error, naming the
        # format the file declared and why it was rejected) travels in the same
        # manifest_error channel the abandoned arm used so the operator's next
        # question is answerable one turn before the run can be judged.
        # A positively live process outranks this reading: the worker is still
        # in flight and its half-written or mid-write manifest is a condition of
        # that work, not an unreadable delivery, so the run reads running and a
        # reader answers where it is rather than reporting it unreadable.
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} is present but could not be read: "
            f"{manifest_error}"
        )
        # The named object is the manifest: the file is what needs repair, and
        # the abandoned instruction (which points at the launch log and offers
        # redispatch) must never read as the remedy for a file that exists.
        action = (
            f"the manifest at {manifest} cannot be read — repair or replace "
            "it before judging the run"
        )
    elif manifest_status in NON_TERMINAL_MANIFEST_STATUSES:
        # A worker-reported working status is evidence of life, not death. What
        # the process table says now happened after the worker's last word, so
        # the row reads working — the status stays on it rather than being
        # dropped — and never abandoned, whatever state the process is in.
        classification = "running"
        if alive is True:
            detail = (
                f"the worker manifest reports it is still working: {manifest_status}"
            )
        else:
            detail = (
                f"the worker manifest reports it was still working "
                f"({manifest_status}) when the process ended; the run is not "
                "reported dead"
            )
        action = f"reckon crew observe --run {run_id}"
    elif alive is False and _commits_beyond_base(record):
        # Committed work is proof the worker delivered, and the fact lives in
        # git rather than in any manifest format, so it survives a missing or
        # unreported manifest. A dead process with commits past its base to
        # show is not a vanished worker; it reads running and names the
        # committed work as what survived.
        commits_beyond_base = _commits_beyond_base(record)
        classification = "running"
        detail = (
            f"the worktree at {record.get('worktree')} carries "
            f"{commits_beyond_base} commit"
            f"{'s' if commits_beyond_base != 1 else ''} beyond its recorded "
            "base; the delivered work survives in git"
        )
        action = (
            f"inspect the worktree at {record.get('worktree')}; the committed "
            "work is safe and can be promoted or resumed once a manifest "
            "documents it"
        )
    elif alive is False and admission_refusal is not None:
        # A run the backend refused at admission is named for what it is rather
        # than folded into the abandoned bucket. The process is gone and no
        # manifest was delivered in both cases, but here the stop is that no
        # model ever served a turn — a fact the stream states and the generic
        # bucket cannot, so a reader is spared diagnosing a vanish as the lane
        # fault they already know about. The narrow arm sits beside the
        # abandoned reading and takes nothing from it: a genuine vanish carries
        # none of these marks and still reads abandoned.
        classification = "refused-at-admission"
        detail = (
            "refused at admission: the backend returned no turn "
            f"({admission_refusal['reason']!r}, terminal reason "
            f"{admission_refusal['terminal_reason']!r}) with every token "
            "counter zero; no model was reached"
        )
        action = (
            f"read the refusing stream {record.get('log_path')} and launch log "
            f"{record.get('stderr_path')}; the run never reached a model, and a "
            "resume replaces the pointer, so keep the stream as the durable "
            "record of the refusal"
        )
    elif terminal and alive is False:
        # Abandoned requires positive proof of death: the process table says
        # the worker is gone AND nothing eligible for promotion was delivered.
        # The stored phase alone is the last writer's label, not evidence, so
        # it only participates when the process verdict confirms it.
        classification = "abandoned"
        if manifest_derived:
            delivery = "only a recovery-derived manifest exists"
        elif not manifest_present:
            delivery = "no manifest was delivered"
        else:
            # A present-but-unreadable manifest never reaches this arm: it is
            # intercepted above as its own outcome before the terminal reading
            # can fold it into abandoned.
            delivery = f"the manifest status {manifest_status!r} is not usable"
        detail = (
            f"the stored phase is terminal but {delivery}; nothing is eligible "
            "for promotion"
        )
        action = (
            f"reckon crew resume --run {run_id} --advice "
            f"{shlex.quote(f'review {manifest} and replace it with a worker-written manifest')}"
            if manifest_derived
            else (
                f"read launch log {record.get('stderr_path')}; inspect the worktree at "
                f"{record.get('worktree')} and redispatch if needed"
            )
        )
    elif terminal:
        # A terminal stored phase is not a dead run while the process table
        # has not confirmed death. An alive process outranks the stored phase,
        # and a pid whose liveness cannot be checked is no proof of death
        # either, so the run is never called abandoned here and its action
        # never advises redispatch — duplicating a live worker is the cost
        # this arm exists to stop.
        if alive is True:
            classification = "running"
            detail = "the process is alive despite the terminal stored phase"
        else:
            classification = "running"
            detail = (
                "the stored phase is terminal but process liveness could not be "
                "proven; the pointer is left in place pending a manifest or "
                "evidence of death"
            )
        action = f"reckon crew observe --run {run_id}"
    elif phase == "launch-failed":
        # A launch that never wrote a stream record reached no model, so this
        # is an infrastructure fault rather than a worker turn. It sits on its
        # own state so a reader sees it apart from a working run, and the lift
        # refuses it until a person acts.
        failures = list(record.get("launch_failures") or ())
        latest = failures[-1] if failures else {}
        tail = str(latest.get("stderr_tail") or "").strip().splitlines()
        cause = tail[-1] if tail else "the process exited before any turn"
        classification = "launch-failed"
        detail = (
            f"the launch for backend {latest.get('backend') or record.get('backend')!r} "
            f"exited with status {latest.get('exit_status')} before writing any "
            f"stream record ({cause}); {len(failures)} launch failure"
            f"{'s' if len(failures) != 1 else ''} recorded; no model was reached"
        )
        action = (
            f"fix the command and PATH for backend "
            f"{latest.get('backend') or record.get('backend')!r}, then resume "
            f"{run_id} by hand — the lift loop stays stopped until then"
        )
    elif alive is True:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif alive is False:
        # The deliverable is read before the process, so this arm reaches only
        # a run with nothing to show. Every manifest reading that a killed
        # worker can leave behind — a complete status behind a parsed review, a
        # non-terminal status, an unreadable file, committed work past base,
        # a refusal or a lane stop in the stream — is arbitrated above this
        # point and never falls here, because a dead process says nothing about
        # what the run delivered before it died.
        classification = "abandoned"
        detail = (
            "the process is gone without a complete manifest; nothing is eligible "
            "for promotion"
        )
        action = (
            f"read launch log {record.get('stderr_path')}; the worktree at "
            f"{record.get('worktree')} is left in place for review and is never "
            "force-removed"
        )
    else:
        classification = "running"
        detail = (
            "an in-harness run: liveness belongs to the calling harness, so it "
            "is reported as running until a manifest appears"
        )
        action = f"reckon crew observe --run {run_id}"

    session_resolution = None
    resume_remedy = None
    if classification in {"blocked", INTERRUPTED_RUN_PHASE}:
        session_resolution = _blocked_session_resolution(record, run_id)
    if classification == INTERRUPTED_RUN_PHASE and session_resolution is not None:
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is not None:
            action = resume_remedy["command"]
            detail = (
                f"{detail}; session {resume_remedy['session_id']!r} survives in "
                f"the {resume_remedy['source']} record"
            )
        else:
            absent_evidence = str(
                session_resolution.get("detail")
                or "no session id was found in the available run evidence"
            )
            action = (
                f"inspect the worktree at {record.get('worktree')}, then redispatch "
                "the unfinished work"
            )
            detail = f"{detail}; redispatch is required because {absent_evidence}"
    if session_resolution is not None and (
        refusal_block is not None
        or retry_block is not None
        or exhaustion_block is not None
    ):
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is None:
            absent_evidence = str(
                session_resolution.get("detail")
                or "no session id was found in the available run evidence"
            )
            detail = f"{detail}; no resume remedy: {absent_evidence}"
            if action.startswith("reckon crew resume"):
                action = (
                    f"inspect the worktree at {record.get('worktree')} and launch "
                    "log; no session id is available to resume"
                )

    hold = refusal_block or exhaustion_block or retry_block or budget_hold
    if classification == INTERRUPTED_RUN_PHASE:
        recovery_classification = INTERRUPTED_RUN_PHASE
    elif manifest_unwritten:
        recovery_classification = "unwritten"
    elif classification in {"blocked", "paused"} and hold is not None:
        recovery_classification = "held"
    elif classification == "blocked" and needs_help_complete_value:
        recovery_classification = "needs-help"
    elif (
        classification == WAITING_STATUS
        and wait_observation is not None
        and wait_observation.get("state") == "met"
    ):
        recovery_classification = "ready"
    elif classification == WAITING_STATUS and wait and wait.get("overdue"):
        recovery_classification = "wait-aged"
    else:
        recovery_classification = classification
    recovery_verb = RECOVERY_VERBS[recovery_classification]
    if recovery_classification == INTERRUPTED_RUN_PHASE and resume_remedy is not None:
        recovery_verb = "resume"

    lifting_condition = None
    if classification == WAITING_STATUS and wait is not None:
        lifting_condition = (
            f"{wait['condition']} reports one of {', '.join(wait['terminal'])}"
        )
    elif classification == "paused":
        if budget_hold is not None:
            lifting_condition = (
                f"the {budget_hold['limit_kind']} window resets at "
                f"{budget_hold['resets_at']}"
            )
        elif background_wait:
            lifting_condition = "the background work named by the row ends"
        else:
            lifting_condition = DEFAULT_LIFTING_CONDITIONS["paused"]

    timing = _budget_timing(record, now_seconds=now_seconds)
    classified = {
        "run_id": run_id,
        "project": record.get("project"),
        # Several coordinator sessions share one project, so every read of a
        # run has to say whose it is. Without it a session reading the live
        # view cannot tell its own fleet from a peer's, and acting on a peer's
        # row is worse than not seeing it.
        "session": record.get("session"),
        "plan": (record.get("node") or {}).get("plan"),
        "node": (record.get("node") or {}).get("id"),
        "classification": classification,
        # Cause and remedy are separate from the compatibility lifecycle
        # grouping above. This pair is the authoritative instruction surface:
        # readers act on the verb and use the classification to understand why.
        "recovery_classification": recovery_classification,
        "recovery": recovery_verb,
        "lifting_condition": lifting_condition,
        "resets_at": (
            str(hold.get("resets_at") or "unknown")
            if recovery_classification == "held" and hold is not None
            else None
        ),
        "phase": phase,
        "effective_phase": (
            INTERRUPTED_RUN_PHASE if classification == INTERRUPTED_RUN_PHASE else phase
        ),
        "interruption": interruption,
        "process_alive": alive,
        # False when the stored answer was carried because the launching host
        # could not be shown to be this host, or there is no pid to ask about.
        # An unproven answer is not death, so a reader needing certainty reads
        # this field rather than treating a stale stored value as a verdict.
        "liveness_proven": liveness_proven,
        "manifest_present": manifest_present,
        "manifest_file_present": manifest_file_present,
        "manifest_fresh": manifest_present,
        "manifest_path": str(manifest) if str(manifest) != "." else "",
        # A living worker's complete or failed report remains on disk but is
        # not exposed as an outcome. The single-event watcher consumes this
        # field, so returning the raw report here would call the run terminal
        # while the classification and ticker correctly call it live.
        "manifest_status": None if deferred_outcome else manifest_status or None,
        # Keep the worker's raw spelling alongside the effective status. A
        # live process defers terminal-looking placeholders, while the one-shot
        # watcher still needs to recognise a fresh completion written by the
        # resumed attempt it is waiting for.
        "manifest_reported_status": manifest_reported_status or None,
        "manifest_derived": manifest_derived,
        "manifest_commits": manifest_commits,
        # Committed work past the recorded base, read from git on the abandoned
        # tail only. A surface that would otherwise read the same run dead
        # consults this field, so classification and the pane never disagree.
        "commits_beyond_base": commits_beyond_base,
        # The refusal text when a present manifest could not be read, carried on
        # the row so a surface that discards nothing has it one field away.
        "manifest_error": manifest_error or None,
        # A content digest of the manifest as read, so a watcher can tell a
        # rewrite that changed something from a touch that did not. None when
        # no manifest was present or readable, so absence never looks like a
        # digest to compare against.
        "manifest_digest": manifest_digest,
        # Review presence and readability are separate facts. An emitted review
        # that did not parse is evidence to repair, never an absent review that
        # can be silently regenerated without showing what the reviewer wrote.
        "review_present": review is not None or bool(review_error),
        "review_status": (
            "unreadable"
            if review_error
            else str(review.get("status") or "") or None
            if review is not None
            else None
        ),
        "review_error": review_error or None,
        "terminal_at": terminal_at,
        "terminal_age_seconds": terminal_age_seconds,
        "log_age_seconds": age,
        "log_fresh": None if age is None else age <= stale_after_seconds,
        **timing,
        "worktree": record.get("worktree"),
        "detail": detail,
        "next_action": action,
        # Set only for a blocked run: "?" when the escape-hatch question is
        # complete enough that `reckon crew resume --advice` can answer it,
        # "!" when the reader has to read the manifest itself. The fact behind
        # it travels too so the renderer derives the glyph instead of persisting it.
        "marker": marker,
        "needs_help_complete": needs_help_complete_value,
        "external_wait": wait,
        "wait_age_seconds": wait.get("age_seconds") if wait else None,
        "wait_overdue": wait.get("overdue") if wait else None,
        "wait_condition_state": (
            wait_observation.get("state") if wait_observation is not None else None
        ),
        "wait_observed": (
            wait_observation.get("observed") if wait_observation is not None else None
        ),
        # A declaration that lifted its run and came back unchanged is the
        # defect the lift loop's own stop cannot name; carried on the row so a
        # reader sees why the run is still parked rather than inferring it.
        "wait_key_defect": wait.get("wait_key_defect") or None if wait else None,
    }
    if session_resolution is not None:
        classified["session_resolution"] = session_resolution
    if resume_remedy is not None:
        classified["resume_remedy"] = resume_remedy
    return classified


def _commits_beyond_base(record: Mapping[str, Any]) -> int:
    """Count commits in the worktree past the pointer's recorded base.

    The count lives in git, so it survives any manifest format: a worktree
    whose history carries commits after the base the run launched from
    delivered work, whatever the manifest says, and that fact is never erased
    by a missing or unreported manifest. Zero when the worktree or base is
    absent or the count cannot be read — an unreadable tree proves nothing, so
    it must not fabricate a rescue.
    """
    worktree = Path(str(record.get("worktree") or ""))
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not base or not worktree.is_dir():
        return 0
    count = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if count.returncode != 0:
        return 0
    try:
        return max(0, int(count.stdout.decode().strip()))
    except (ValueError, UnicodeDecodeError):
        return 0


def _worktree_diff_paths(record: Mapping[str, Any]) -> list[str]:
    """Return the base-to-worktree path census used for recovery evidence."""
    worktree = Path(str(record.get("worktree") or ""))
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not base or not worktree.is_dir():
        return []
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, "--"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if tracked.returncode or untracked.returncode:
        return []
    paths = {
        os.fsdecode(raw)
        for raw in (*tracked.stdout.split(b"\0"), *untracked.stdout.split(b"\0"))
        if raw
    }
    return sorted(paths)


def _derived_manifest_text(record: Mapping[str, Any], paths: list[str]) -> str:
    """Render evidence that recovery found without claiming worker delivery."""
    node = str((record.get("node") or {}).get("id") or record.get("run_id") or "")
    final_message = " ".join(str(record.get("final_message") or "").split())
    changed = ", ".join(paths) or "none"
    evidence = (
        f"final message: {final_message}" if final_message else "final message: none"
    )
    return (
        f"node: {node}\n"
        "status: derived\n"
        "derived: true\n"
        "derived_reason: terminal run omitted its worker manifest\n"
        "commits: none\n"
        f"changed_paths: {changed}\n"
        "tests: not verified — worker manifest missing\n"
        "test_logs: none\n"
        "baseline_suite: none\n"
        "after_suite: none\n"
        "artifacts: none\n"
        f"evidence_inputs: {evidence}\n"
        "follow_ons: none\n"
        "blockers: replace this derived artifact with a worker-written manifest\n"
    )


def _derive_missing_manifest(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """Preserve terminal evidence without turning it into delivered work."""
    fences = (config or {}).get("fences") or {}
    if fences.get("manifest_required", True) is False:
        return record
    if str(record.get("phase") or "") not in {"complete", "failed"}:
        return record
    manifest_value = str(record.get("manifest_path") or "")
    if not manifest_value:
        return record
    manifest = Path(manifest_value)
    if manifest.exists():
        return record
    if _commits_beyond_base(record):
        # The work is already committed past the recorded base: git is the
        # evidence, and no recovery artifact should be fabricated over it with
        # a "commits: none" that the history contradicts.
        return record
    paths = _worktree_diff_paths(record)
    final_message = str(record.get("final_message") or "").strip()
    if not paths and not final_message:
        return record
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with manifest.open("x", encoding="utf-8") as handle:
            handle.write(_derived_manifest_text(record, paths))
    except FileExistsError:
        # Worker delivery won the race and remains authoritative.
        return record

    run_id = str(record.get("run_id") or "")

    def record_gap(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["delivery_gap"] = {
            "kind": "missing-worker-manifest",
            "derived_manifest_path": str(manifest),
            "derived_at": _utc_now(),
            "final_message_present": bool(final_message),
            "changed_paths": paths,
        }
        return pointer

    return _mutate_pointer(run_id, record_gap) if run_id else record


def closure_disposition_valid(disposition: str, classification: str) -> bool:
    """Whether a recorded closure disposition excuses a pointer from the fences.

    This is the single definition of ``reconciled`` shared by the closure drain
    and the dispatch fence, so a pointer the drain counts as reconciled is never
    refused by dispatch, and a pointer either surface still calls unreconciled
    is refused on both. A ``handed-off`` disposition remains valid until the
    receiving session reconciles the pointer; ``still-working`` excuses only a
    pointer whose current classification is still ``running``. Any missing,
    malformed or unknown disposition, or a disposition outlived by its run, is
    not valid.
    """
    return disposition == "handed-off" or (
        disposition == "still-working" and classification == "running"
    )


def _partition_session_rows(
    rows: Iterable[Mapping[str, Any]], session: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate one coordinator's rows from visible peer-session rows.

    Omitting a session preserves the project-wide interpretation: every row is
    counted and there is no peer partition. Supplying one uses the dispatching
    session already persisted on each pointer, so aiming the fence adds no
    ownership state of its own. A legacy row with no recorded owner remains in
    the counted set: absence cannot prove that a live pointer belongs to a peer.
    """
    copied = [dict(row) for row in rows]
    if session is None:
        return copied, []
    own: list[dict[str, Any]] = []
    peers: list[dict[str, Any]] = []
    for row in copied:
        owner = str(row.get("session") or "")
        (own if not owner or owner == session else peers).append(row)
    return own, peers


def overdue_unreconciled_runs(
    *,
    project: str,
    grace: str,
    now_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return actionable terminal pointers older than the configured grace.

    A pointer is listed only when it is terminal past the grace AND not excused
    by a recorded closure disposition: the same predicate the closure drain
    uses, so a past-grace pointer that carries no valid disposition is still
    refused here (the forgotten-work case) while one the drain counts as
    reconciled is never refused.
    """
    if not grace:
        return []
    grace_seconds = parse_duration(grace)
    rows = []
    for pointer in list_live(project=project):
        row = classify_pointer(pointer, now_seconds=now_seconds)
        age = row.get("terminal_age_seconds")
        if (
            row["classification"]
            in {"scoring", "promotable", "completed_unpromoted", "blocked", "paused"}
            and isinstance(age, int)
            and age > grace_seconds
        ):
            recorded = pointer.get("closure_disposition")
            disposition = (
                str(recorded.get("kind") or "") if isinstance(recorded, Mapping) else ""
            )
            if not closure_disposition_valid(disposition, row["classification"]):
                rows.append(row)
    return rows


def _utc_seconds() -> float:
    """Current time as epoch seconds, matching a file mtime's clock."""
    return datetime.now(tz=timezone.utc).timestamp()


@contextmanager
def _watch_registration(project: str, stall_window: str):
    """Register a watcher together with the process responsible for reaping it."""
    with _project_watch_claim(project, stall_window) as (acquired, watcher):
        if acquired:
            parent_pid = os.getppid()
            watcher.update(
                {
                    "parent_pid": parent_pid,
                    "parent_start_time": _process_start_time(parent_pid),
                }
            )
            with watch_lock_path(project).open("r+b") as handle:
                _write_watch_record(handle, watcher)
        yield acquired, watcher


def unwatch(project: str) -> dict[str, Any]:
    """Stop the registered watcher for one project and release its claim."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            watcher = _read_watch_record(handle)
            registered_project = str(watcher.get("project") or "")
            if registered_project != project:
                raise CrewError(
                    f"refusing to stop watcher for project {project!r}: "
                    f"the locked registration names {registered_project!r}"
                )
            try:
                pid = int(watcher.get("pid"))
            except (TypeError, ValueError) as exc:
                raise CrewError(
                    f"refusing to stop watcher for project {project!r}: "
                    "the locked registration has no valid pid"
                ) from exc

            try:
                _signal_process_group(pid, watcher.get("pid_start_time"))
            except ProcessLookupError:
                stopped = False
                reason = "watcher-exited"
                detail = (
                    f"watcher pid {pid} exited before it could be signalled; "
                    "its registration was released"
                )
            else:
                stopped = True
                reason = "stopped"
                detail = f"stopped registered watcher pid {pid}"

            # The watcher owns this lock until its process exits. Taking it
            # before clearing the record makes registration release observable
            # to a subsequent arming command, without replacing the lock inode.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _write_watch_record(handle, {})
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return {
                "project": project,
                "stopped": stopped,
                "registration_released": True,
                "reason": reason,
                "detail": detail,
                "watcher": watcher,
            }

        watcher = _read_watch_record(handle)
        _write_watch_record(handle, {})
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "project": project,
            "stopped": False,
            "registration_released": True,
            "reason": "nothing-to-stop",
            "detail": f"project {project!r} has no registered watcher to stop",
            "watcher": watcher,
        }


def agent_label(pointer: Mapping[str, Any]) -> str:
    """Compact `model/effort` — or `alias·effort` — for the ticker.

    Read from the configuration persisted at dispatch rather than from current
    flight config, because a later config change must not silently restate what
    ran. The alias and its effort spelling are display decisions frozen at
    dispatch, so an aliased run renders the alias in place of the model it
    shortens; the composition is the renderer's so the two cannot drift. A run
    dispatched before aliases existed carries no alias and keeps the
    precomposed `model/effort` form it rendered then. Absent fields are simply
    omitted: a partial label is still useful and an invented one is not.
    """
    agent = pointer.get("agent")
    if not isinstance(agent, Mapping):
        return ""
    alias = str(agent.get("alias") or "").strip()
    if not alias:
        model = str(agent.get("model") or "").strip()
        effort = str(agent.get("effort") or "").strip()
        if model and effort:
            return f"{model}/{effort}"
        return model or effort
    return _agent_label(agent)


def _pointer_role(pointer: Mapping[str, Any]) -> str:
    """The dispatch role a run carried, from its own record.

    Read from the persisted pointer rather than current config for the same
    reason :func:`agent_label` reads its agent block from the record: a later
    role change must not restate what actually ran. Dispatch writes the role on
    the record root and on the node, so either spelling is accepted. The display
    narrowing (``documentation`` to ``docs``, unknown to the marker) happens in
    the renderer where the column lives; the snapshot threads the raw spelling.
    """
    role = str(pointer.get("role") or "").strip()
    if not role:
        role = str(((pointer.get("node") or {}) or {}).get("role") or "").strip()
    return role


# A state that needs action always may explain itself, and so may a member of
# the waiting family: the clause on a waiting row names what lifts it and on a
# blocked row names what a reader can do, so the explained set is the action
# set plus the self-lifting family — an overdue wait is explained by both
# routes at once. An unreadable manifest is one of the actionable states,
# because the refusal text naming the rejected format is the one sentence a
# reader needs before repairing the file.
EXPLAINED_STATES = frozenset(
    NEEDS_ACTION | WAITING_STATES | {"unreadable", "unwritten"}
)


def _watch_snapshot(
    pointer: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares."""
    row = classify_pointer(
        pointer,
        now_seconds=moment,
        stale_after_seconds=stall_seconds,
    )
    phase = str(pointer.get("phase") or "")
    classification = str(row.get("classification") or "")
    alive = row.get("process_alive")

    # The working bucket is keyed on a process that is genuinely still alive,
    # never on the record phase alone: a run whose process died at any phase it
    # held — the starting phase included — has stopped working regardless of the
    # label the last writer left behind. classify_pointer has already checked
    # the process table, so a dead process falls through to the abandoned state
    # a coordinator must act on instead of the stale working label. A manifest
    # that has reached a verdict likewise cannot keep a run in working, so the
    # terminal readings are arbitrated before any working state is chosen. The
    # classifier also defers complete and failed reports while the pointer says
    # their process is alive, so this reducer consumes that decision instead of
    # deriving a second verdict from the manifest.
    if classification == "scoring":
        state = "completed_unpromoted"
    elif classification in {"promotable", "completed_unpromoted"}:
        state = "complete"
    elif classification == WAITING_STATUS:
        # A declared external wait stays in the waiting family even when it has
        # aged past its expectation — the run has not failed, so the fleet keeps
        # counting it as waiting rather than blocked. The age is the news, and
        # the news is carried by the action marker on the row a reader sees.
        state = "wait-aged" if row.get("wait_overdue") else WAITING_STATUS
    elif classification == "paused":
        # A paused run is a member of the waiting family: nothing needs a
        # person, so it renders under the same calm verb as a declared external
        # wait and its classifier detail names what lifts it. Rendering it as
        # its own grid word would need a second routing route with no reader
        # benefit — the distinction that matters is that it is not blocked.
        state = WAITING_STATUS
    elif classification == INTERRUPTED_RUN_PHASE:
        # The compatibility watch vocabulary does not yet expose interruptions
        # as their own column. Keep the run in the needs-action bucket while the
        # row's recovery classification and action retain the precise cause.
        state = "blocked"
    elif classification in {"blocked", "failed"}:
        # A provider refusal blocks even though no manifest reached a verdict:
        # the process is gone, but the stop is triageable and resumable once
        # the limit lifts, so it reads as a block rather than an abandonment.
        state = classification
    elif classification == "unreadable":
        # A manifest that is present but unreadable is neither a delivery nor
        # an absence, so the run reads as unreadable rather than falling into
        # the abandoned bucket the liveness checks below would assign it.
        state = "unreadable"
    elif str(row.get("recovery_classification") or "") == "unwritten":
        # The compatibility state stays non-terminal while the typed surface
        # names that the worker never replaced its template. This keeps a
        # placeholder from satisfying a terminal fence without inventing a
        # second attention vocabulary in the run registry.
        state = "running"
    elif phase == "stopped":
        state = "stopped"
    elif alive is False:
        # Abandoned means the worker died with nothing of the run surviving it.
        # A dead process whose manifest reported it was still working, or whose
        # worktree carries commits past its base, left work that outlived the
        # process: the classifier reads it running, and this reducer must not
        # paint the same run dead or the pane would disagree with the reader.
        if classification == "running" and (
            (row.get("manifest_status") or "") in NON_TERMINAL_MANIFEST_STATUSES
            or row.get("commits_beyond_base")
        ):
            state = "working"
        else:
            state = "abandoned"
    elif classification == "running" or phase in {"working", "running"}:
        state = "dispatched" if phase == "starting" else "working"
    else:
        state = classification or phase or "unknown"

    detail = str(row.get("detail") or "")
    for prefix in (
        "the worker manifest reports blocked: ",
        "the worker manifest reports failed: ",
    ):
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
            break

    if state in ("dispatched", "working"):
        # A run stops progressing whether it dies during dispatch or mid-work,
        # so the stall check has to reach every non-terminal state a pointer
        # can sit in — gating it on "working" alone left a run killed before
        # its phase ever advanced past "starting" permanently exempt.
        quiet = _stream_quiet_seconds(pointer, now_seconds=moment)
        if quiet > stall_seconds:
            # A quiet stream is a hang only when nothing is waiting. An alive
            # worker sitting in a bounded wait — a sleep, a peer read, a task
            # wait, a rejected window, or a rate-limit retry loop — wakes
            # itself, so it pauses rather than stalling; a genuinely hung
            # process with none of those still stalls and is not weakened here.
            wait = _stall_wait_reason(pointer)
            if wait is not None:
                state = WAITING_STATUS
                detail = f"paused: sitting in {wait} for {quiet}s; it lifts itself"
            else:
                state = "stalled"
                detail = f"stream quiet for {quiet}s"
        else:
            detail = ""
    elif state not in EXPLAINED_STATES:
        # Named as the states that MAY explain themselves rather than the ones
        # that may not. An allow-list of states to clear leaves every state
        # added later carrying whatever the classifier attached, which makes
        # routine progress read as a warning.
        detail = ""

    recovery_classification = str(row.get("recovery_classification") or state)
    recovery_verb = str(row.get("recovery") or "")
    lifting_condition = row.get("lifting_condition")
    if state == "stalled":
        recovery_classification = "stalled"
        recovery_verb = RECOVERY_VERBS["stalled"]
        lifting_condition = None
    elif state == "wait-aged":
        recovery_classification = "wait-aged"
        recovery_verb = RECOVERY_VERBS["wait-aged"]
    elif state == WAITING_STATUS and classification == "running":
        recovery_classification = "paused"
        recovery_verb = RECOVERY_VERBS["paused"]
        lifting_condition = detail

    # What ran it, as facts rather than a display string. The alias and effort
    # spelling were decided at dispatch and frozen onto the pointer; a later
    # configuration edit must not restate what ran, so the facts are read from
    # the record. Composition is the renderer's, so the model and effort travel
    # separately and the monitor decides how they read.
    agent_map = (
        pointer.get("agent") if isinstance(pointer.get("agent"), Mapping) else {}
    )
    return {
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or row.get("run_id") or "unknown"),
        # The dispatching session, so a reader can tell its own fleet from a
        # peer's on a stream that is necessarily project-wide.
        "session": str(pointer.get("session") or ""),
        "backend": str(agent_map.get("backend") or "").strip(),
        "model": str(agent_map.get("model") or "").strip(),
        "effort": str(agent_map.get("effort") or "").strip(),
        "alias": str(agent_map.get("alias") or "").strip(),
        # What kind of work it is, on the record the same way. Read beside the
        # agent because the two describe the same run and are reduced the same
        # way — the snapshot carries the raw spelling and the renderer narrows
        # it to fit its column.
        "role": _pointer_role(pointer),
        # Whether this run is a shadow of a committed primary. Dispatch decides
        # shadowship at launch and writes the lineage onto the pointer; the
        # renderer dims a shadow row end to end from that fact, so the snapshot
        # carries it under its own name rather than as a flattened display flag.
        "lineage": pointer.get("lineage"),
        "state": state,
        "recovery_classification": recovery_classification,
        "recovery": recovery_verb,
        "lifting_condition": lifting_condition,
        "resets_at": row.get("resets_at"),
        "next_action": row.get("next_action"),
        # The full, untruncated reason. The bounded clause a reader can act on
        # is derived from it at render time, so nothing here is shaped for the
        # grid before it is stored.
        "detail": detail,
        # The fact a block's glyph is derived from. Only a "blocked" state
        # carries a marker; a run entering any other state has nothing for the
        # reader to answer or read a manifest for.
        "needs_help_complete": row.get("needs_help_complete"),
        "wait_overdue": row.get("wait_overdue"),
        # The rest of a declared wait travels beside its probe's verdict:
        # whether the condition probe has run at all and what it last observed,
        # the horizon the wait was declared against, and the brief a resumed
        # worker reads. A run that declares no wait has taken no measurement, so
        # None is the explicit unmeasured state and a zero never stands in for
        # one.
        "wait_condition_state": row.get("wait_condition_state"),
        "wait_observed": row.get("wait_observed"),
        "expected_horizon_seconds": (row.get("external_wait") or {}).get(
            "expected_horizon_seconds"
        ),
        "resume_brief": (row.get("external_wait") or {}).get("resume_brief"),
        # The manifest facts the fold needs to detect a rewrite: the effective
        # status (empty while a live process defers a terminal report, so a
        # deferred report never reads like a verdict), the commit list, and the
        # content digest that distinguishes a changed rewrite from a touch.
        "manifest_status": str(row.get("manifest_status") or ""),
        "manifest_commits": list(row.get("manifest_commits") or []),
        "manifest_digest": row.get("manifest_digest"),
    }


# The ordinary three buckets remain unchanged when no external wait exists. A
# waiting bucket appears while at least one declared condition is outstanding,
# keeping healthy waits out of both work-in-progress and needs-action figures.
# Every snapshot belongs to exactly one bucket, so the figures still add up.
FLEET_WORKING_STATES = ("dispatched", "working", "running")
FLEET_UNPROMOTED_STATES = ("complete", "completed_unpromoted")
FLEET_WAITING_STATES = tuple(sorted(WAITING_STATES))
# The blocked bucket is the action set minus the waiting family. The action set
# is the marker set — every state whose row a reader should look at, an overdue
# wait included — but the counter says what kind of run this is, and an overdue
# wait is still waiting. Count and marker therefore separate on that one
# member: what the number reports as blocked is what a reader must act on
# excluding a run that is legitimately still in the waiting column.
FLEET_BLOCKED_STATES = tuple(sorted(NEEDS_ACTION - WAITING_STATES))


def _fleet_counts(
    snapshots: Mapping[str, Mapping[str, Any]], *, session: str | None = None
) -> dict[str, int]:
    """Partition the fleet into working, blocked, delivered, and waiting work.

    ``working`` is what a reader means by a live worker. ``blocked`` is
    everything that has stopped progressing and needs the coordinator, a stall
    or a failure included. ``unpromoted`` is delivered work waiting on a gate.
    ``waiting`` is a run whose declared external condition remains outstanding.
    A run that leaves the fleet is in none of them.

    ``session`` narrows the counted population to the runs that session owns,
    using the ownership already persisted on each snapshot; omitted, the
    partition covers the whole fleet, preserving the project-wide reading. A
    legacy snapshot with no recorded owner stays in the counted set: absence
    cannot prove that a live pointer belongs to a peer.
    """
    rows, _peers = _partition_session_rows(snapshots.values(), session)
    states = [str(snapshot.get("state") or "") for snapshot in rows]
    held = sum(
        str(snapshot.get("recovery_classification") or "") == "held"
        for snapshot in rows
    )
    counts = {
        "working": sum(state in FLEET_WORKING_STATES for state in states),
        "blocked": sum(
            str(snapshot.get("state") or "") in FLEET_BLOCKED_STATES
            and str(snapshot.get("recovery_classification") or "") != "held"
            for snapshot in rows
        ),
        "unpromoted": sum(state in FLEET_UNPROMOTED_STATES for state in states),
    }
    waiting = (
        sum(
            str(snapshot.get("state") or "") in FLEET_WAITING_STATES
            and str(snapshot.get("recovery_classification") or "") != "held"
            for snapshot in rows
        )
        + held
    )
    if waiting:
        counts["waiting"] = waiting
    return counts


def _manifest_rewritten(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """Whether the report beneath an unchanged verdict was replaced.

    A terminal manifest is the worker's own report and the fold emits one
    state change per verdict, so a worker that replaces that report — the
    measured case: an empty-commit failed placeholder overwritten eighteen
    minutes later by the real failed manifest — must still surface, or the
    coordinator holds the first reading forever. The signal is the content
    digest, not the mtime: a digest fires only when the rewrite changed
    something, while mtime alone fires on a touch of identical content, and a
    transition without news is the noise this display exists to avoid. The
    accepted cost is the opposite hole, a legitimate rewrite to byte-identical
    content goes unseen — it carries no news to deliver, so there is nothing
    a reader should be woken for.

    Both readings must be an effective terminal verdict: a live process's
    terminal report is deferred, so its rewrites stay silent until the process
    dies, and an in-progress manifest is progress, not a verdict.
    """
    if str(previous.get("manifest_status") or "") not in TERMINAL_MANIFEST_STATUSES:
        return False
    if str(current.get("manifest_status") or "") not in TERMINAL_MANIFEST_STATUSES:
        return False
    previous_digest = previous.get("manifest_digest")
    current_digest = current.get("manifest_digest")
    return bool(
        previous_digest and current_digest and previous_digest != current_digest
    )


def fleet_transitions(
    known: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], str | None, str, dict[str, int]]],
    dict[str, dict[str, Any]],
]:
    """Fold one fleet observation into ordered transitions and the next state.

    The counts travel per transition, recomputed after each one is applied,
    because a line's numbers are read as the fleet *at that line*. Stamping one
    batch-wide count on every line of a multi-transition poll describes the end
    of the batch instead: a promotion would report the fleet it had already
    left, and three simultaneous landings would all claim the third one's
    totals.

    Departures first, then arrivals, then state changes — a promotion frees its
    slot before the next dispatch is counted into it, which is the order a
    reader infers from the numbers. A manifest rewrite that leaves the state
    unchanged is folded after the state changes of the same observation: its
    classification word did not move, so nothing else about the run could have
    either.
    """
    running = {run_id: dict(snapshot) for run_id, snapshot in known.items()}
    changes: list[tuple[Mapping[str, Any], str | None, str]] = []

    for run_id in (item for item in known if item not in current):
        # A departure is its own fact and inherits no clause or marker from the
        # state it left. Carrying one forward reports a block on the line
        # announcing that the block is over.
        departed = {**known[run_id], "detail": "", "needs_help_complete": None}
        changes.append((departed, str(known[run_id]["state"]), "promoted"))
    for run_id in (item for item in current if item not in known):
        changes.append(
            (
                {
                    **current[run_id],
                    "state": "dispatched",
                    "detail": "",
                    "needs_help_complete": None,
                },
                None,
                "dispatched",
            )
        )
    for run_id in (item for item in current if item in known):
        previous = str(known[run_id]["state"])
        state = str(current[run_id]["state"])
        previous_recovery = str(
            known[run_id].get("recovery_classification") or previous
        )
        current_recovery = str(current[run_id].get("recovery_classification") or state)
        if state != previous or current_recovery != previous_recovery:
            changes.append((current[run_id], previous, state))
        elif _manifest_rewritten(known[run_id], current[run_id]):
            # The classification word did not move but the report it sits on
            # did. The emitted snapshot is marked so the event builder records
            # the rewrite as its own kind; the run's memory keeps the clean
            # copy so the marker never leaks into a later departure.
            rewritten = dict(current[run_id])
            rewritten["manifest_rewritten"] = True
            changes.append((rewritten, previous, state))
            running[run_id] = dict(current[run_id])

    events: list[tuple[dict[str, Any], str | None, str, dict[str, int]]] = []
    for snapshot, previous, state in changes:
        run_id = str(snapshot.get("run_id") or "")
        if state == "promoted":
            running.pop(run_id, None)
        elif not snapshot.get("manifest_rewritten"):
            running[run_id] = dict(snapshot)
        events.append((dict(snapshot), previous, state, _fleet_counts(running)))
    return events, running


def _watch_transition(
    project: str,
    *,
    kind: str,
    snapshot: Mapping[str, Any],
    previous: str | None,
    current: str,
    counts: Mapping[str, int],
    spend_runs: Sequence[Mapping[str, Any]] | None = None,
    rate_statuses: Mapping[str, Any] | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build one lossless transition object for text or JSON rendering.

    This is the surface the events log persists, so it carries facts only: the
    model, effort, alias and backend separately, the full untruncated detail,
    the structured fact a block's glyph is derived from, and the run's
    cumulative spend — wall seconds, model seconds, charged tokens, generation
    rate and notional cost as separate numeric facts. No composed label, no
    pre-claused reason and no display glyph are written here — the monitor
    derives those from these facts, so the log stays re-renderable.

    ``spend_runs`` is the record set the accumulator folds (the live fleet, or
    rows already promoted); omitted, the project's own live pointers are read.
    ``rate_statuses`` maps a backend to its dated rate standing for the notional
    cost figure; omitted, the resolved configuration is read.
    """
    event = {
        "project": project,
        "event": kind,
        "observed_at": _utc_now(),
        "run_id": snapshot.get("run_id"),
        "node": snapshot.get("node"),
        "session": snapshot.get("session") or "",
        "role": snapshot.get("role") or "",
        # The shadow lineage the snapshot carried from the pointer, threaded
        # through the field-by-field rebuild so the events log records the same
        # fact the renderer reads to dim the row.
        "lineage": snapshot.get("lineage"),
        "backend": str(snapshot.get("backend") or ""),
        "model": str(snapshot.get("model") or ""),
        "effort": str(snapshot.get("effort") or ""),
        "alias": str(snapshot.get("alias") or ""),
        "from_state": previous,
        "to_state": current,
        "working": counts["working"],
        "blocked": counts["blocked"],
        "unpromoted": counts["unpromoted"],
        "detail": str(snapshot.get("detail") or ""),
        "recovery_classification": str(
            snapshot.get("recovery_classification") or current
        ),
        "recovery": str(snapshot.get("recovery") or ""),
        "lifting_condition": snapshot.get("lifting_condition"),
        "resets_at": snapshot.get("resets_at"),
        "next_action": snapshot.get("next_action"),
        # The declared wait's own facts, beside the condition that lifts it: the
        # probe's verdict and last observation, the horizon the wait was
        # declared against, and the brief a resumed worker reads. Carried whole
        # so a renderer can tell a probe that never ran from one still pending,
        # and can hold the wait's age against its horizon without re-deriving
        # either. None is the explicit unmeasured state — a run that declares no
        # wait has taken no measurement, so no zero stands in for one.
        "wait_condition_state": snapshot.get("wait_condition_state"),
        "wait_observed": snapshot.get("wait_observed"),
        "wait_overdue": snapshot.get("wait_overdue"),
        "expected_horizon_seconds": snapshot.get("expected_horizon_seconds"),
        "resume_brief": snapshot.get("resume_brief"),
        "needs_help_complete": snapshot.get("needs_help_complete"),
    }
    if snapshot.get("manifest_rewritten"):
        # A rewrite of the report beneath an unchanged verdict: from_state and
        # to_state are the same word, so the distinct kind is what separates
        # this event from one where the classification moved. The new report's
        # facts ride along, so a reader sees what changed rather than only that
        # something changed.
        event["event"] = "manifest-rewritten"
        event["manifest_status"] = str(snapshot.get("manifest_status") or "")
        event["manifest_commits"] = list(snapshot.get("manifest_commits") or [])
        event["commit_count"] = len(event["manifest_commits"])
    if "waiting" in counts or previous in WAITING_STATES or current in WAITING_STATES:
        event["waiting"] = counts.get("waiting", 0)
    event.update(
        _spend_facts(
            project,
            snapshot,
            spend_runs=spend_runs,
            rate_statuses=rate_statuses,
            streams_root=streams_root,
        )
    )
    return event


def _spend_facts(
    project: str,
    snapshot: Mapping[str, Any],
    *,
    spend_runs: Sequence[Mapping[str, Any]] | None = None,
    rate_statuses: Mapping[str, Any] | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """The transition's cumulative spend as separate, re-renderable facts.

    Every figure is written numerically — never as a pre-formatted string — and
    ``None`` is the explicit unmeasured state for each derived figure, because a
    zero would assert a measurement that was never taken. Tokens are the total a
    meter charges (input including cache reads, plus output); the rate is
    generated output over model seconds.
    """
    run_id = str(snapshot.get("run_id") or "")
    facts: dict[str, Any] = {
        "spend_wall_seconds": None,
        "spend_model_seconds": None,
        "spend_machine_seconds": None,
        "spend_charged_tokens": None,
        "spend_generation_rate": None,
        "spend_notional_cost_usd": None,
        "spend_folded_run_count": 0,
        "spend_measured_stream_count": 0,
        "spend_unmeasured_stream_count": 0,
    }
    if not run_id:
        return facts
    rows = spend_runs
    if rows is None:
        rows = runs._list_live_records(project=project)
    spend = metering.accumulate_run_spend(rows, run_id, streams_root=streams_root)
    if not isinstance(spend, metering.AccumulatedRunSpend):
        return facts
    measured = spend.measured_stream_count > 0
    facts.update(
        {
            "spend_folded_run_count": spend.folded_run_count,
            "spend_measured_stream_count": spend.measured_stream_count,
            "spend_unmeasured_stream_count": spend.unmeasured_stream_count,
            "spend_wall_seconds": spend.elapsed_seconds,
            "spend_model_seconds": spend.generation_seconds,
            "spend_machine_seconds": spend.machine_seconds,
            "spend_charged_tokens": spend.total_charged_tokens if measured else None,
            "spend_generation_rate": _generation_rate(spend, measured),
            "spend_notional_cost_usd": _notional_cost(snapshot, spend, rate_statuses),
        }
    )
    return facts


def _generation_rate(
    spend: metering.AccumulatedRunSpend, measured: bool
) -> float | None:
    """Generated tokens over model seconds, or None when either is unknown.

    A measured zero model span is still a span a rate could be divided from, so
    only a strictly positive span rates a denominator; an unmeasured chain never
    fabricates a rate from a zero it did not observe.
    """
    if (
        not measured
        or spend.generation_seconds is None
        or spend.generation_seconds <= 0
    ):
        return None
    return spend.cumulative_output_tokens / spend.generation_seconds


def _notional_cost(
    snapshot: Mapping[str, Any],
    spend: metering.AccumulatedRunSpend,
    rate_statuses: Mapping[str, Any] | None,
) -> float | None:
    """The run's notional dollar figure from declared rates, or None unpriced.

    The figure is computed from public rates and measured tokens, never read
    from the harness — which prices whatever model name it was told to speak and
    therefore ranks the free local lane as the most expensive backend. A lane
    with no dated rate pair stays explicitly unpriced, and a chain with no
    measured stream stays unmeasured.
    """
    if spend.measured_stream_count == 0:
        return None
    if rate_statuses is None:
        rate_statuses = quota_weight.backend_rate_statuses()
    status = rate_statuses.get(str(snapshot.get("backend") or ""))
    priced = bool(getattr(status, "priced", False))
    rate = getattr(status, "rate", None) if priced else None
    if rate is None:
        return None
    return round(
        spend.cumulative_input_tokens / 1_000_000 * rate.input_per_million
        + spend.cumulative_output_tokens / 1_000_000 * rate.output_per_million,
        2,
    )


# Rendering a transition is a layout concern with its own contract, so it lives
# beside the grid it fills. The plain default keeps this module's callers, and
# every test that reads a line as a string, free of escape sequences.
_PLAIN = Ticker()


def _scoped_watch_event(event: Mapping[str, Any], session: str) -> dict[str, Any]:
    """Re-derive a transition's figures over one session's live pointers.

    The watcher seat is project-global and the stream it writes carries the
    whole fleet's totals, so a follower cannot ask for per-session figures on
    the wire; the population is re-selected here at render time from the same
    live pointers, using the ownership already persisted on each pointer. A
    legacy pointer with no recorded owner stays counted, matching the dispatch
    fence.
    """
    project = str(event.get("project") or "")
    stall_seconds = parse_duration(DEFAULT_WATCH_STALL_WINDOW)
    moment = _utc_seconds()
    pointers = runs._list_live_records(project=project) if project else ()
    current = {
        str(pointer.get("run_id") or ""): _watch_snapshot(
            pointer, moment=moment, stall_seconds=stall_seconds
        )
        for pointer in pointers
        if pointer.get("run_id")
    }
    counts = _fleet_counts(current, session=session)
    scoped = dict(event)
    scoped["working"] = counts["working"]
    scoped["blocked"] = counts["blocked"]
    scoped["unpromoted"] = counts["unpromoted"]
    previous = scoped.get("from_state")
    state = scoped.get("to_state")
    if "waiting" in counts or previous in WAITING_STATES or state in WAITING_STATES:
        scoped["waiting"] = counts.get("waiting", 0)
    elif "waiting" in scoped:
        del scoped["waiting"]
    return scoped


def format_watch_transition(
    event: Mapping[str, Any],
    *,
    with_session: bool = False,
    ticker: Ticker | None = None,
    session: str | None = None,
) -> str:
    """Render one transition as the compact human-facing watch line.

    ``ticker`` supplies a caller's own grid — the CLI passes one carrying the
    reader's width, theme and colour choice. Omitted, the shared plain grid
    renders, because there is no terminal to detect: the pane is a pipe, so
    colour is a decision a caller makes rather than one this module can infer.

    ``session`` re-scopes the line's figures to the runs that session owns. A
    session-scoped follower relays a project-wide stream whose every event
    carries the fleet's totals; re-selecting the population before rendering is
    what makes its trailing figures describe the runs it is following. Omitted,
    the line shows the figures the event arrived with, unchanged.
    """
    if event.get("legacy"):
        return str(event.get("rendered") or "")
    if session is not None:
        event = _scoped_watch_event(event, session)
    return (ticker or _PLAIN).render(event, with_session=with_session)


def _refuse_unresolvable_watch(project: str) -> None:
    """Refuse to arm a watcher whose project routes to a missing backend.

    A watcher that cannot resolve a backend it may be asked to lift reads as
    armed and loses every park it lifts, leaving a 0-byte stream per tick while
    the pointer stays working. The check runs before the registration is taken,
    so the seat is never held by a watcher that cannot do its job.
    """
    from reckon.crew.dispatch import assert_routable_backends_resolvable

    assert_routable_backends_resolvable(project, _resolved_review_config(project, None))


def watch_ticker(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[dict[str, Any]]:
    """Yield a baseline and then every observed fleet state transition."""
    _refuse_unresolvable_watch(project)
    stall_seconds = parse_duration(stall_window)
    known: dict[str, dict[str, Any]] = {}
    fleet_seen = False
    # Rate standings move on their own cadence, so one resolution serves the
    # whole watch session rather than a config read per transition.
    rate_statuses = quota_weight.backend_rate_statuses()

    with _watch_registration(project, stall_window) as (acquired, watcher):
        if not acquired:
            yield {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
            }
            return

        while True:
            pointers = list_live(project=project)
            moment = _utc_seconds()
            current = {
                str(pointer.get("run_id") or ""): _watch_snapshot(
                    pointer, moment=moment, stall_seconds=stall_seconds
                )
                for pointer in pointers
                if pointer.get("run_id")
            }
            if not current and not fleet_seen:
                sleeper(poll_interval)
                continue

            counts = _fleet_counts(current)
            if not fleet_seen:
                fleet_seen = True
                known = {run_id: dict(snapshot) for run_id, snapshot in current.items()}
                for snapshot in current.values():
                    yield _watch_transition(
                        project,
                        kind="baseline",
                        snapshot=snapshot,
                        previous=None,
                        current=str(snapshot["state"]),
                        counts=counts,
                        spend_runs=pointers,
                        rate_statuses=rate_statuses,
                    )
                continue

            folded, next_known = fleet_transitions(known, current)
            events = [
                _watch_transition(
                    project,
                    kind="transition",
                    snapshot=snapshot,
                    previous=previous,
                    current=state,
                    counts=event_counts,
                    spend_runs=pointers,
                    rate_statuses=rate_statuses,
                )
                for snapshot, previous, state, event_counts in folded
            ]
            known = next_known
            if events:
                yield from events
                if not current:
                    return
                continue
            sleeper(poll_interval)


def watch_follow(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    transitions: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield each newly terminal run, or the full transition stream on request.

    An empty project remains armed until its first pointer appears. Once a
    fleet has appeared, removing its last pointer ends the stream. Terminal
    and stalled run ids are remembered so an unreconciled pointer cannot
    repeatedly wake the watcher or hide a later run.
    """
    if transitions:
        yield from watch_ticker(
            project,
            stall_window=stall_window,
            poll_interval=poll_interval,
            sleeper=sleeper,
        )
        return

    stall_seconds = parse_duration(stall_window)
    reported_runs: set[str] = set()
    fleet_seen = False

    with _watch_registration(project, stall_window) as (acquired, watcher):
        if not acquired:
            yield {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
            }
            return

        while True:
            pointers = list_live(project=project)
            if not pointers:
                if fleet_seen:
                    return
                sleeper(poll_interval)
                continue
            fleet_seen = True

            moment = _utc_seconds()
            classified = [
                (pointer, classify_pointer(pointer, now_seconds=moment))
                for pointer in pointers
            ]
            for _pointer, row in classified:
                run_id = str(row.get("run_id") or "")
                if run_id not in reported_runs and row.get("manifest_status") in {
                    "complete",
                    "blocked",
                    "failed",
                }:
                    reported_runs.add(run_id)
                    yield {"project": project, "event": "terminal", **row}
                    break
            else:
                for pointer, row in classified:
                    run_id = str(row.get("run_id") or "")
                    if run_id not in reported_runs and row.get(
                        "manifest_status"
                    ) not in {"complete", "blocked", "failed"}:
                        quiet = _stream_quiet_seconds(pointer, now_seconds=moment)
                        # A quiet stream sleeping in a bounded wait is paused,
                        # not stalled, so it must not wake the follower the way
                        # a hang does — the same correction the ticker applies.
                        # Only a live process can be sitting in the wait: a dead
                        # one followed a bounded call no further and is a lost
                        # run the follower must still report.
                        live = row.get("process_alive") is True
                        if quiet > stall_seconds and (
                            not live or _stall_wait_reason(pointer) is None
                        ):
                            reported_runs.add(run_id)
                            yield {
                                "project": project,
                                "event": "stalled",
                                **row,
                                "stalled_for_seconds": quiet,
                            }
                            break
                else:
                    sleeper(poll_interval)


def recover(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Classify every live pointer, repairing the record and launching reviews.

    Each pointer is re-observed first, so the classification rests on the
    current stream and process table rather than on whatever the last writer
    believed. What gets repaired is the *record*: no worktree is removed, no
    process is reaped, and no run is promoted on this command's initiative — a
    completed-but-unpromoted run is reported with its manifest path so the
    orchestrator can promote it deliberately.

    One thing this command does launch, by design: a run in ``scoring`` has a
    complete review dispatch already composed for it, and leaving that command
    as a string for someone to retype is the defect this sweep exists to
    close. The review is dispatched on the same admission path any dispatch
    takes, and a refusal — scope, member, follower, context fit, budget, or an
    unavailable local lane — is recorded against the run with its reason
    rather than swallowed, so the run says why it is still awaiting review
    instead of looking identical to a review that ran and wrote nothing.
    """
    from reckon.crew.dispatch import observe

    reports = []
    scoring: list[dict[str, Any]] = []
    for pointer in list_live():
        if project and str(pointer.get("project") or "") != project:
            continue
        run_id = str(pointer.get("run_id") or "")
        observed: Mapping[str, Any] = pointer
        unreadable = ""
        if run_id:
            try:
                observed = observe(run_id, config=config)
            except CrewError as exc:
                unreadable = str(exc)
        observed = _derive_missing_manifest(observed, config=config)
        report = classify_pointer(observed)
        if unreadable:
            report["detail"] = f"{report['detail']} (stream unreadable — {unreadable})"
        reports.append(report)
        if report["classification"] == "scoring":
            scoring.append(observed)
    counts = {
        name: sum(1 for item in reports if item["classification"] == name)
        for name in (
            "running",
            "scoring",
            "promotable",
            "completed_unpromoted",
            INTERRUPTED_RUN_PHASE,
            "abandoned",
        )
    }
    for name in ("waiting", "paused", "stopped", "blocked", "failed", "unreadable"):
        count = sum(1 for item in reports if item["classification"] == name)
        if count:
            counts[name] = count
    reflex = [
        dispatch_review_for_run(record, config=config, launcher=launcher)
        for record in scoring
    ]
    return {
        "runs": reports,
        "counts": counts,
        "classes": list(RECOVERY_CLASSES),
        "reviews_dispatched": [
            r["review_run_id"] for r in reflex if r.get("dispatched")
        ],
        "reviews_awaiting_lane": [
            r["run_id"] for r in reflex if r.get("awaiting_lane")
        ],
        "reviews_refused": [r for r in reflex if r.get("refused")],
    }
