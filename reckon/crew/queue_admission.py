"""Admit queued local dispatches when the lane's router opens a slot.

One loop, run by the fleet supervisor as a declared service, reads the lane
document and the queued run pointers. Whenever the router grants a session
room it launches that session's highest-ranked queued entry through the same
dispatch path a coordinator uses, so the queued pointer becomes the dispatched
run. A launch the shared path refuses or fails leaves the request queued, with
the failure and its time recorded on it: the shared path rolls a refused spawn
back through ``_release_launch_claim``, which unlinks the pointer, so the
admission pass keeps the pointer's content before it launches and writes it
back when the launch does not survive.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import queue_order, runs
from reckon.crew.dispatch_admission import (
    _LANE_GATE_WAITING_STATES,
    LaneHeld,
    LanePaused,
    _dispatch_fleet_gate,
    _dispatch_lane_gate,
)
from reckon.crew.dispatch_plan import _dispatch_lane_allowance
from reckon.crew.node import BudgetHold, CrewError, PlanReviewMissingError, TaskNode
from reckon.crew.routing import plan_review_units as plan_review_units  # noqa: PLC0414
from reckon.ledger import normalize_section

ADMIT_LOCK_NAME = "admit.lock"
DEFAULT_ADMIT_INTERVAL_SECONDS = 5.0
#: The phase a request carries while it waits for a lane slot. The admission
#: pass admits from this phase, and every reader that reports where a request
#: stands reads its place through :func:`queued_placements`.
QUEUED_PHASE = "queued"
HELD_PHASE = "held"
#: The reason an admission pass records on a request it holds because one of
#: the plan units the request reads carries no stored review. A hold written by
#: a coordinator keeps its own reason and its own life: only a hold carrying
#: this reason and the ``automatic`` mark is released by the pass that finds
#: the review stored.
PLAN_REVIEW_HELD_REASON = "plan-review-owed"
#: The pointer key holding the plan digests a request was queued against: its
#: own section's text and the document text it reads as its contract. Admission
#: compares the plan against these rather than against the plan's whole debt, so
#: an edit elsewhere in the plan holds no request that did not read it.
REVIEW_CONTRACT_KEY = "review_contract"
LAUNCH_FAILED_PHASE = "launch-failed"
#: The request-phase vocabulary, one place every reader of a request's phase
#: takes its words from. ``awaiting-upstream`` is a request whose section
#: declares refs that have not landed; ``blocked-upstream`` is the same request
#: once one of those refs is closed as abandoned or failed, which no admission
#: pass will launch: the wait is not a slot hold and no lane will clear it.
AWAITING_UPSTREAM_PHASE = "awaiting-upstream"
BLOCKED_UPSTREAM_PHASE = "blocked-upstream"
#: The two phases a request carries while it waits on refs rather than on a
#: lane slot. Both are read here rather than spelled out at each filter.
WAITING_UPSTREAM_PHASES = (AWAITING_UPSTREAM_PHASE, BLOCKED_UPSTREAM_PHASE)
#: A run whose phase is one of these will not deliver the section it names, so
#: a ref waiting on that section is blocked rather than pending.
DEAD_RUN_PHASES = frozenset({"failed"})
# A queued request whose last attempt failed for a reason other than a lane
# hold waits this long before it is attempted again, doubling per consecutive
# failure up to the cap. The back-off is bounded so a request that can never
# launch is still retried eventually rather than parked forever.
ADMISSION_RETRY_BACKOFF_SECONDS = 30.0
ADMISSION_RETRY_BACKOFF_CAP_SECONDS = 600.0
#: One JSON line per admission pass whose lane had a queued pointer, appended
#: under the crew home beside ``pick-timings.jsonl``. A pass that holds a
#: request for its session's allowance, its lane's gate, its retry back-off or
#: an earlier request otherwise records nothing, so the log is what names why a
#: request waited. The log is trimmed to this many bytes on every append, so a
#: service that runs for days cannot grow it without limit.
ADMISSION_VERDICT_LOG = "admission-verdicts.jsonl"
ADMISSION_VERDICT_LOG_MAX_BYTES = 1_048_576


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_now() -> str:
    return _now().isoformat()


def _admit_lock_path() -> Path:
    return runs.crew_home() / "locks" / ADMIT_LOCK_NAME


@contextmanager
def _admit_lock():
    """Serialise two admission passes so one queued run launches once."""
    path = _admit_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _inside_run_directory(value: str, directory: Path) -> bool:
    """Whether a declared path lies inside one run's own directory.

    The queued run's id is minted at dispatch, so no coordinator could have
    named a path inside its directory: a path there is a default the plan
    derived for the queued run, and it must not follow the request onto the
    launched run's id. Only an absolute path can be inside the run directory —
    a relative path resolves against the repository, never the run directory.
    """
    if not value:
        return False
    path = Path(value).expanduser()
    if not path.is_absolute():
        return False
    return path.resolve().is_relative_to(directory.resolve())


def _node_from_pointer(pointer: Mapping[str, Any]) -> TaskNode:
    node = dict(pointer.get("node") or {})
    # A manifest path or write path a previous dispatch defaulted into the
    # queued run's own directory is dropped here, so the launched run's plan
    # derives those defaults afresh from its own id. Every other path — a
    # ``--manifest`` the coordinator named elsewhere, a declared write path, the
    # brief the run reads — is carried over unchanged.
    queued_dir = runs.run_dir(str(pointer["run_id"]))
    manifest_path = str(node.get("manifest_path") or "")
    if _inside_run_directory(manifest_path, queued_dir):
        manifest_path = ""
    return TaskNode(
        id=str(node["id"]),
        goal=str(node.get("goal") or ""),
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        after=list(node.get("after") or ()),
        brief=str(node.get("brief") or ""),
        brief_sha256=str(node.get("brief_sha256") or ""),
        brief_path=str(node.get("brief_path") or ""),
        role=str(node.get("role") or "implement"),
        spec_level=str(node.get("spec_level") or ""),
        done_when=str(node.get("done_when") or ""),
        write_paths=[
            str(path)
            for path in (node.get("write_paths") or ())
            if not _inside_run_directory(str(path), queued_dir)
        ],
        time_budget=str(node.get("time_budget") or ""),
        manifest_path=manifest_path,
        negative_control=str(node.get("negative_control") or ""),
        estimated_hours=node.get("estimated_hours"),
        requires_decisions=list(node.get("requires_decisions") or ()),
        peer_scopes=dict(node.get("peer_scopes") or {}),
    )


def _resolve_lane_config(
    pointer: Mapping[str, Any], *, select_local: bool = True
) -> Mapping[str, Any]:
    """Resolve the request's project config, selecting its local lane when needed."""
    from reckon import flight as flight_module
    from reckon.crew_dispatch_commands import _resolved_flight

    config = _resolved_flight(
        flight_module,
        str(pointer["project"]),
        pointer.get("repo"),
        runs.recorded_dispatch_overrides(pointer),
    )
    if select_local and pointer.get("local"):
        config = flight_module.select_local_backend(config)
    return config


def _steering_brief(pointer: Mapping[str, Any]) -> Path | None:
    """The launch-time authority text a queued request carries, if any.

    ``steering.md`` in the request's own run directory is what the launch
    reads, not the brief text the queued pointer carries: the file is where a
    coordinator replaces the node's authority while the request waits. It is
    contract-checked here, inside the admission path and before the launch
    composes anything, so a file that no longer reads as authority text is
    refused and the request returns to the queue with the failure named.
    """
    path = runs.run_dir(str(pointer["run_id"])) / runs.STEERING_FILE
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CrewError(f"the steering file {path} is not readable: {exc}") from exc
    if not text.strip():
        raise CrewError(f"the steering file {path} carries no authority text")
    return path


def _launch_queued(
    pointer: Mapping[str, Any],
    *,
    launcher: Callable[..., int] | None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    backend_override: str = "",
) -> dict[str, Any]:
    """Launch one queued run through the shared dispatch path a coordinator uses.

    ``backend_override`` names the backend this request launches on instead of
    the one it was queued for: a rebind of a request that never launched, so the
    request is re-composed from the recorded node and its recorded dispatch
    options (its waivers among them) rather than the backend it originally named.
    """
    from reckon.crew.dispatch import dispatch

    options = dict(pointer.get("dispatch_options") or {})
    node = _node_from_pointer(pointer)
    # The request's steering file is the authority the worker reads, so the
    # node is pointed at it in place of the brief the queue-time pointer
    # carried. A request whose authority is a plan section alone has no such
    # file and launches from its section as it always has.
    steering = _steering_brief(pointer)
    if steering is not None:
        node.brief = str(steering)
        node.brief_path = str(steering)
        node.brief_sha256 = ""
    # Keep the request visible while dispatch checks its own identity and
    # other claims. A refused launch leaves the same pointer for dependents.
    # An operator may discard the request this pass is holding. A later launch
    # failure can remove its run directory, including a discard marker, so copy
    # that marker to the shared spool before handing the request to dispatch.
    from reckon.crew.promotion import preserve_discard_record

    preserve_discard_record(str(pointer["run_id"]))
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    destination = str(backend_override).strip()
    selection = pointer.get("picker_selection")
    replay_pick = (
        isinstance(selection, Mapping)
        and selection.get("action") == "route"
        and not destination
        and not pointer.get("named_route")
        and not pointer.get("route_fallback")
    )
    # The launch continues the request it was admitted from, so it keeps that
    # request's run id: the id a dispatch printed is the one a coordinator
    # records and later acts on, and a fresh id here would leave the printed one
    # naming nothing the moment the lane frees.
    from reckon.crew import routing

    # The pass has already judged the gate over the units this request reads;
    # the launch publishes them so the same gate inside dispatch asks the same
    # question instead of the whole plan's. A direct dispatch publishes nothing
    # and keeps the whole-plan check.
    units_token = routing.ADMISSION_REVIEW_UNITS.set(plan_review_units(node.section))
    try:
        return dispatch(
            node=node,
            run_id=str(pointer["run_id"]),
            project=str(pointer["project"]),
            repo=pointer.get("repo"),
            config=config,
            session=str(pointer["session"]),
            wave=str(options.get("wave") or ""),
            base=str(options.get("base") or "HEAD"),
            locked_decisions=tuple(options.get("locked_decisions") or ()),
            peer_scopes=node.peer_scopes,
            member=str(options.get("member") or ""),
            launcher=launcher,
            execution_override=bool(options.get("allow_execution_mismatch")),
            orchestrator_lane_reason=options.get("orchestrator_lane_reason") or None,
            unreconciled_override=bool(options.get("allow_unreconciled_runs")),
            unreviewed_plan_override=bool(options.get("allow_unreviewed_plan")),
            watch_required=True,
            watch_override=bool(options.get("no_watch")),
            local=bool(pointer.get("local")) and not destination,
            backend_override=destination or str(pointer.get("backend") or ""),
            repairs=str(options.get("repairs") or ""),
            accept_directory_claim=bool(options.get("accept_directory_claim")),
            no_fence_reason=str(options.get("no_fence_reason") or ""),
            # A stored picker answer is supplied to dispatch, so the model is
            # resolved from that choice without asking the picker again. An
            # edited route or a recorded fallback stays on the backend it
            # already names.
            route="picker" if replay_pick else "deterministic",
            picker_selection=selection,
            comment=str(options.get("comment") or ""),
            dispatch_options=options,
            admission_launch=True,
            admitted_from=str(pointer["run_id"]),
        )
    finally:
        routing.ADMISSION_REVIEW_UNITS.reset(units_token)


# The dispatch options a rebind copies from the request it moves. The waivers a
# lane change carries, never adds: the plan-review and directory-claim overrides
# the original dispatch recorded, and the free-text comment beside them. A
# rebind copies exactly these, so a run moved to another lane is not refused by
# a gate the original request had already answered — and a request that carried
# no waiver arrives carrying none.
_CARRIED_OPTION_KEYS = ("allow_unreviewed_plan", "accept_directory_claim", "comment")


def carried_options(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """The recorded dispatch options a lane change copies onto the new launch."""
    options = dict(pointer.get("dispatch_options") or {})
    return {key: options[key] for key in _CARRIED_OPTION_KEYS if key in options}


def move_queued_run(
    run_id: str,
    backend_name: str,
    *,
    reason: str = "",
    config: Mapping[str, Any] | None = None,
    launch: bool = True,
    launcher: Callable[..., int] | None = None,
) -> dict[str, Any]:
    """Move a queued request that never launched onto another backend.

    A queued request has no prompt and no worktree — its first attempt has not
    started — so the move re-composes its launch from the recorded node through
    the admission pass's own path rather than composing a fresh prompt. That
    recomposition forwards the request's recorded dispatch options, so the
    waivers the original dispatch answered are carried into the new launch, and
    the request is released only inside it, so the move never races the
    admission pass with a discard of a request that is still waiting.
    """
    record = runs.read_pointer(run_id)
    if str(record.get("phase") or "") != "queued":
        raise CrewError(f"run {run_id!r} is not a queued request to move")
    destination = str(backend_name).strip()
    if not destination:
        raise CrewError("changing a run's backend requires a destination backend")
    config_for = None if config is None else (lambda _pointer: config)
    if not launch:
        return {
            "run_id": run_id,
            "node": record.get("node"),
            "backend": destination,
            "launch": destination,
            "reason": str(reason),
            "carried": carried_options(record),
        }
    moved = _await_moved_launch(
        record,
        snapshot=copy.deepcopy(dict(record)),
        destination=destination,
        launcher=launcher,
        config_for=config_for,
    )
    carried = carried_options(record)
    moved_run_id = str(moved["run_id"])

    def stamp(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["carried_options"] = carried
        pointer["lane_move"] = {
            "from_backend": str(record.get("backend") or ""),
            "to_backend": destination,
            "reason": str(reason),
        }
        return pointer

    return runs._mutate_pointer(moved_run_id, stamp)


def _await_moved_launch(
    record: Mapping[str, Any],
    *,
    snapshot: dict[str, Any],
    destination: str,
    launcher: Callable[..., int] | None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    """Launch a moved queued request, restoring it when the launch does not land.

    The launch releases the request as it launches, so a launch that is refused
    would otherwise take a request that is still waiting for a lane down with
    it. The snapshot is written back when the launch leaves no live pointer for
    it — neither the request's own id nor one naming it as the request it was
    admitted from. It is not written back when the request came back queued on
    the destination, so no second copy of one waiting request is created.
    """
    run_id = str(record["run_id"])
    try:
        return _require_launched_record(
            _launch_queued(
                record,
                launcher=launcher,
                config_for=config_for,
                backend_override=destination,
            )
        )
    except Exception:
        project = str(record.get("project") or "")
        survives = any(
            str(row.get("run_id") or "") == run_id
            or str(row.get("admitted_from") or "") == run_id
            for row in runs.list_live(project=project or None)
        )
        if not survives:
            with contextlib.suppress(Exception):
                runs.queue_dispatch(snapshot)
        raise


def _session_allowance_read(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> tuple[float | None, str]:
    """The extra-worker allowance the lane grants the queued run's session.

    Returns the allowance and the source it was read from, so a pass can record
    which published figure it held or launched on. ``None`` means no lane
    document is declared, so the lane publishes nothing to hold on and every
    queued run may be attempted. A published allowance of zero or less holds
    that session.
    """
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    backend = (config.get("backends") or {}).get(str(pointer.get("backend") or ""))
    if not backend:
        return None, "no backend declares the request's lane"
    decision = _dispatch_lane_allowance(
        backend,
        session=str(pointer["session"]),
        weights_configured=_fair_use_weights_configured(config),
    )
    if decision is None:
        return None, "the lane publishes no allowance"
    source = str(decision.get("source") or "unknown")
    if decision.get("held"):
        return 0, source
    allowance = decision.get("allowance")
    if isinstance(allowance, bool) or not isinstance(allowance, (int, float)):
        return None, source
    return float(allowance), source


def _lane_gate_hold(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """The reason the lane's gate would refuse this request, or ``None``.

    The shared dispatch path reads the lane's gate document before it creates a
    worktree, and a paused or unreadable gate holds the launch there. Asking the
    same question here, before the queued pointer is released, lets a request the
    gate holds stay exactly as it is: no release, no failed launch,
    no re-queue for a request whose state never moved. The companion question —
    the session's lane allowance — is asked by :func:`_session_allowance_read`,
    whose ``0`` holds the session; together they are the two the dispatch path asks.
    """
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    backend = (config.get("backends") or {}).get(str(pointer.get("backend") or ""))
    if not isinstance(backend, Mapping) or not backend:
        return None
    gate = _dispatch_lane_gate(backend)
    if gate.get("state") in _LANE_GATE_WAITING_STATES:
        return str(
            gate.get("detail") or gate.get("reason") or gate.get("state") or "held"
        )
    return None


def _record_metered_hold(run_id: str, hold: BudgetHold) -> None:
    """Keep a serving-lane hold on the waiting request without a failed attempt."""
    reason = str(hold.verdict.get("reason") or hold)

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["hold"] = dict(hold.verdict)
        pointer["reason"] = reason
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _local_fallback(
    pointer: Mapping[str, Any],
    hold: BudgetHold,
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """Rebind a picker route to local when its metered lane stops serving."""
    selection = pointer.get("picker_selection")
    if (
        pointer.get("named_route")
        or not isinstance(selection, Mapping)
        or selection.get("action") != "route"
    ):
        return None
    config = (
        config_for(pointer)
        if config_for is not None
        else _resolve_lane_config(pointer, select_local=False)
    )
    local_backend = str(config.get("local_backend") or "")
    original_backend = str(pointer.get("backend") or "")
    if not local_backend or original_backend == local_backend:
        return None
    fallback = {
        "from_backend": original_backend,
        "to_backend": local_backend,
        "reason": str(hold.verdict.get("reason") or hold),
    }
    rebound = copy.deepcopy(dict(pointer))
    rebound["backend"] = local_backend
    rebound["local"] = True
    rebound["route_fallback"] = fallback
    rebound["reason"] = fallback["reason"]
    options = dict(rebound.get("dispatch_options") or {})
    options["route_fallback"] = fallback
    rebound["dispatch_options"] = options
    runs._mutate_pointer(str(pointer["run_id"]), lambda _current: dict(rebound))
    return rebound


def _retry_delay(attempts: int) -> timedelta:
    """The bounded wait before a request that just failed may be retried."""
    seconds = ADMISSION_RETRY_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, ADMISSION_RETRY_BACKOFF_CAP_SECONDS))


def _resolved_request_claims(
    pointer: Mapping[str, Any],
) -> list[tuple[Path | None, Path]]:
    """A queued request's write paths, resolved as the dispatch scope check does.

    The resolution itself lives in ``dispatch_claims._pointer_scope_entries``, so
    the pre-check and the claim check cannot drift: each declared path becomes a
    ``(repository, absolute path)`` pair, and a pair in another repository never
    overlaps however its relative spelling reads. Comparing bare
    repository-relative paths instead would hold two requests in different
    repositories — both declaring, say, ``pyproject.toml`` — behind each other
    for the starvation age with no overlap at all.
    """
    from reckon.crew.dispatch_claims import _pointer_scope_entries

    return [
        (repository, absolute)
        for repository, _path, absolute, _declared, _derived_from in (
            _pointer_scope_entries(pointer)
        )
    ]


def _pass_claims(
    pointer: Mapping[str, Any],
    claims: dict[str, list[tuple[Path | None, Path]]],
) -> list[tuple[Path | None, Path]]:
    """A request's resolved claims for this pass, resolved at most once.

    The resolution reads the request's repository and mounts, so resolving
    inside every comparison would cost a pass about N-squared reads for N
    requests. The pass hands its own cache to every comparison, so each request
    is resolved once and a request whose write paths or mounts change is
    resolved afresh on the next pass, when the cache is new.
    """
    run_id = str(pointer.get("run_id") or "")
    resolved = claims.get(run_id)
    if resolved is None:
        resolved = _resolved_request_claims(pointer)
        claims[run_id] = resolved
    return resolved


def _queued_requests_overlap(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    claims: dict[str, list[tuple[Path | None, Path]]],
) -> bool:
    """Whether two queued requests name a write path in one repository.

    The comparison is the dispatch scope check's own: same repository, and paths
    that overlap by component containment. A pair in different repositories never
    overlaps, however their relative spellings read.
    """
    first_claims = _pass_claims(first, claims)
    if not first_claims:
        return False
    return any(
        repository == other_repository
        and runs._scopes_overlap(absolute.as_posix(), other_absolute.as_posix())
        for repository, absolute in first_claims
        for other_repository, other_absolute in _pass_claims(second, claims)
    )


def _earlier_overlapping_request(
    pointer: Mapping[str, Any],
    earlier: Sequence[Mapping[str, Any]],
    *,
    moment: datetime,
    claims: dict[str, list[tuple[Path | None, Path]]],
) -> Mapping[str, Any] | None:
    """An earlier-ranked queued request whose write paths overlap this one.

    The head of the queue has no earlier-ranked request, so it is never held
    this way. Once this request has waited longer than the starvation age the
    pre-check stops holding it and it may launch ahead under the ordinary claim
    rules, so a later request is never held without end behind an earlier one
    whose own session is never granted a slot. ``claims`` is the pass's resolved
    claims, so each comparison reads a request's paths rather than resolving
    them again.
    """
    queued_at = parse_utc(pointer.get("queued_at"))
    if queued_at is not None and (
        (moment - queued_at).total_seconds() > queue_order.STARVATION_AGE_SECONDS
    ):
        return None
    for candidate in earlier:
        if _queued_requests_overlap(pointer, candidate, claims=claims):
            return candidate
    return None


def _failure_text(failure: Any) -> str:
    """The reason text of a recorded failure, whether it is a string or a row."""
    if isinstance(failure, Mapping):
        return str(failure.get("kind") or failure.get("detail") or failure)
    return str(failure or "")


def _is_lane_hold(failure: Any) -> bool:
    """Whether a recorded failure is the lane holding the request, not a fault.

    A lane hold is retried as soon as the lane opens, so it is never backed off;
    the two words are the exceptions ``_launch_queued`` raises for exactly this:
    the lane's allowance is zero, or its gate is paused.
    """
    return _failure_text(failure).startswith(("LaneHeld", "LanePaused"))


def _admit_code_stamp() -> str:
    """The content identity of the checkout the admission service would run.

    The same identity the followers compute, so a commit that changes no file
    under ``reckon/`` moves nothing here and re-executes nothing.
    """
    from reckon.crew.follower_registration import follower_code_stamp

    return follower_code_stamp()


def _source_root() -> Path:
    """The sys.path root that supplied this imported package.

    The followers' reloader resolves its checkout the same way, so both name
    one root rather than each counting its own way up the tree. This module
    sits in a subpackage, where counting parents stops at ``reckon/`` -- the
    package directory, which is not a checkout and reads as clean and
    importable wherever it is asked.
    """
    from reckon.crew_follow_commands import _import_root

    return _import_root(sys.modules[__name__])


def _checkout_code_is_trusted() -> bool:
    """Whether the checkout's current code may be loaded and run.

    The same trusted-checkout test the followers' reloader uses: clean under
    ``reckon/`` against ``HEAD`` and importable. A tree mid-merge refuses, so a
    running service is never replaced by code a half-finished edit produced.
    """
    from reckon import checkout_trust

    return checkout_trust.moved_checkout_is_trustworthy(_source_root()).trusted


def _reexec_into_current_code() -> None:
    """Replace this process with a fresh image of the checkout's current code.

    ``os.execve`` reuses the process, so the supervisor that declared this
    service need not restart it: the same pid continues on the code now on
    disk. The launcher leads with the sys.path root that supplied this import,
    so the replacement loads the checkout rather than whatever argv[0] named.
    """
    root = str(_source_root())
    launcher = (
        f"import sys; sys.path.insert(0, {root!r}); from reckon.cli import main; main()"
    )
    os.execve(  # noqa: S606 - replacement preserves descriptors and stdout.
        sys.executable,
        [sys.executable, "-c", launcher, *sys.argv[1:]],
        dict(os.environ),
    )


def _pause(seconds: float) -> None:
    """Wait out the interval between two admission passes."""
    time.sleep(seconds)


def _file_stamp(path: str) -> str:
    """A change detector for one dependency: its stat, or that it is absent."""
    if not path:
        return "none"
    try:
        stat = Path(path).stat()
    except OSError:
        return "absent"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _dependency_guard(
    pointer: Mapping[str, Any],
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, str]:
    """The things a queued request depends on, fingerprinted at failure time.

    The request is retried without waiting out its back-off the moment any of
    these changes: its brief file, its plan document, the flight configuration
    that resolved its backend, or the code of the checkout it would launch
    from. Only their stamps are kept, so the guard is a comparison rather than a
    copy of the request. Fingerprinting the code stamp is what lets a request a
    pass refused under older code be retried by the first pass after the service
    re-executes onto the current code, rather than waiting out its back-off.

    The lane document the router publishes is deliberately not watched: it is
    republished every few seconds, so its stamp moves constantly and would
    retry a request that can never launch at the publication cadence. The
    flight config layers change only when an operator changes the routing, so
    they are the honest dependency.
    """
    node = dict(pointer.get("node") or {})
    repo = str(pointer.get("repo") or "")
    project = str(pointer.get("project") or "")
    plan = str(node.get("plan") or "")
    brief = str(node.get("brief_path") or "")
    plan_path = (
        str(Path(repo) / "docs" / "plans" / f"{plan}.html") if repo and plan else ""
    )
    host_config = ""
    project_config = ""
    try:
        from reckon import flight

        host_config = str(flight.host_config_path())
        if repo:
            project_config = str(flight.project_config_path(project, repo))
    except Exception:  # noqa: BLE001 - an unresolvable flight path is simply not a dependency the guard can watch
        host_config = host_config or ""
        project_config = project_config or ""
    return {
        "brief": _file_stamp(brief),
        "plan": _file_stamp(plan_path),
        "host-config": _file_stamp(host_config),
        "project-config": _file_stamp(project_config),
        "code": _admit_code_stamp(),
    }


def _report_queued_failure(pointer: Mapping[str, Any]) -> None:
    """Surface a request's first failure once through the session's pane stream.

    The session reads the project's watch stream, so the report is one appended
    transition for the queued run. It is written once: the pointer records
    ``failure_reported_at`` on the first stamp, and every later pass sees it and
    reports nothing. A stream that cannot be written is not fatal: the record on
    the pointer is the fact, and a session attaching later still reads it there.

    A request the operator discarded during the pass is not reported at all. The
    discard removes the pointer and records the departure, so the row would name
    a run the fleet no longer holds and report a failure nobody is waiting on.
    """
    project = str(pointer.get("project") or "")
    run_id = str(pointer.get("run_id") or "")
    if not project or not run_id:
        return
    if _discarded_since(pointer):
        return
    try:
        from reckon.crew.recovery import _fleet_counts
        from reckon.crew.recovery_watch import DEFAULT_WATCH_STALL_WINDOW

        live = runs._list_live_records(project=project)
        if all(str(record.get("run_id") or "") != run_id for record in live):
            live = [*live, pointer]
        snapshots = runs._watch_stream_snapshots(
            live, stall_window=DEFAULT_WATCH_STALL_WINDOW
        )
        snapshot = snapshots.get(run_id)
        if snapshot is None:
            return
        row = runs._stream_transition(
            project,
            snapshot=snapshot,
            previous=None,
            current=str(snapshot["state"]),
            counts=_fleet_counts(snapshots),
        )
        runs._append_watch_lines(runs.watch_stream_path(project), [row])
    except Exception:  # noqa: BLE001 - a report that cannot be written never stops admission
        return


def _retry_backoff_hold(
    pointer: Mapping[str, Any],
    *,
    moment: datetime,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """Why a failed request is left where it is, or ``None`` to attempt it.

    A request whose last attempt failed for a reason other than a lane hold is
    not attempted on every pass: the pass leaves its pointer and its recorded
    failure untouched until something it depends on changes or its bounded
    back-off elapses. A lane hold has no recorded back-off and is retried at
    once. This is what keeps a request that cannot launch — a missing brief, for
    one — from cycling through a release, a failed dispatch and a restore on
    every pass.
    """
    failure = pointer.get("failure")
    if not failure:
        return None
    if _is_lane_hold(failure):
        return None
    guard = pointer.get("failure_guard")
    if guard is not None and guard != _dependency_guard(pointer, config_for):
        return None
    retry_at = pointer.get("retry_at")
    if not retry_at:
        return None
    when = parse_utc(retry_at)
    if when is None:
        return None
    if moment < when:
        return (
            f"last attempt failed: {_failure_text(failure)}; "
            f"not retried before {retry_at}"
        )
    return None


def _stamp_failure(
    record: dict[str, Any],
    failure: Any,
    *,
    moment: datetime,
    guard: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Record one attempt's failure and when the request may be retried."""
    attempts = int(record.get("failure_attempts") or 0) + 1
    record["failure"] = failure if isinstance(failure, str) else dict(failure)
    record["failure_at"] = moment.isoformat()
    # The first stamp of a failure is the one the session is told about, so the
    # report marker is set once and never moved by a later attempt.
    record.setdefault("failure_reported_at", moment.isoformat())
    record["failure_attempts"] = attempts
    record["retry_at"] = (moment + _retry_delay(attempts)).isoformat()
    if guard is not None:
        record["failure_guard"] = dict(guard)
    return record


def _requeue_admitted_failures(live: list[dict[str, Any]]) -> list[str]:
    """Re-queue runs this drainer launched that the reaper marked launch-failed.

    The admission pass stamps every run it launches with the request it
    launched from. A run the reaper later marks ``launch-failed`` is a request
    still waiting behind a launch that never reached a model, so the next pass
    writes it back as one fresh queued pointer carrying the failure.
    """
    requeued = []
    for pointer in live:
        if str(pointer.get("phase") or "") != LAUNCH_FAILED_PHASE:
            continue
        request = pointer.get("admit_request")
        if not isinstance(request, Mapping):
            continue
        failures = list(pointer.get("launch_failures") or ())
        failure = dict(failures[-1]) if failures else {}
        restored = copy.deepcopy(dict(request))
        restored["run_id"] = str(pointer["run_id"])
        restored["phase"] = "queued"
        restored["queued_at"] = _utc_now()
        restored.setdefault("created_at", restored["queued_at"])
        # Stamp the re-queue with the same back-off a failed attempt records, so
        # a request that keeps failing on launch is attempted once per back-off
        # rather than released, failed and restored on every pass.
        _stamp_failure(
            restored,
            failure or {"kind": LAUNCH_FAILED_PHASE},
            moment=_now(),
            guard=_dependency_guard(restored, None),
        )
        _write_pointer(str(pointer["run_id"]), restored)
        requeued.append(str(pointer["run_id"]))
    return requeued


def _write_pointer(run_id: str, record: dict[str, Any]) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _discarded_since(snapshot: Mapping[str, Any]) -> bool:
    """Whether a deliberate discard has been recorded for this request.

    The pass holds the queued pointer in memory while it attempts a launch and
    writes it back when the launch does not survive. A ``crew discard`` issued in
    that window removes the pointer and records the departure — in the run
    directory, or in the shared discards spool for a request that never launched
    and so has no run directory. Without this reading the restore would write the
    request back and undo the discard. The record's presence is the fact the
    restore reads.
    """
    from reckon.crew.promotion import discard_recorded

    return discard_recorded(str(snapshot["run_id"]))


def _restore_queued(
    snapshot: Mapping[str, Any],
    failure: str,
    *,
    moment: datetime | None = None,
    guard: Mapping[str, Any] | None = None,
) -> None:
    """Write a launched request back as one queued pointer carrying the failure.

    The restored pointer carries the same bounded back-off a failure annotated
    in place would: a request whose release already withdrew it is retried once
    per back-off, not on every pass. A request discarded during the pass is not
    written back at all: the discard stood, so the restore reads its record and
    leaves the absence the operator asked for.
    """
    if _discarded_since(snapshot):
        return
    restored = copy.deepcopy(dict(snapshot))
    restored["phase"] = "queued"
    _stamp_failure(restored, failure, moment=moment or _now(), guard=guard)
    for key in (
        "pid",
        "pid_start_time",
        "worktree",
        "log_path",
        "stderr_path",
        "prompt_path",
        "admitted_at",
    ):
        restored.pop(key, None)
    _write_pointer(str(snapshot["run_id"]), restored)


def _record_queued_failure(
    snapshot: Mapping[str, Any],
    failure: str,
    *,
    moment: datetime | None = None,
    guard: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Return the retained request's id and any unrecoverable write error."""
    original_run_id = str(snapshot["run_id"])
    failed_at = moment or _now()

    def annotate(current: dict[str, Any]) -> dict[str, Any]:
        return _stamp_failure(current, failure, moment=failed_at, guard=guard)

    try:
        runs._mutate_pointer(original_run_id, annotate)
    except Exception as annotate_error:  # noqa: BLE001 - a failed write needs restoration
        try:
            _restore_queued(snapshot, failure, moment=failed_at, guard=guard)
        except Exception as restore_error:  # noqa: BLE001 - report storage failure to the pass
            annotation = f"annotation {type(annotate_error).__name__}: {annotate_error}"
            restoration = f"restore {type(restore_error).__name__}: {restore_error}"
            return original_run_id, f"{annotation}; {restoration}"
        return original_run_id, None
    return original_run_id, None


def _require_launched_record(candidate: Any) -> Mapping[str, Any]:
    if (
        not isinstance(candidate, Mapping)
        or not candidate.get("run_id")
        or not candidate.get("phase")
        or candidate.get("phase") == "queued"
    ):
        raise CrewError("dispatch did not return a launched record")
    return candidate


def _verdict_row(
    pointer: Mapping[str, Any],
    *,
    allowance: float | None,
    allowance_source: str,
    verdict: str,
    detail: str = "",
    retry_at: str = "",
    failure: str = "",
) -> dict[str, Any]:
    """One queued pointer's verdict for a pass, as the log records it."""
    row: dict[str, Any] = {
        "run_id": str(pointer.get("run_id") or ""),
        "session": str(pointer.get("session") or ""),
        "allowance": allowance,
        "allowance_source": allowance_source,
        "verdict": verdict,
    }
    if detail:
        row["detail"] = detail
    if retry_at:
        row["retry_at"] = retry_at
    if failure:
        row["failure"] = failure
    return row


def _record_gated_verdicts(
    result: dict[str, Any],
    *,
    gate: Mapping[str, Any],
    lane: str,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> None:
    """Name every queued request a waiting fleet gate held, and the gate itself.

    The gate is read before the queue is, so a pass that returns here would
    otherwise record nothing for requests its own gate is holding — the same
    empty trace as a request held for its session's allowance. The requests are
    read only to name them: nothing is released, launched or rewritten.
    """
    state = str(gate.get("state") or "")
    for pointer in runs.list_live():
        if pointer.get("phase") != "queued":
            continue
        if lane and str(pointer.get("backend")) != lane:
            continue
        allowance, source = _session_allowance_read(pointer, config_for=config_for)
        result["verdicts"].append(
            _verdict_row(
                pointer,
                allowance=allowance,
                allowance_source=source,
                verdict="held-by-gate",
                detail=state,
            )
        )


def _pass_verdict_record(
    result: Mapping[str, Any], *, lane: str, code_stamp: str
) -> dict[str, Any] | None:
    """The one record a pass leaves, or ``None`` when it examined no pointer.

    The same object is what the verdict log holds and what the service loop
    prints, so a coordinator reading the log and a caller reading stdout see
    one account of the pass rather than two that can drift.
    """
    verdicts = result.get("verdicts") or []
    landing = result.get("landing") or {}
    if not verdicts and not landing.get("landed") and not landing.get("obligations"):
        return None
    return {
        "at": _utc_now(),
        "lane": lane,
        "code_stamp": code_stamp,
        "gate": result.get("gate"),
        "pointers": list(verdicts),
        "landing": landing,
    }


def _publish_pass_verdicts(record: Mapping[str, Any] | None) -> None:
    """Write one pass's verdicts to stdout as one JSON line.

    The bare ``crew admit`` verb is the fleet's admit service: a coordinator
    watching it must see each decision as the pass reaches it rather than only
    afterwards out of the verdict log. A pass that examined no queued pointer
    decided nothing, so it prints nothing — the same silence it records.
    """
    if record is None:
        return
    print(json.dumps(record), flush=True)


def _record_pass_verdicts(
    result: Mapping[str, Any], *, lane: str, code_stamp: str
) -> dict[str, Any] | None:
    """Write one log line for a pass whose lane had a queued pointer.

    A pass that examined no queued pointer writes nothing: an idle pass is the
    absence of a line, not a line saying so. The line goes through the shared
    crew-home log writer, so this log and the picker's timings are written the
    same way, and the bound that follows is the shared service-log trim.

    The trim keeps the file's inode, so the appender's own descriptor stays
    valid, but it reads the tail and then rewrites the file in place: a line
    another lane's service appends in that window is still lost. The window is
    narrow and accepted, because this log is a best-effort record rather than a
    decision the service rests on. For the same reason a trim that raises is
    swallowed: a storage error while bounding the record must not end a service
    whose admission decision has already been made.
    """
    verdicts = result.get("verdicts") or []
    landing = result.get("landing") or {}
    if not verdicts and not landing.get("landed") and not landing.get("obligations"):
        return None
    from reckon.crew.fleet_supervisor import trim_service_log
    from reckon.crew.picker import _append_crew_log_line

    record = _pass_verdict_record(result, lane=lane, code_stamp=code_stamp)
    _append_crew_log_line(ADMISSION_VERDICT_LOG, record)
    try:
        trim_service_log(
            runs.crew_home() / ADMISSION_VERDICT_LOG,
            limit=ADMISSION_VERDICT_LOG_MAX_BYTES,
            keep=ADMISSION_VERDICT_LOG_MAX_BYTES,
        )
    except OSError:  # a pass never fails on its own record
        return record
    return record


# The three answers a wait ref resolves to. ``satisfied`` clears the ref;
# ``abandoned`` blocks every request waiting on it, because nothing will ever
# deliver it; ``pending`` leaves the request where it is.
SATISFIED = "satisfied"
ABANDONED = "abandoned"
PENDING = "pending"

#: The two refs a section may name beside another section: a gate, and another
#: queued request by its run id. The prefixes are what tell the three apart, so
#: a gate id spelled like a section id is still read as a gate.
GATE_REF_PREFIX = "gate:"
REQUEST_REF_PREFIX = "run:"

#: The statuses a run's own manifest records when it delivered, and when it
#: ended without delivering. They are the run vocabulary and not the plan's: a
#: plan reads ``shipped`` or ``done``, while a manifest reads ``complete``.
#:
#: ``blocked`` is deliberately not among the failed ones. A blocked manifest is
#: a worker that stopped and asked for help, and the run behind it can still be
#: resumed or repaired to deliver, so the request waiting on it keeps waiting
#: rather than being closed as upstream-abandoned. Only ``failed`` — and a
#: discarded request — means nothing will ever deliver.
RUN_DELIVERED_STATUSES = ("complete",)
RUN_FAILED_STATUSES = ("failed", "abandoned")


def section_wait_refs(plan_html: str, section: str) -> list[str]:
    """The refs one section declares it waits on, read from the plan itself.

    The mapping is the plan's own ``plan-section-depends-on`` declaration, so a
    request queued for a waited section carries the refs the plan already
    states rather than a second copy a coordinator would have to keep in step.
    A plan that declares no mapping, and a section the mapping does not name,
    both yield no refs, and the request is queueable at once.
    """
    from reckon._schema import section_depends_on

    requested_section = normalize_section(section)
    refs = next(
        (
            value
            for key, value in (section_depends_on(plan_html) or {}).items()
            if normalize_section(key) == requested_section
        ),
        None,
    )
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(ref).strip() for ref in refs if str(ref).strip()]


def section_waits_for(pointer: Mapping[str, Any]) -> list[str]:
    """The refs a queued request waits on, as its own record states them."""
    refs = pointer.get("waits")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(ref).strip() for ref in refs if str(ref).strip()]


def _docs_for_project(
    project: str, *, current_project: str, current_docs: Path
) -> Path | None:
    if project == current_project:
        return current_docs
    try:
        from reckon._store import _mounts_path

        mounts = json.loads(_mounts_path().read_text(encoding="utf-8"))
        mounted = mounts.get(project)
        return Path(mounted).expanduser().resolve() if mounted else None
    except (OSError, ValueError, TypeError):
        return None


def _section_status(row: Mapping[str, Any], section: str) -> str:
    declarations = row.get("section_declarations") or {}
    return next(
        (
            str(status).lower()
            for key, status in declarations.items()
            if normalize_section(key) == normalize_section(section)
        ),
        "",
    )


def derive_wait_refs(node: TaskNode, *, repo_root: str | Path) -> list[str]:
    """Read section, plan and explicit request waits from the authored node."""
    from reckon import _plan_html as plan_html_reader

    refs = section_wait_refs(_plan_html(repo_root, node.plan), node.section)
    if node.plan:
        state = plan_html_reader.read_state(_plan_html(repo_root, node.plan))
        refs.extend(str(ref) for ref in state.get("depends_on") or ())
    refs.extend(f"{REQUEST_REF_PREFIX}{run_id}" for run_id in node.after)
    return list(dict.fromkeys(refs))


def _validated_batch_waits(
    nodes: Sequence[TaskNode],
    run_ids: Mapping[str, str],
    *,
    project: str,
    repo_root: str | Path,
    live: Sequence[Mapping[str, Any]],
    read_own_verdict: bool = True,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """Resolve all waits and reject absent targets or a combined graph cycle."""
    from reckon import _plan_html as plan_html_reader
    from reckon import ledger
    from reckon._schema import parse_plan_ref

    current_docs = Path(repo_root) / "docs"
    cache: dict[str, Any] = {}
    request_nodes = {str(run_ids[node.id]): node for node in nodes}
    live_requests = {
        str(pointer.get("run_id") or ""): pointer
        for pointer in live
        if pointer.get("run_id")
    }
    refs_by_run: dict[str, list[str]] = {}
    projects_by_run: dict[str, dict[str, str]] = {}
    edges: dict[str, set[str]] = {}

    def section_key(target_project: str, slug: str, section: str) -> str:
        identity = normalize_section(section)
        if identity.startswith("§"):
            identity = f"s{identity[1:]}"
        return f"{target_project}:{slug}#{identity}"

    def plan_key(target_project: str, slug: str) -> str:
        return f"{target_project}:{slug}"

    def target(ref: str, owner_project: str) -> tuple[str, str]:
        if ref.startswith(REQUEST_REF_PREFIX):
            run_id = ref[len(REQUEST_REF_PREFIX) :]
            if run_id not in request_nodes and run_id not in live_requests:
                known = any(
                    ledger.run_path(candidate, run_id).is_file()
                    for candidate in {owner_project, project}
                    if candidate
                )
                if not known:
                    raise CrewError(f"wait names missing request {run_id}")
            upstream_project = str(
                (live_requests.get(run_id) or {}).get("project") or owner_project
            )
            return f"run:{run_id}", upstream_project
        if ref.startswith((GATE_REF_PREFIX, "readiness:")):
            return ref, owner_project
        parsed = parse_plan_ref(ref)
        if parsed is None:
            raise CrewError(f"wait names malformed plan ref {ref!r}")
        target_project = parsed.project or owner_project
        docs = _docs_for_project(
            target_project, current_project=project, current_docs=current_docs
        )
        if docs is None:
            raise CrewError(f"wait names missing project {target_project!r}: {ref}")
        html = _plan_html(docs.parent, parsed.slug)
        if not html:
            raise CrewError(f"wait names missing plan {target_project}:{parsed.slug}")
        row = plan_html_reader.read_state(html)
        if parsed.stage:
            authored = {
                normalize_section(identity)
                for identity, _heading in plan_html_reader.authored_section_headings(
                    html
                )
            }
            if (
                not _section_status(row, parsed.stage)
                and normalize_section(parsed.stage) not in authored
            ):
                raise CrewError(f"wait names missing section {ref}")
            return section_key(
                target_project, parsed.slug, parsed.stage
            ), target_project
        return plan_key(target_project, parsed.slug), target_project

    def section_edges(key: str) -> None:
        if key in edges:
            return
        edges[key] = set()
        parsed = parse_plan_ref(key)
        if parsed is None or not parsed.stage:
            return
        docs = _docs_for_project(
            parsed.project or project,
            current_project=project,
            current_docs=current_docs,
        )
        if docs is None:
            return
        html = _plan_html(docs.parent, parsed.slug)
        for ref in section_wait_refs(html, parsed.stage):
            child, _ = target(ref, parsed.project or project)
            edges[key].add(child)
            if not child.startswith("run:"):
                section_edges(child)
        state = plan_html_reader.read_state(html)
        for ref in state.get("depends_on") or ():
            child, _ = target(str(ref), parsed.project or project)
            edges[key].add(child)

    for run_id, node in request_nodes.items():
        refs = derive_wait_refs(node, repo_root=repo_root)
        wait_projects: dict[str, str] = {}
        edge_set: set[str] = set()
        for ref in refs:
            child, upstream_project = target(ref, project)
            edge_set.add(child)
            if ref.startswith(REQUEST_REF_PREFIX):
                wait_projects[ref[len(REQUEST_REF_PREFIX) :]] = upstream_project
            elif not ref.startswith((GATE_REF_PREFIX, "readiness:")):
                section_edges(child)
        if read_own_verdict and node.plan and node.section:
            rows, _ = _readiness_rows(project, current_docs, cache)
            verdicts = (rows.get(node.plan) or {}).get("sections", {})
            own = next(
                (
                    verdict
                    for section, verdict in verdicts.items()
                    if normalize_section(section) == normalize_section(node.section)
                ),
                None,
            )
            holding_kinds = {
                blocker.get("kind") for blocker in (own or {}).get("blockers") or ()
            }
            explicit_gates = {
                parsed.stage
                for ref in refs
                if ref.startswith(GATE_REF_PREFIX)
                if (parsed := parse_plan_ref(ref[len(GATE_REF_PREFIX) :])) is not None
                and parsed.stage
            }
            held_gates = {
                str(blocker.get("id") or blocker.get("gate") or "")
                for blocker in (own or {}).get("blockers") or ()
                if blocker.get("kind") == "gate"
            }
            if holding_kinds & {"gate", "decision"} and not (
                held_gates
                and held_gates <= explicit_gates
                and "decision" not in holding_kinds
            ):
                refs.append(
                    f"readiness:{section_key(project, node.plan, node.section)}"
                )
        refs_by_run[run_id] = list(dict.fromkeys(refs))
        projects_by_run[run_id] = wait_projects
        edges[f"run:{run_id}"] = edge_set

    for run_id, pointer in live_requests.items():
        if run_id in request_nodes:
            continue
        if str(pointer.get("phase") or "") in {
            "failed",
            "complete",
            "blocked-upstream",
            "discarded",
        }:
            continue
        owner = str(pointer.get("project") or project)
        edges[f"run:{run_id}"] = set()
        for ref in section_waits_for(pointer):
            try:
                child, _ = target(ref, owner)
            except CrewError:
                # Existing requests only supply cycle edges. An unresolved
                # edge cannot complete a cycle through this dispatch's batch.
                edges[f"run:{run_id}"].add(ref)
                continue
            edges[f"run:{run_id}"].add(child)
            if not child.startswith("run:"):
                section_edges(child)

    for run_id, node in request_nodes.items():
        if node.plan and node.section:
            key = section_key(project, node.plan, node.section)
            section_edges(key)
            edges[key].add(f"run:{run_id}")
    for run_id, pointer in live_requests.items():
        node = dict(pointer.get("node") or {})
        if node.get("plan") and node.get("section") and f"run:{run_id}" in edges:
            key = section_key(
                str(pointer.get("project") or project),
                str(node["plan"]),
                str(node["section"]),
            )
            section_edges(key)
            edges[key].add(f"run:{run_id}")

    visited: set[str] = set()
    active: list[str] = []

    def visit(key: str) -> list[str] | None:
        if key in active:
            return [*active[active.index(key) :], key]
        if key in visited:
            return None
        active.append(key)
        for child in sorted(edges.get(key, ())):
            cycle = visit(child)
            if cycle:
                return cycle
        active.pop()
        visited.add(key)
        return None

    for run_id in request_nodes:
        cycle = visit(f"run:{run_id}")
        if cycle:
            raise CrewError("wait cycle: " + " -> ".join(cycle))
    return refs_by_run, projects_by_run


def _readiness_rows(
    project: str, docs_dir: str | Path | None, cache: dict[str, Any]
) -> tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]:
    """Read one project's roadmap section verdicts and inventory per pass."""
    key = project or str(docs_dir or "")
    cached = cache.get(key)
    if cached is not None:
        return cached
    rows: dict[str, Mapping[str, Any] | Any] = {}
    inventory: dict[str, Mapping[str, Any] | Any] = {}
    try:
        from reckon import _plan_html as plan_html_reader
        from reckon.crew.plan_review import project_section_readiness
        from reckon.mcp_discovery import _discover_project, _inventory_row

        if not project:
            cache[key] = ({}, {})
            return cache[key]
        discovered = _discover_project(project, None)
        items = [_inventory_row(item) for item in discovered.get("inventory", [])]
        for item in items:
            if item.get("type") != "plan" or not item.get("slug"):
                continue
            html = _plan_html(Path(docs_dir).parent, str(item["slug"]))
            item["section_declarations"] = plan_html_reader.read_state(html).get(
                "section_declarations", {}
            )
        inventory = {str(item.get("slug") or ""): item for item in items}
        rows = project_section_readiness(project, Path(docs_dir))
    except Exception:  # noqa: BLE001 - an unbuildable roadmap holds every wait
        rows, inventory = {}, {}
    cache[key] = (rows, inventory)
    return rows, inventory


def _project_of_docs_dir(docs_dir: str | Path | None) -> str:
    """The project a checkout's docs directory belongs to, from mounts.json."""
    if not docs_dir:
        return ""
    try:
        from reckon._store import _mounts_path

        registered = json.loads(_mounts_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unregistered docs dir names no project
        return ""
    target = Path(str(docs_dir)).expanduser().resolve()
    for project, directory in (registered or {}).items():
        with contextlib.suppress(OSError, RuntimeError):
            if Path(str(directory)).expanduser().resolve() == target:
                return str(project)
    return ""


def _section_ref(ref: str) -> tuple[str, str] | None:
    """The ``(slug, section)`` a section ref names, or ``None``.

    The spelling is the repository's own. :func:`reckon._schema.parse_plan_ref`
    already answers ``[project:]slug[#stage]`` for the roadmap, the document
    check and the ledger, and it is written the way it is because two spellings
    of one ref have to compare equal; a hand-rolled split answers differently
    for the forms it does not anticipate, and then two readers of the same plan
    disagree about which section a wait names.

    A section ref carries a stage and nothing else — ``plan#s1`` — so a ref
    without one names no section of any plan and is not a wait.
    """
    from reckon._schema import parse_plan_ref

    parsed = parse_plan_ref(ref)
    if parsed is None or not parsed.stage:
        return None
    return parsed.slug, normalize_section(parsed.stage)


def wait_ref_kind(ref: str) -> str:
    """Which of the three kinds of wait one ref names.

    A section waits on a section, a gate, or another queued request, and the
    three are spelled apart rather than guessed from a ref's shape: a gate id
    and a section id are both bare words, so a reader guessing between them
    reads a wait as its own opposite.
    """
    text = str(ref).strip()
    if text.startswith(GATE_REF_PREFIX):
        return "gate"
    if text.startswith(REQUEST_REF_PREFIX):
        return "request"
    return "section"


def _gate_state(
    ref: str,
    *,
    rows: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]],
) -> str:
    """Resolve a wait on a gate through the roadmap's own gate edges.

    A gate that holds execution reaches the roadmap as a blocker row carrying
    the gate's id in the section readiness split, so its verdict is read there
    rather than re-derived from the plan's gate records. The roadmap is the
    reader that already knows which gate holds which section; a second reading
    is how a request comes to be held on a gate the roadmap would call passed.

    A gate the roadmap holds no blocker for has passed or gates nothing this
    request waits on, so the wait is satisfied.
    """
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

    parsed = parse_plan_ref(ref[len(GATE_REF_PREFIX) :].strip())
    if parsed is None or not parsed.stage:
        return PENDING
    slug, gate_id = parsed.slug, parsed.stage
    row = rows.get(slug)
    if row is None:
        status = str(
            (inventory.get(slug) or {}).get("workflow_status")
            or (inventory.get(slug) or {}).get("status")
            or ""
        )
        if status in COMPLETED_STATUSES:
            return SATISFIED
        if status in TERMINAL_STATUSES:
            return ABANDONED
        return PENDING
    for held in (row.get("sections") or {}).values():
        for blocker in held.get("blockers") or ():
            if str(blocker.get("gate") or "") != gate_id:
                continue
            status = str(blocker.get("status") or "")
            if status in TERMINAL_STATUSES and status not in COMPLETED_STATUSES:
                return ABANDONED
            return PENDING
    return SATISFIED


def _request_state(
    ref: str,
    *,
    live: Sequence[Mapping[str, Any]],
    project: str = "",
    issues: list[str] | None = None,
) -> str:
    """A request lands only when its complete run's revision reaches HEAD."""
    run_id = str(ref[len(REQUEST_REF_PREFIX) :]).strip()
    if not run_id:
        return PENDING
    for pointer in live:
        if str(pointer.get("run_id") or "") != run_id:
            continue
        phase = str(pointer.get("phase") or "")
        if phase in (BLOCKED_UPSTREAM_PHASE, *DEAD_RUN_PHASES) or _discarded(run_id):
            return ABANDONED
    if _discarded(run_id):
        return ABANDONED
    try:
        manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return PENDING
    # The manifest's status is read by the reader that owns that format, which
    # reads a JSON body as a JSON document. A status read here as a ``status:``
    # line alone answers empty for a manifest written as JSON -- a run that
    # delivered reads as never having delivered, and the request waiting on it
    # is never released. A body no reader can parse is not evidence of delivery,
    # so it stays pending.
    from reckon.crew.reports import ManifestParseError, parse_manifest

    try:
        status = str(parse_manifest(text, path=str(manifest)).get("status") or "")
    except ManifestParseError as exc:
        if issues is not None:
            issues.append(f"{run_id}: refused manifest status or shape: {exc}")
        return PENDING
    if status in RUN_FAILED_STATUSES:
        return ABANDONED
    if status not in RUN_DELIVERED_STATUSES or not project:
        return PENDING
    try:
        from reckon import ledger
        from reckon.crew.dispatch_sections import project_mount_repository
        from reckon.crew.promotion_scope import _revision_is_ancestor

        row = json.loads(ledger.run_path(project, run_id).read_text(encoding="utf-8"))
        revision = str(row.get("promoted_revision") or "").strip()
        if not revision:
            if issues is not None:
                issues.append(f"{run_id}: promoted_revision absent")
            return PENDING
        checkout = project_mount_repository(project)
        if checkout is None:
            if issues is not None:
                issues.append(f"{run_id}: project {project} has no registered checkout")
            return PENDING
        return (
            SATISFIED if _revision_is_ancestor(checkout, revision, "HEAD") else PENDING
        )
    except (OSError, ValueError, KeyError):
        return PENDING


def _wait_state(
    ref: str,
    *,
    waiting_section: str,
    rows: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]],
    dead_sections: set[tuple[str, str]],
    live: Sequence[Mapping[str, Any]],
    project: str = "",
    docs_dir: Path | None = None,
    cache: dict[str, Any] | None = None,
    wait_projects: Mapping[str, str] | None = None,
    issues: list[str] | None = None,
) -> str:
    """Resolve one wait ref of any of the three kinds."""
    kind = wait_ref_kind(ref)
    if ref.startswith("readiness:"):
        target = ref[len("readiness:") :]
        return _section_readiness_state(
            target,
            project=project,
            docs_dir=docs_dir,
            cache=cache if cache is not None else {},
        )
    if kind == "gate":
        return _gate_state(ref, rows=rows, inventory=inventory)
    if kind == "request":
        run_id = ref[len(REQUEST_REF_PREFIX) :]
        return _request_state(
            ref,
            live=live,
            project=str((wait_projects or {}).get(run_id) or ""),
            issues=issues,
        )
    return _ref_state(
        ref,
        waiting_section=waiting_section,
        rows=rows,
        inventory=inventory,
        dead_sections=dead_sections,
        project=project,
        docs_dir=docs_dir,
        cache=cache,
    )


def _section_readiness_state(
    ref: str, *, project: str, docs_dir: Path | None, cache: dict[str, Any]
) -> str:
    from reckon._schema import parse_plan_ref

    parsed = parse_plan_ref(ref)
    if parsed is None or not parsed.stage or docs_dir is None:
        return PENDING
    target_project = parsed.project or project
    target_docs = _docs_for_project(
        target_project, current_project=project, current_docs=docs_dir
    )
    if target_docs is None:
        return PENDING
    rows, inventory = _readiness_rows(target_project, target_docs, cache)
    item = inventory.get(parsed.slug)
    if item is None:
        return PENDING
    status = _section_status(item, parsed.stage)
    if status == "done":
        return SATISFIED
    if status in {"descoped", "deferred", "failed", "blocked"}:
        return ABANDONED
    verdict = next(
        (
            value
            for section, value in (rows.get(parsed.slug) or {})
            .get("sections", {})
            .items()
            if normalize_section(section) == normalize_section(parsed.stage)
        ),
        None,
    )
    return SATISFIED if verdict and verdict.get("ready") else PENDING


def _dispatch_nonrun_wait_state(ref: str, *, project: str, docs_dir: Path) -> str:
    """Read authored declarations without a portfolio scope-claim scan."""
    from reckon import _plan_html as plan_html_reader
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

    gate_ref = ref.startswith(GATE_REF_PREFIX)
    parsed = parse_plan_ref(ref[len(GATE_REF_PREFIX) :] if gate_ref else ref)
    if parsed is None:
        return PENDING
    target_project = parsed.project or project
    target_docs = _docs_for_project(
        target_project, current_project=project, current_docs=docs_dir
    )
    if target_docs is None:
        return PENDING
    html = _plan_html(target_docs.parent, parsed.slug)
    if not html:
        return PENDING
    state = plan_html_reader.read_state(html)
    if gate_ref:
        gate = next(
            (
                item
                for item in state.get("gates") or ()
                if str(item.get("id") or "") == parsed.stage
            ),
            None,
        )
        if gate is None:
            return PENDING
        if gate.get("passed"):
            return SATISFIED
        source = parse_plan_ref(str(gate.get("gating_plan") or ""))
        if source is not None and source.stage:
            source_docs = _docs_for_project(
                source.project or target_project,
                current_project=project,
                current_docs=docs_dir,
            )
            if source_docs is not None:
                source_state = plan_html_reader.read_state(
                    _plan_html(source_docs.parent, source.slug)
                )
                if _section_status(source_state, source.stage) == "done":
                    return SATISFIED
        return PENDING
    if parsed.stage:
        declaration = _section_status(state, parsed.stage)
        if declaration == "done":
            return SATISFIED
        if declaration in {"descoped", "deferred", "failed", "blocked"}:
            return ABANDONED
        return PENDING
    status = str(state.get("status") or "")
    if status in COMPLETED_STATUSES:
        return SATISFIED
    return ABANDONED if status in TERMINAL_STATUSES else PENDING


def _needs_own_section_verdict(node: TaskNode, *, repo_root: str | Path) -> bool:
    """Ask the roadmap about the node's section when authored holds can apply."""
    if not node.plan or not node.section:
        return False
    from reckon import _plan_html as plan_html_reader

    state = plan_html_reader.read_state(_plan_html(repo_root, node.plan))
    return bool(state.get("gates") or state.get("decisions"))


def dispatch_waits(
    node: TaskNode,
    *,
    repo_root: str | Path,
    project: str = "",
    run_id: str = "",
    live: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Validate a dispatch's graph and return all refs and its unmet refs.

    The caller uses the same run identity for validation and storage. No queued
    pointer exists when this raises for a missing target or a graph cycle.
    """
    own_verdict = _needs_own_section_verdict(node, repo_root=repo_root)
    if not derive_wait_refs(node, repo_root=repo_root) and not own_verdict:
        return [], {}, []
    docs_dir = Path(str(repo_root)) / "docs"
    owner_project = project or _project_of_docs_dir(docs_dir)
    if live is None:
        try:
            live = runs.list_live()
        except Exception:  # noqa: BLE001 - an unreadable live set holds no wait
            live = []
    candidate = run_id or f"candidate:{node.id}"
    refs_by_run, projects_by_run = _validated_batch_waits(
        [node],
        {node.id: candidate},
        project=owner_project,
        repo_root=repo_root,
        live=live,
        read_own_verdict=own_verdict,
    )
    refs = refs_by_run[candidate]
    wait_projects = projects_by_run[candidate]
    if not refs:
        return [], wait_projects, []
    if not any(wait_ref_kind(ref) == "request" for ref in refs):
        unmet = [
            ref
            for ref in refs
            if ref.startswith("readiness:")
            or _dispatch_nonrun_wait_state(
                ref, project=owner_project, docs_dir=docs_dir
            )
            != SATISFIED
        ]
        return refs, wait_projects, unmet
    cache: dict[str, Any] = {}
    rows, inventory = _readiness_rows(owner_project, docs_dir, cache)
    dead = _dead_sections(live)
    unmet = [
        ref
        for ref in refs
        if _wait_state(
            ref,
            waiting_section=node.section,
            rows=rows,
            inventory=inventory,
            dead_sections=dead,
            live=live,
            project=owner_project,
            docs_dir=docs_dir,
            cache=cache,
            wait_projects=wait_projects,
        )
        != SATISFIED
    ]
    return refs, wait_projects, unmet


def unmet_waits(
    node: TaskNode,
    *,
    repo_root: str | Path,
    project: str = "",
    live: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Return a node's unmet refs after validating its dependency graph."""
    return dispatch_waits(node, repo_root=repo_root, project=project, live=live)[2]


def _ref_state(
    ref: str,
    *,
    waiting_section: str,
    rows: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]],
    dead_sections: set[tuple[str, str]],
    project: str = "",
    docs_dir: Path | None = None,
    cache: dict[str, Any] | None = None,
) -> str:
    """Resolve a plan or section ref from its project's declared state."""
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import COMPLETED_STATUSES, TERMINAL_STATUSES

    parsed = parse_plan_ref(ref)
    if parsed is None:
        return PENDING
    if docs_dir is not None and cache is not None:
        target_project = parsed.project or project
        target_docs = _docs_for_project(
            target_project, current_project=project, current_docs=docs_dir
        )
        if target_docs is None:
            return PENDING
        rows, inventory = _readiness_rows(target_project, target_docs, cache)
    slug, section = parsed.slug, normalize_section(parsed.stage or "")
    if section and (slug, section) in dead_sections:
        return ABANDONED
    item = inventory.get(slug)
    if item is None:
        return PENDING
    status = str(item.get("workflow_status") or item.get("status") or "")
    if not section:
        if status in COMPLETED_STATUSES:
            return SATISFIED
        if status in TERMINAL_STATUSES:
            return ABANDONED
        return PENDING
    declaration = _section_status(item, section)
    if declaration == "done":
        return SATISFIED
    if declaration in {"descoped", "deferred", "failed", "blocked"}:
        return ABANDONED
    if status in TERMINAL_STATUSES:
        return ABANDONED
    verdicts = (rows.get(slug) or {}).get("sections", {})
    if not any(normalize_section(key) == section for key in verdicts):
        return PENDING
    return PENDING


def _dead_sections(live: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """The ``(plan, section)`` pairs no live request will ever deliver.

    A section whose own request is blocked on its own upstream will not deliver
    whatever waits behind it, and neither will one whose request is recorded
    failed or deliberately discarded. The set is what makes blocking
    transitive: the tail of a chain is blocked because the middle is, without
    the tail's ref having to name the original failure.
    """
    dead: set[tuple[str, str]] = set()
    for pointer in live:
        node = dict(pointer.get("node") or {})
        plan = str(node.get("plan") or pointer.get("plan") or "")
        section = str(node.get("section") or pointer.get("section") or "")
        if not plan or not section:
            continue
        phase = str(pointer.get("phase") or "")
        if phase in (BLOCKED_UPSTREAM_PHASE, *DEAD_RUN_PHASES) or _discarded(
            str(pointer.get("run_id") or "")
        ):
            dead.add((plan, normalize_section(section)))
    return dead


def _discarded(run_id: str) -> bool:
    """Whether a deliberate discard is recorded for one run.

    A discard is the operator closing a request as abandoned, so it reads as
    abandoned to whatever waits behind it. An unreadable discard store is no
    evidence of a discard, so it reads as none.
    """
    from reckon.crew.promotion import discard_recorded

    try:
        return bool(discard_recorded(run_id))
    except Exception:  # noqa: BLE001 - an unreadable discards store is no evidence
        return False


def resolve_waits(
    pointer: Mapping[str, Any],
    *,
    live: Sequence[Mapping[str, Any]],
    docs_dir: str | Path | None,
    cache: dict[str, Any],
    dead_sections: set[tuple[str, str]] = frozenset(),  # type: ignore[assignment]
) -> str:
    """The phase one waiting request takes, from the roadmap's own readiness.

    ``awaiting-upstream`` while every ref is pending, ``blocked-upstream`` the
    moment any ref is closed as abandoned or failed, and ``queued`` once the
    last ref lands. A request carrying a ref that is neither of those stays
    exactly where it is: an unobserved ref is not evidence that its dependency
    landed.
    """
    refs = section_waits_for(pointer)
    if not refs:
        return QUEUED_PHASE
    rows, inventory = _readiness_rows(
        str(pointer.get("project") or "") or _project_of_docs_dir(docs_dir),
        docs_dir,
        cache,
    )
    issues: list[str] = []
    owner_project = str(pointer.get("project") or "") or _project_of_docs_dir(docs_dir)
    states = [
        _wait_state(
            ref,
            waiting_section=str(
                dict(pointer.get("node") or {}).get("section")
                or pointer.get("section")
                or ""
            ),
            rows=rows,
            inventory=inventory,
            dead_sections=dead_sections,
            live=live,
            project=owner_project,
            docs_dir=Path(docs_dir) if docs_dir is not None else None,
            cache=cache,
            wait_projects=dict(pointer.get("wait_projects") or {}),
            issues=issues,
        )
        for ref in refs
    ]
    if isinstance(pointer, dict):
        pointer["upstream_obligations"] = list(dict.fromkeys(issues))
    if ABANDONED in states:
        return BLOCKED_UPSTREAM_PHASE
    if all(state == SATISFIED for state in states):
        return QUEUED_PHASE
    return AWAITING_UPSTREAM_PHASE


def _rederive_waits(
    pointer: dict[str, Any],
    *,
    cache: dict[tuple[str, str, str], list[str]] | None = None,
) -> bool:
    """Re-read a stored request's waits from the plan and rewrite them.

    The plan is what a coordinator steers a queued request through, so an edit
    that narrows a section's waits can release the request and an edit that
    widens them can send it back to ``awaiting-upstream``. The derivation is
    the one that composed the waits at dispatch, and it keeps the request's own
    ``--after`` refs because no plan edit carries them. A pointer with no
    repository, or one whose plan cannot be read, keeps the waits it has: an
    unreadable authority is not evidence that the waits changed.
    """
    node = _node_from_pointer(pointer)
    repo_root = str(pointer.get("repo") or "")
    if not repo_root:
        return False
    if not node.plan.strip() and not node.after:
        return False
    key = (repo_root, node.plan, node.section)
    derived = None if cache is None else cache.get(key)
    if derived is None:
        derived = derive_wait_refs(node, repo_root=repo_root)
        if cache is not None:
            cache[key] = list(derived)
    stored = section_waits_for(pointer)
    # The node's own ``--after`` refs are kept whatever the plan now says, so a
    # re-derived list has to be compared as the union it is: the request's refs
    # carrying the run: prefix are its own.
    own = {ref for ref in stored if ref.startswith(REQUEST_REF_PREFIX)}
    derived = list(dict.fromkeys([*derived, *own]))
    if derived == stored:
        return False
    run_id = str(pointer.get("run_id") or "")

    def rewrite(current: dict[str, Any]) -> dict[str, Any]:
        current["waits"] = list(derived)
        current["waits_at"] = _utc_now()
        return current

    try:
        runs._mutate_pointer(run_id, rewrite)
    except Exception:  # noqa: BLE001 - an unwritable pointer keeps its waits
        return False
    pointer["waits"] = list(derived)
    return True


def advance_waiting_requests(live: list[dict[str, Any]]) -> dict[str, Any]:
    """Move each waiting request to the phase its refs now justify.

    The pass re-reads readiness for every request in ``WAITING_UPSTREAM_PHASES``
    and moves it to ``queued`` or ``blocked-upstream``. Barrier blocking is
    transitive, so the resolution repeats to a fixpoint: a request blocked by a
    ref the same pass has just blocked becomes blocked too, whatever order the
    pointers are listed in. A queued request is read by the same pass, because
    a plan edit that widens its waits moves it back to ``awaiting-upstream``
    and one that narrows them can release it.
    """
    candidates = [
        pointer
        for pointer in live
        if str(pointer.get("phase") or "") in (*WAITING_UPSTREAM_PHASES, QUEUED_PHASE)
    ]
    if not candidates:
        return {"promoted": [], "blocked": [], "obligations": []}
    cache: dict[str, Any] = {}
    # One plan read per pass, shared by every request it derives for.
    derived_cache: dict[tuple[str, str, str], list[str]] = {}
    for pointer in candidates:
        try:
            _rederive_waits(pointer, cache=derived_cache)
        except Exception:  # noqa: BLE001, S112 - one unreadable request cannot end the pass
            continue
    promoted: list[str] = []
    blocked: list[str] = []
    obligations: dict[str, str] = {}
    for _round in range(len(candidates) + 1):
        dead = _dead_sections(live)
        changed = False
        for pointer in candidates:
            phase = str(pointer.get("phase") or "")
            if phase not in (*WAITING_UPSTREAM_PHASES, QUEUED_PHASE):
                continue
            docs_dir = Path(str(pointer.get("repo") or "") or ".") / "docs"
            previous_issues = list(pointer.get("upstream_obligations") or ())
            target = resolve_waits(
                pointer,
                live=live,
                docs_dir=docs_dir,
                cache=cache,
                dead_sections=dead,
            )
            run_id = str(pointer.get("run_id") or "")
            issues = list(pointer.get("upstream_obligations") or ())
            if issues:
                obligations[run_id] = issues[0]
            if issues != previous_issues:

                def record_issues(
                    current: dict[str, Any], recorded: list[str] = issues
                ) -> dict[str, Any]:
                    current["upstream_obligations"] = recorded
                    return current

                runs._mutate_pointer(run_id, record_issues)
            if target == phase:
                continue
            if not _move_pointer(run_id, target):
                continue
            pointer["phase"] = target
            changed = True
            if target == QUEUED_PHASE:
                promoted.append(run_id)
            elif target == BLOCKED_UPSTREAM_PHASE:
                blocked.append(run_id)
        if not changed:
            break
    return {
        "promoted": promoted,
        "blocked": blocked,
        "obligations": [
            {"run_id": run_id, "detail": detail}
            for run_id, detail in obligations.items()
        ],
    }


def _plan_html(repo_root: str | Path, plan: str) -> str:
    """One copy of a plan document, or an empty string when absent."""
    if not plan:
        return ""
    try:
        return (Path(str(repo_root)) / "docs" / "plans" / f"{plan}.html").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""


def queue_requests(
    nodes: Sequence[TaskNode],
    *,
    project: str,
    session: str,
    repo_root: str | Path,
    config: Mapping[str, Any],
    backend_name: str = "",
    launch_kind: str = "cli",
    run_ids: Mapping[str, str] | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    """Keep the batch entry while dispatch owns validation and queue writes.

    Each request is stamped with the plan digests it was queued against before
    this call returns, so the plan as the queuing session left it is the text
    admission holds the request to rather than whatever the plan says at its
    first pass.
    """
    from reckon.crew import runs as runs_module
    from reckon.crew.dispatch import queue_requests as dispatch_queue_requests

    stored = dispatch_queue_requests(
        nodes,
        project=project,
        session=session,
        repo_root=repo_root,
        config=config,
        backend_name=backend_name,
        launch_kind=launch_kind,
        run_ids=run_ids,
        cap=cap,
    )
    for run_id in stored:
        pointer = runs_module.read_pointer(run_id)
        if isinstance(pointer, dict):
            record_review_contract(pointer)
    return stored


class _PointerNotHoldableError(Exception):
    """A request that is a live run, not a waiting request, may not be held.

    Only a request still waiting for a slot — ``queued`` — or already waiting
    for its upstream — any of ``WAITING_UPSTREAM_PHASES`` — can be held. Every
    other phase names a run the fleet is working on: one being admitted holds a
    worktree, and one starting or working is a live worker. Writing a wait over
    such a pointer would take a running run off its lane and re-store it as a
    request nothing had launched, so the write is refused and the pointer is
    left exactly as it stands.
    """


def _hold_pointer(
    run_id: str, refs: Sequence[str], *, wait_projects: Mapping[str, str] | None = None
) -> bool:
    """Record a request's refs and its waiting phase in one write.

    The refs and the phase are one fact about a request, so they are written
    together: a pointer left holding refs in the queued phase would be launched
    by the pass that never sees it waiting. A pointer that cannot be written
    stays queued and launches when the lane reaches it, which is the behaviour
    of a request whose plan declares no wait.

    A pointer carrying a run that is already admitted or working is not the
    record of a request at all, so nothing is written to it and the hold
    reports that it did not land. The phase is read inside the same mutation
    that writes, under the pointer's own lock, so a run admitted between the
    caller's decision and this write is refused too.
    """

    def hold(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("phase") or "") not in (
            QUEUED_PHASE,
            *WAITING_UPSTREAM_PHASES,
        ):
            raise _PointerNotHoldableError(run_id)
        current["waits"] = list(refs)
        current["wait_projects"] = dict(wait_projects or {})
        current["phase"] = AWAITING_UPSTREAM_PHASE
        current["waits_at"] = _utc_now()
        return current

    try:
        runs._mutate_pointer(run_id, hold)
    except _PointerNotHoldableError:
        return False
    except Exception:  # noqa: BLE001 - an unwritable pointer stays where it is
        return False
    return True


def review_contract(node: TaskNode, *, repo_root: str | Path) -> dict[str, Any] | None:
    """The plan digests a queued request is reviewed against, taken as it queues.

    A request is gated on its own contract when it enters the queue: its
    section's text and the document text it reads as that contract — the
    summary, the decisions and the gates. Both digests are taken here, from the
    plan as the requesting session queued it, so a later edit is measured
    against what the request was dispatched for rather than against the whole
    plan's debt. ``None`` when the plan cannot be read: a request whose contract
    was never recorded is not held on a comparison it has no side of.
    """
    from reckon import _plan_html as plan_html
    from reckon.crew import plan_review, routing

    plan = str(node.plan or "").strip()
    if not plan:
        return None
    try:
        path = Path(str(repo_root)) / "docs" / "plans" / f"{plan}.html"
        state = plan_html.read_state_file(path)
        digests = plan_review._section_digests(path)
    except Exception:  # noqa: BLE001 - an unreadable plan records no contract
        return None
    unit = ""
    if str(node.section or "").strip():
        from reckon._plan_html import section_record_id

        unit = section_record_id(node.section)
    return {
        "section": unit,
        "section_digest": str(digests.get(unit) or ""),
        "document_digest": routing._contract_digest(state),
        "recorded_at": _utc_now(),
    }


def _contract_is_current(contract: Mapping[str, Any], node: TaskNode) -> bool:
    """Whether a recorded contract still describes the section now queued.

    ``crew queue edit`` changes a request's section, and the digests recorded
    for the old section say nothing about the new one, so such a request is
    stamped again from the plan rather than held on a comparison of two
    different sections.
    """
    from reckon._plan_html import section_record_id

    recorded = str(contract.get("section") or "")
    present = section_record_id(node.section) if str(node.section or "").strip() else ""
    return recorded == present


def record_review_contract(pointer: Mapping[str, Any]) -> bool:
    """Stamp one queued pointer with the contract it was queued against.

    The write happens under the pointer's own lock and only while the request
    still waits, so a pointer that has been launched is left alone. A pointer
    that cannot be written keeps no contract and is not held on one.
    """
    if not isinstance(pointer, dict):
        return False
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return False
    node = _node_from_pointer(pointer)
    if not node.plan.strip():
        return False
    contract = review_contract(node, repo_root=str(pointer.get("repo") or ""))
    if contract is None:
        return False

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("phase") or "") not in (
            QUEUED_PHASE,
            *WAITING_UPSTREAM_PHASES,
        ):
            raise _PointerNotHoldableError(run_id)
        current[REVIEW_CONTRACT_KEY] = contract
        return current

    try:
        runs._mutate_pointer(run_id, stamp)
    except Exception:  # noqa: BLE001 - an unwritten contract holds nothing
        return False
    pointer[REVIEW_CONTRACT_KEY] = contract
    return True


def _move_pointer(run_id: str, phase: str) -> bool:
    """Write one request's new phase, reporting whether the write landed.

    A pointer that cannot be written stays where it is and is attempted again
    on the next pass; the pass never treats an unwritten move as done, so a
    request is not launched from a phase the store never recorded.
    """

    def move(current: dict[str, Any]) -> dict[str, Any]:
        current["phase"] = phase
        current["waits_at"] = _utc_now()
        return current

    try:
        runs._mutate_pointer(run_id, move)
    except Exception:  # noqa: BLE001 - an unwritable pointer stays where it is
        return False
    return True


def _dispatch_gate_owed(
    node: TaskNode,
    *,
    project: str,
    repo: str,
    options: Mapping[str, Any],
) -> str | None:
    """The dispatch gate's verdict for a request that recorded no contract."""
    from reckon.crew import routing

    try:
        authority = routing.resolve_dispatch_authority(project, repo)
        routing.require_plan_reviewed(
            node=node,
            project=project,
            repo=repo,
            authority=authority,
            allow_unreviewed=bool(options.get("allow_unreviewed_plan")),
            enforce=True,
            units=plan_review_units(node.section),
        )
    except PlanReviewMissingError as exc:
        return str(exc)
    except Exception:  # noqa: BLE001 - an unreadable authority holds nothing
        return None
    return None


def _plan_review_owed(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """Why this request's own contract owes a stored review, or ``None``.

    A request is checked for review when it enters the queue, and the dispatch
    gate has already judged it there. What admission owes is the narrower
    question the queue raises: has the plan text this request was queued
    against moved since, and does no stored review cover the text it moved to?
    Only the request's own section and the document text it reads as its
    contract — the summary, the decisions and the gates — are compared, so an
    edit to another section, an ordering or bookkeeping edit, and a contract
    change a review already covers all leave the request admissible. A fresh
    dispatch of the same section and this request therefore reach the same
    verdict on the same plan.

    Two states carry no comparison of their own, and both are answered by the
    dispatch gate rather than by holding nothing. A pointer with no contract
    current for the section it now names — every request written by the queue
    writer that records none, and every request whose section ``crew queue
    edit`` changed — has no second side of a digest comparison. A plan whose
    stored reviews cover no unit the request reads — an unreviewed plan, or one
    whose every review is incomplete — has no reviewed text to compare against
    and is owed the review the gate asks for. Each is held exactly when the
    gate holds it, so the two never disagree.

    Only the enforced mode holds, exactly as the dispatch gate does; under the
    shipped report-only default the launch proceeds.
    """
    from reckon import _plan_html as plan_html
    from reckon import flight
    from reckon.crew import plan_review, routing

    node = _node_from_pointer(pointer)
    project = str(pointer.get("project") or "")
    repo = str(pointer.get("repo") or "")
    if not node.plan.strip() or not project or not repo:
        return None
    config = (
        config_for(pointer) if config_for is not None else _resolve_lane_config(pointer)
    )
    if not flight.plan_review_gate_enforces(config):
        return None
    options = dict(pointer.get("dispatch_options") or {})
    if options.get("allow_unreviewed_plan"):
        # A request dispatched with the waiver carries it into the queue: its
        # coordinator has answered the review question for that dispatch.
        return None
    contract = pointer.get(REVIEW_CONTRACT_KEY)
    if not isinstance(contract, Mapping) or not _contract_is_current(contract, node):
        # A request queued by a writer that records no contract is judged by the
        # dispatch gate itself, which is what admission asked before requests
        # carried their own: it holds on every unit the request reads. Narrowing
        # that to the request's own contract needs the digests taken at queue
        # time, so a pointer without them keeps the wider hold rather than
        # holding nothing.
        return _dispatch_gate_owed(node, project=project, repo=repo, options=options)
    try:
        path = Path(repo) / "docs" / "plans" / f"{node.plan}.html"
        records, uncovered, changes = plan_review.review_coverage(
            project, node.plan, plan=path
        )
        units = plan_review_units(node.section)
        if not any(unit not in uncovered for unit in units):
            # No stored review covers a single unit this request reads — the
            # plan is unreviewed, or every review is incomplete — so there is no
            # reviewed text to compare against and the request is owed the
            # review the dispatch gate asks for. Answering here with the gate's
            # own verdict is what keeps a queued request and a fresh dispatch
            # from disagreeing.
            return _dispatch_gate_owed(
                node, project=project, repo=repo, options=options
            )
        digests = plan_review._section_digests(path)
        moved: list[str] = []
        unit = str(contract.get("section") or "")
        if (
            unit
            and unit in uncovered
            and str(digests.get(unit) or "") != str(contract.get("section_digest"))
        ):
            moved.append(unit)
        state = plan_html.read_state_file(path)
        if (
            plan_review.DOCUMENT_UNIT in uncovered
            and routing._contract_digest(state)
            != str(contract.get("document_digest") or "")
            and routing._document_change_is_contract(
                project, node.plan, plan=path, records=records
            )
        ):
            moved.append(plan_review.DOCUMENT_UNIT)
    except Exception:  # noqa: BLE001 - an unreadable plan holds nothing
        return None
    if not moved:
        return None
    return (
        f"plan {node.plan!r} in project {project!r} changed after this request "
        "was queued and no stored review covers the new text; "
        f"uncovered units: {', '.join(sorted(moved))}; "
        f"{routing._uncovered_change_detail(moved, changes)}"
        "the request waits until a review of that text is stored; "
        f"compose one with `{plan_review.review_invocation(project, node.plan)}`"
    )


def _hold_for_plan_review(pointer: Mapping[str, Any], detail: str) -> bool:
    """Move a request the gate holds to ``held``, recording why and its remedy.

    The hold is marked automatic so that the pass which finds the review stored
    releases it, while a hold a coordinator wrote is left alone whatever the
    plan's review and this appointment's review have in common.
    """
    run_id = str(pointer.get("run_id") or "")
    started = _utc_now()

    def hold(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("phase") or "") not in (
            QUEUED_PHASE,
            *WAITING_UPSTREAM_PHASES,
        ):
            raise _PointerNotHoldableError(run_id)
        current["queue_hold"] = {
            "prior_phase": str(current.get("phase") or QUEUED_PHASE),
            "started_at": started,
            "reason": PLAN_REVIEW_HELD_REASON,
            "detail": detail,
            "automatic": True,
        }
        current["phase"] = HELD_PHASE
        return current

    try:
        runs._mutate_pointer(run_id, hold)
    except _PointerNotHoldableError:
        return False
    except Exception:  # noqa: BLE001 - an unwritable pointer stays where it is
        return False
    if isinstance(pointer, dict):
        pointer["phase"] = HELD_PHASE
    return True


def _release_plan_review_holds(
    live: Sequence[Mapping[str, Any]],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> list[str]:
    """Release every request this path held whose units are now reviewed.

    The pass asks the gate again rather than trusting a record of the hold, so
    a review stored since the last pass releases its requests with no
    coordinator call. A hold a coordinator wrote carries no ``automatic`` mark
    and is never released here, and a request so held stays held.
    """
    released: list[str] = []
    for pointer in live:
        if str(pointer.get("phase") or "") != HELD_PHASE:
            continue
        hold = pointer.get("queue_hold")
        if not isinstance(hold, Mapping) or not hold.get("automatic"):
            continue
        if str(hold.get("reason") or "") != PLAN_REVIEW_HELD_REASON:
            continue
        if _plan_review_owed(pointer, config_for=config_for) is not None:
            continue
        prior = str(hold.get("prior_phase") or "")
        if prior not in (QUEUED_PHASE, *WAITING_UPSTREAM_PHASES):
            continue
        run_id = str(pointer.get("run_id") or "")
        ended = _now()

        def release(
            current: dict[str, Any],
            prior_phase: str = prior,
            released_at: datetime = ended,
        ) -> dict[str, Any]:
            started = parse_utc((current.get("queue_hold") or {}).get("started_at"))
            queued_at = parse_utc(current.get("queued_at"))
            if started is not None and queued_at is not None:
                current["queued_at"] = (queued_at + (released_at - started)).isoformat()
            current["phase"] = prior_phase
            current.pop("queue_hold", None)
            current["queue_released_at"] = released_at.isoformat()
            return current

        try:
            runs._mutate_pointer(run_id, release)
        except Exception:  # noqa: BLE001, S112 - an unwritable pointer stays held
            continue
        if isinstance(pointer, dict):
            pointer["phase"] = prior
            pointer.pop("queue_hold", None)
        released.append(run_id)
    return released


def _fair_use_weights_configured(config: Mapping[str, Any]) -> bool:
    from reckon import flight as flight_module

    return flight_module.fair_use_weights_configured(config)


def _fair_use_weights(
    pointers: Sequence[Mapping[str, Any]],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, float] | None:
    """Each project's declared fair-use weight, or ``None`` when none is set.

    The weights live in flight configuration, so they are resolved through the
    same per-project config the pass reads for every other question. Only the
    weights each config actually declares are merged, first declaration winning,
    and the equal default is applied once at the end: merging completed weight
    maps instead would let whichever project came first in the queue fix every
    other project's weight at the default. ``None`` is what tells the pass to
    keep the plain admission order: a configuration that declares no weight
    keeps exactly the order it had.
    """
    from reckon import flight as flight_module

    projects: list[str] = []
    configs: dict[str, Mapping[str, Any]] = {}
    for pointer in pointers:
        project = str(pointer.get("project") or "")
        if project in configs:
            continue
        projects.append(project)
        configs[project] = (
            config_for(pointer)
            if config_for is not None
            else _resolve_lane_config(pointer, select_local=False)
        )
    declared: dict[str, float] = {}
    for project in projects:
        for name, weight in flight_module.declared_project_weights(
            configs[project]
        ).items():
            declared.setdefault(name, weight)
    if not declared:
        return None
    return flight_module.project_weights(
        {flight_module.QUEUE_KEY: {flight_module.QUEUE_PROJECT_WEIGHTS_KEY: declared}},
        projects,
    )


def weighted_project_order(
    ordered: Sequence[Mapping[str, Any]],
    weights: Mapping[str, float],
) -> list[Mapping[str, Any]]:
    """Interleave a priority-ordered queue so each project's share tracks its weight.

    Weighted deficit round robin across the projects that still hold a request:
    every such project accrues its weight per round, the largest deficit is
    served next and pays the round's total, and its highest-ranked request —
    the one the priority order already placed first for that project — is
    taken. A project with nothing left accrues nothing, so a queue shallower
    than its share never holds a slot idle while another project has work.
    """
    pending: dict[str, list[Mapping[str, Any]]] = {}
    first_seen: dict[str, int] = {}
    for index, pointer in enumerate(ordered):
        project = str(pointer.get("project") or "")
        pending.setdefault(project, []).append(pointer)
        first_seen.setdefault(project, index)
    deficits: dict[str, float] = dict.fromkeys(pending, 0.0)
    servable = list(pending)
    result: list[Mapping[str, Any]] = []
    while servable:
        total = sum(weights.get(project, 1.0) for project in servable)
        for project in servable:
            deficits[project] += weights.get(project, 1.0)
        chosen = max(
            servable, key=lambda project: (deficits[project], -first_seen[project])
        )
        deficits[chosen] -= total
        result.append(pending[chosen].pop(0))
        if not pending[chosen]:
            servable.remove(chosen)
    return result


def fair_share_admission_order(
    queued: Sequence[Mapping[str, Any]],
    live: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    """The order one admission pass will launch the queue in.

    The priority order (:func:`reckon.crew.queue_order.admission_order`) still
    ranks the requests, and the projects are interleaved across it by their
    configured fair-use weights. With no weight declared the priority order is
    returned unchanged, so an install that sets none keeps exactly the order it
    had.
    """
    order = queue_order.admission_order(
        queued,
        [pointer for pointer in live if pointer.get("phase") != HELD_PHASE],
        now=now,
    )
    weights = _fair_use_weights(queued, config_for=config_for)
    if weights is None:
        return order
    return weighted_project_order(order, weights)


def queued_placements(
    pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    lane: str = "",
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return each queued request's place in the order the pass admits by.

    Every surface that reports where a queued run stands reads its place here
    rather than ordering the queue itself: a second ordering is how two
    surfaces come to disagree about which request goes first. The order is the
    admission pass's own, computed by
    :func:`reckon.crew.queue_order.admission_order` on the same snapshot the
    pass ranks, including the configured project weights, so a request's
    reported place is the place it will be launched at.

    The place is published as three figures, because they answer three questions.
    ``position`` is its place among every queued request on the lane, which is
    the figure a dispatch reports when a dispatch is queued. ``rank`` is its
    place among its own session's queued requests, which is what a coordinator
    reading its own queue wants. ``effective_rank`` is the weighted launch
    place when weights are configured, or its place within its project when
    they are absent. ``reason`` names the signal that placed it. ``lane`` narrows
    the ordering to one backend's
    queued requests, matching the narrowing the pass applies to its own queue.

    A queued request the snapshot does not carry reads no place, so a caller
    asks only about requests it already holds. A queued pointer without a
    readable ``queued_at`` also reads no place: the ordering cannot place it, so
    it is left out of the ordering rather than allowed to take the whole read
    down. Its run is still listed as queued by every surface that reads the
    snapshot itself; only its place is withheld, and every other pointer keeps
    the place the ordering gives it.
    """
    moment = now or _now()
    queued = [
        pointer
        for pointer in pointers
        if str(pointer.get("phase") or "") == QUEUED_PHASE
        and (not lane or str(pointer.get("backend") or "") == lane)
        and parse_utc(pointer.get("queued_at")) is not None
    ]
    if not queued:
        return {}
    placements: dict[str, dict[str, Any]] = {}
    session_rank: dict[str, int] = {}
    project_rank: dict[str, int] = {}
    details = queue_order.admission_order_details(
        queued,
        [pointer for pointer in pointers if pointer.get("phase") != HELD_PHASE],
        now=moment,
    )
    reasons = {str(pointer.get("run_id") or ""): reason for pointer, reason in details}
    weights = _fair_use_weights(queued, config_for=config_for)
    order = [pointer for pointer, _reason in details]
    if weights is not None:
        order = weighted_project_order(order, weights)
    for position, pointer in enumerate(order, 1):
        session = str(pointer.get("session") or "")
        project = str(pointer.get("project") or "")
        session_rank[session] = session_rank.get(session, 0) + 1
        project_rank[project] = project_rank.get(project, 0) + 1
        reason = reasons[str(pointer.get("run_id") or "")]
        if weights is not None:
            reason = f"weighted fair use ({project}: {weights[project]:g}); {reason}"
        placements[str(pointer.get("run_id") or "")] = {
            "position": position,
            "rank": session_rank[session],
            "effective_rank": position
            if weights is not None
            else project_rank[project],
            "reason": reason,
        }
    return placements


def admit_once(
    *,
    launcher: Callable[..., int] | None = None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    lane: str = "",
    now: datetime | None = None,
    lock: bool = True,
) -> dict[str, Any]:
    """Run one admission pass and report what it launched, held and failed.

    The pass reads the fleet gate, then the queued and live pointers. It ranks
    the queued entries by current lane occupancy and admits each in turn while
    the lane grants its session room. A held or paused gate launches nothing.
    A queued pointer whose arrival time cannot be read is left out of the
    ordering and named in a verdict, so it costs its own place rather than
    ending the pass for every other request on the lane.
    ``lock`` serialises the pass against a concurrent drainer so one queued run
    launches once; a caller already holding the admit lock passes ``lock=False``.
    """
    moment = now or _now()
    gate = _dispatch_fleet_gate()
    result: dict[str, Any] = {
        "gate": gate.get("state"),
        "launched": [],
        "held": [],
        "failed": [],
        "requeued": [],
        "verdicts": [],
    }
    local_gated = gate.get("state") in _LANE_GATE_WAITING_STATES
    if local_gated:
        _record_gated_verdicts(result, gate=gate, lane=lane, config_for=config_for)
    with _admit_lock() if lock else nullcontext():
        result["requeued"] = _requeue_admitted_failures(
            runs.list_live(phase=LAUNCH_FAILED_PHASE)
        )
        live = runs.list_live()
        # Readiness is re-read here, on every pass: a request waiting on its
        # section's refs moves to ``queued`` the moment the last ref lands and
        # to ``blocked-upstream`` the moment one is closed as abandoned or
        # failed, with no coordinator turn in between. This is what lets a
        # batch queued in one call launch in dependency order.
        upstream = advance_waiting_requests(live)
        result["released_plan_review"] = _release_plan_review_holds(
            live, config_for=config_for
        )
        result["promoted_upstream"] = upstream["promoted"]
        result["blocked_upstream"] = upstream["blocked"]
        result["upstream_obligations"] = upstream["obligations"]
        runs.sweep_handovers(str(pointer.get("run_id") or "") for pointer in live)
        queued = [pointer for pointer in live if pointer.get("phase") == QUEUED_PHASE]
        if lane:

            def included(pointer: Mapping[str, Any]) -> bool:
                backend = str(pointer.get("backend") or "")
                if backend == lane:
                    return True
                config = (
                    config_for(pointer)
                    if config_for is not None
                    else _resolve_lane_config(pointer, select_local=False)
                )
                # The installed local drainer also admits every metered route.
                return (
                    lane == str(config.get("local_backend") or "") and backend != lane
                )

            queued = [pointer for pointer in queued if included(pointer)]
        if local_gated:
            queued = [
                pointer
                for pointer in queued
                if str(pointer.get("backend") or "")
                != str(
                    (
                        config_for(pointer)
                        if config_for is not None
                        else _resolve_lane_config(pointer, select_local=False)
                    ).get("local_backend")
                    or ""
                )
            ]
        # A queued pointer whose arrival time cannot be read has no place in the
        # ordering, and the ordering refuses such a pointer outright. That
        # refusal belongs to the pointer, not to the pass: left to propagate, one
        # such record — a fixture, or a hand-edited record in the crew home —
        # ends the pass and the service with it, so no queued request on the lane
        # launches for any session. The pointer is left out of the ordering and
        # named in a verdict instead, and every other request is admitted in the
        # order the pass would otherwise have given them.
        placeable = [
            pointer
            for pointer in queued
            if parse_utc(pointer.get("queued_at")) is not None
        ]
        unplaceable = [
            pointer for pointer in queued if parse_utc(pointer.get("queued_at")) is None
        ]
        order = fair_share_admission_order(
            placeable, live, now=moment, config_for=config_for
        )
        remaining: dict[tuple[str, str], float | None] = {}
        allowance_sources: dict[tuple[str, str], str] = {}
        verdicts: list[dict[str, Any]] = result["verdicts"]
        for pointer in unplaceable:
            allowance, source = _session_allowance_read(pointer, config_for=config_for)
            verdicts.append(
                _verdict_row(
                    pointer,
                    allowance=allowance,
                    allowance_source=source,
                    verdict="unplaced",
                    detail="the queued pointer carries no readable queued_at",
                )
            )
        # The pass's own claim resolutions, so every overlap comparison reads a
        # request's resolved claims rather than resolving them again. A fresh
        # cache per pass means a request whose write paths or mounts change is
        # resolved afresh on the next pass.
        claims: dict[str, list[tuple[Path | None, Path]]] = {}
        for index, pointer in enumerate(order):
            session = str(pointer["session"])
            allowance_key = (str(pointer.get("backend") or ""), session)
            if allowance_key not in remaining:
                read, source = _session_allowance_read(pointer, config_for=config_for)
                remaining[allowance_key] = read
                allowance_sources[allowance_key] = source
            allowance = remaining[allowance_key]
            allowance_source = allowance_sources[allowance_key]
            if allowance is not None and allowance <= 0:
                result["held"].append(str(pointer["run_id"]))
                verdicts.append(
                    _verdict_row(
                        pointer,
                        allowance=allowance,
                        allowance_source=allowance_source,
                        verdict="held-for-allowance",
                    )
                )
                continue
            gate_hold = _lane_gate_hold(pointer, config_for=config_for)
            if gate_hold is not None:
                # The gate would refuse the launch the release below feeds into,
                # so the request is left where it is: releasing it would emit a
                # withdrawal and a re-queue for a state that never moved. The
                # session-allowance hold above is the same question's other half.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = gate_hold
                verdicts.append(
                    _verdict_row(
                        pointer,
                        allowance=allowance,
                        allowance_source=allowance_source,
                        verdict="held-by-gate",
                        detail=gate_hold,
                    )
                )
                continue
            backoff_hold = _retry_backoff_hold(
                pointer, moment=moment, config_for=config_for
            )
            if backoff_hold is not None:
                # The request failed for a reason other than a lane hold and
                # nothing it depends on has changed yet, so it is left exactly as
                # it is rather than released, failed and restored again.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                    backoff_hold
                )
                verdicts.append(
                    _verdict_row(
                        pointer,
                        allowance=allowance,
                        allowance_source=allowance_source,
                        verdict="held-on-retry-backoff",
                        detail=backoff_hold,
                        retry_at=str(pointer.get("retry_at") or ""),
                    )
                )
                continue
            overlap = _earlier_overlapping_request(
                pointer, order[:index], moment=moment, claims=claims
            )
            if overlap is not None:
                # An earlier-ranked queued request names a path this one also
                # names, so this launch would be refused against a claim that
                # fences nothing while both wait. It is left exactly as it is —
                # queued, with no failure recorded — and the head of the queue,
                # which has no earlier overlap, is never held this way.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                    f"held behind earlier queued request "
                    f"{str(overlap.get('run_id') or 'unknown')!r} over a shared path"
                )
                verdicts.append(
                    _verdict_row(
                        pointer,
                        allowance=allowance,
                        allowance_source=allowance_source,
                        verdict="held-behind-earlier-request",
                        detail=str(overlap.get("run_id") or ""),
                    )
                )
                continue
            review_owed = _plan_review_owed(pointer, config_for=config_for)
            if review_owed is not None and _hold_for_plan_review(pointer, review_owed):
                # The plan's own units moved under the request since it was
                # queued. Held, not failed: the request keeps its place, its
                # waits and its stored brief, and the pass that finds a stored
                # review covering those units releases it without a
                # coordinator turn.
                result["held"].append(str(pointer["run_id"]))
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                    review_owed
                )
                verdicts.append(
                    _verdict_row(
                        pointer,
                        allowance=allowance,
                        allowance_source=allowance_source,
                        verdict="held-on-plan-review",
                        detail=review_owed,
                    )
                )
                continue
            snapshot = copy.deepcopy(dict(pointer))
            first_report = not pointer.get("failure_reported_at")
            record = None
            failure = "dispatch did not return a launched record"
            caught: Exception | None = None
            hold_only = False
            reported_id = str(pointer["run_id"])
            restore_error = None
            # A handover identifies the admitted attempt while its original
            # pointer remains published and dispatch checks the launch.
            runs.write_handover(pointer)
            try:
                candidate = _launch_queued(
                    pointer, launcher=launcher, config_for=config_for
                )
                record = _require_launched_record(candidate)
            except BudgetHold as exc:
                rebound = _local_fallback(pointer, exc, config_for=config_for)
                if rebound is None:
                    _record_metered_hold(reported_id, exc)
                    caught = exc
                    hold_only = True
                else:
                    snapshot = copy.deepcopy(rebound)
                    try:
                        candidate = _launch_queued(
                            rebound,
                            launcher=launcher,
                            config_for=config_for,
                            backend_override=str(rebound["backend"]),
                        )
                        record = _require_launched_record(candidate)
                    except (BudgetHold, LaneHeld, LanePaused) as fallback_hold:
                        caught = fallback_hold
                        hold_only = True
                        if isinstance(fallback_hold, BudgetHold):
                            _record_metered_hold(reported_id, fallback_hold)
                    except Exception as fallback_error:  # noqa: BLE001 - restore the waiting request
                        caught = fallback_error
                        failure = f"{type(fallback_error).__name__}: {fallback_error}"
            except Exception as exc:  # noqa: BLE001 - every dispatch failure keeps its request
                caught = exc
                failure = f"{type(exc).__name__}: {exc}"
            finally:
                if record is None and not hold_only:
                    reported_id, restore_error = _record_queued_failure(
                        snapshot,
                        failure,
                        moment=moment,
                        guard=_dependency_guard(pointer, config_for),
                    )
                # The launch outcome is settled here: either the run's own
                # pointer names the node, or the request has been written back.
                runs.clear_handover(pointer)
            if record is None:
                if first_report and not hold_only:
                    _report_queued_failure(pointer)
                key = (
                    "held"
                    if isinstance(caught, (BudgetHold, LaneHeld, LanePaused))
                    else "failed"
                )
                detail_key = "hold_detail" if key == "held" else "failure_detail"
                if caught is not None and isinstance(
                    caught, (BudgetHold, LaneHeld, LanePaused)
                ):
                    verdicts.append(
                        _verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-by-gate",
                            detail=str(caught),
                        )
                    )
                else:
                    verdicts.append(
                        _verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="refused",
                            failure=failure,
                        )
                    )
                result[key].append(reported_id)
                result.setdefault(detail_key, {})[reported_id] = (
                    str(caught) if caught is not None else failure
                )
                if restore_error is not None:
                    result.setdefault("restore_failure_detail", {})[reported_id] = (
                        restore_error
                    )
                continue
            _stamp_admission(str(record["run_id"]), snapshot, admitted_at=_utc_now())
            joined = _join_admitted_plan_review(snapshot, str(record["run_id"]))
            verdicts.append(
                _verdict_row(
                    pointer,
                    allowance=allowance,
                    allowance_source=allowance_source,
                    verdict="launched",
                )
            )
            result["launched"].append(str(record["run_id"]))
            if joined:
                joined_reviews = result.setdefault("joined_plan_reviews", {})
                joined_reviews[str(record["run_id"])] = joined
            if allowance is not None:
                remaining[allowance_key] = allowance - 1
        result["action"] = (
            "admitted" if result["launched"] else "gated" if local_gated else "idle"
        )
        return result


def _stamp_admission(
    run_id: str, request: Mapping[str, Any], *, admitted_at: str
) -> None:
    """Record on a launched run the queued request it was admitted from.

    The stamp is what lets the next pass recognise a run this drainer launched
    and re-queue it when the reaper marks it ``launch-failed``.
    """

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admitted_at"] = admitted_at
        pointer["admit_request"] = copy.deepcopy(dict(request))
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _join_admitted_plan_review(pointer: Mapping[str, Any], launched_run_id: str) -> str:
    """Write a launched plan review's run id into its composed dispatch sidecar.

    A queued plan review's report directory names the crew run that ran it only
    through ``dispatch.json``, and the compose left that sidecar unlaunched. The
    join resolves promotion's lookup, so it is part of admitting the request —
    not a later sweep. A pointer that names no plan review joins nothing and the
    pass carries on. Imported here rather than at module load so the admission
    surface does not depend on the review surface's own imports.
    """
    from reckon.crew import recovery_review_dispatch

    return recovery_review_dispatch.join_admitted_plan_review(pointer, launched_run_id)


def admit_until_stopped(
    *,
    lane: str = "",
    interval: float = DEFAULT_ADMIT_INTERVAL_SECONDS,
    once: bool = False,
    launcher: Callable[..., int] | None = None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Repeat the admission pass on a cadence until stopped.

    ``--once`` runs a single pass and returns, which is what a test drives. The
    loop otherwise catches ``KeyboardInterrupt`` so a stopped service returns
    the count of passes it ran rather than a traceback.

    Every pass whose lane had a queued pointer leaves one JSON line in the
    verdict log under the crew home, so a request that waited without launching
    can be read afterwards rather than inferred from a pointer that may since
    have been discarded. A pass that examined no queued pointer writes nothing.

    The loop also prints that same line to stdout as it is recorded, so a
    coordinator watching the bare verb sees what each pass decided while it is
    running rather than having to read the log afterwards to learn whether it
    was working. ``--once`` returns its pass to the caller instead and prints
    nothing here, because the verb reports that pass itself.

    The code stamp is compared at the head of every pass, before the pass runs.
    A pass must never validate or launch a queued request with code older than
    the checkout it launches from, and the checkout can move during the wait
    between passes: checking afterwards would let one more pass run on the old
    code before the move was noticed. So a stamp the checkout has moved past, on
    a checkout the trusted test accepts, re-executes this process in place before
    the next pass begins, and the failure guard's code fingerprint retries
    work held before re-exec. A stamp on a checkout the trusted test refuses
    leaves the service on its current code, so a tree mid-merge never replaces a
    running service.
    """
    # A service that replaced its own image in place still owns the children
    # the old image left. Registering them here, once, lets the shared reaper
    # collect them; nothing in this image refers to them otherwise.
    from reckon.crew.dispatch_launch import _adopt_launched_workers_from_reexec

    _adopt_launched_workers_from_reexec()
    admission_options: dict[str, Any] = {"lane": lane}
    if launcher is not None:
        admission_options["launcher"] = launcher
    if config_for is not None:
        admission_options["config_for"] = config_for
    if once:
        stamp = _admit_code_stamp()
        from reckon.crew.queue_landing import land_once

        landing = land_once()
        result = admit_once(**admission_options)
        result["landing"] = landing
        _record_pass_verdicts(result, lane=lane, code_stamp=stamp)
        return result
    passes = 0
    code_stamp = _admit_code_stamp()
    try:
        while True:
            if _admit_code_stamp() != code_stamp and _checkout_code_is_trusted():
                _reexec_into_current_code()
            from reckon.crew.queue_landing import land_once

            landing = land_once()
            admitted = admit_once(**admission_options)
            admitted["landing"] = landing
            _publish_pass_verdicts(
                _record_pass_verdicts(admitted, lane=lane, code_stamp=code_stamp)
            )
            passes += 1
            _pause(interval)
    except KeyboardInterrupt:
        return {"passes": passes}
