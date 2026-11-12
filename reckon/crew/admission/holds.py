"""Decide and name request holds, claim collisions and retry refusals."""

from __future__ import annotations

from . import launching as _launching
from . import ordering as _ordering
from . import service as _service
from .shared import (
    _CAPACITY_REFUSAL_REMEDY,
    _LANE_GATE_WAITING_STATES,
    _NODE_CONTRACT_REFUSAL,
    _NON_BINDING_HOLD_VERDICTS,
    _RETRY_HOLD_VERDICTS,
    ADMISSION_RETRY_BACKOFF_CAP_SECONDS,
    ADMISSION_RETRY_BACKOFF_SECONDS,
    RECORDED_HOLD_FIELD,
    REVIEW_ROLE,
    Any,
    BudgetHold,
    Callable,
    Collection,
    CompetenceLimit,
    CrewError,
    Mapping,
    Path,
    ScopeConflict,
    Sequence,
    UnreconciledRuns,
    _dispatch_lane_allowance,
    _dispatch_lane_gate,
    copy,
    datetime,
    os,
    parse_utc,
    queue_order,
    runs,
    subprocess,
    timedelta,
)


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
        config_for(pointer)
        if config_for is not None
        else _launching._resolve_lane_config(pointer)
    )
    backend = (config.get("backends") or {}).get(str(pointer.get("backend") or ""))
    if not backend:
        return None, "no backend declares the request's lane"
    decision = _dispatch_lane_allowance(
        backend,
        session=str(pointer["session"]),
        weights_configured=_ordering._fair_use_weights_configured(config),
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
        config_for(pointer)
        if config_for is not None
        else _launching._resolve_lane_config(pointer)
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
    """Rebind a chosen route to local when its metered lane stops serving.

    Two kinds of route are rebindable, and they are not the same claim. A
    picker's ``route`` decision names a lane the router chose for this request,
    so a lane that will not serve before its reset is a reason to move it. A
    reflex-composed review's route is the lane the review reflex followed the
    reviewed run onto: a preference, not a judgement about this request, and it
    carries no picked selection at all. Either yields to a hold.

    A route a caller named stays authoritative whatever the hold says: a
    coordinator who typed ``--backend`` or ``--local`` meant that lane, and the
    answer for it is a queued request that waits, not a silent move onto another
    lane. ``set_by`` is what separates the two, and only the reflex's own
    spelling is rebindable.
    """
    from reckon.crew.dispatch import REVIEW_FOLLOWS_RUN_ORIGIN

    selection = runs.dispatch_field(pointer, "picker_selection")
    named = pointer.get("named_route")
    origin = str(named.get("set_by") or "") if isinstance(named, Mapping) else ""
    if named and origin != REVIEW_FOLLOWS_RUN_ORIGIN:
        return None
    if not origin and (
        not isinstance(selection, Mapping) or selection.get("action") != "route"
    ):
        return None
    config = (
        config_for(pointer)
        if config_for is not None
        else _launching._resolve_lane_config(pointer, select_local=False)
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


def _non_binding_holds(verdicts: Sequence[Mapping[str, Any]]) -> set[str]:
    """The requests this pass has held for a reason outside the queue."""
    return {
        str(row.get("run_id") or "")
        for row in verdicts
        if str(row.get("verdict") or "") in _NON_BINDING_HOLD_VERDICTS
    }


def _earlier_overlapping_request(
    pointer: Mapping[str, Any],
    earlier: Sequence[Mapping[str, Any]],
    *,
    moment: datetime,
    claims: dict[str, list[tuple[Path | None, Path]]],
    fenced: Collection[str] = (),
) -> Mapping[str, Any] | None:
    """An earlier-ranked queued request whose write paths overlap this one.

    The head of the queue has no earlier-ranked request, so it is never held
    this way. Once this request has waited longer than the starvation age the
    pre-check stops holding it and it may launch ahead under the ordinary claim
    rules, so a later request is never held without end behind an earlier one
    whose own session is never granted a slot. ``claims`` is the pass's resolved
    claims, so each comparison reads a request's paths rather than resolving
    them again.

    An earlier request holds a later one only while it could launch: one the
    same pass has already held on a claim, a gate, a back-off or a plan review
    is named in ``fenced`` and holds nothing, so a request waiting on something
    outside the queue never parks the work behind it.
    """
    queued_at = parse_utc(pointer.get("queued_at"))
    if queued_at is not None and (
        (moment - queued_at).total_seconds() > queue_order.STARVATION_AGE_SECONDS
    ):
        return None
    skipping = set(fenced)
    for candidate in earlier:
        if str(candidate.get("run_id") or "") in skipping:
            continue
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


def _file_stamp(path: str) -> str:
    """A change detector for one dependency: its stat, or that it is absent."""
    if not path:
        return "none"
    try:
        stat = Path(path).stat()
    except OSError:
        return "absent"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _plan_document_path(pointer: Mapping[str, Any]) -> str:
    """The plan file a queued request's node names, inside its repository."""
    node = dict(pointer.get("node") or {})
    repo = str(pointer.get("repo") or "")
    plan = str(node.get("plan") or "")
    return str(Path(repo) / "docs" / "plans" / f"{plan}.html") if repo and plan else ""


def _plan_committed_stamp(pointer: Mapping[str, Any]) -> str:
    """The plan's committed content, as HEAD names the blob that holds it.

    Committing an edit leaves the plan file's working-tree stamp exactly where
    it was, so the dependency guard cannot see the commit that cures a request
    refused because its plan differed from base. The blob at HEAD *is* the
    committed content, so it moves when the commit moves rather than when the
    file does. A plan no commit holds yet reads ``absent``, which the first
    commit that lands it also changes.
    """
    repo = str(pointer.get("repo") or "")
    path = _plan_document_path(pointer)
    if not repo or not path:
        return "none"
    try:
        relative = Path(path).relative_to(Path(repo)).as_posix()
    except ValueError:
        return "outside"
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", f"HEAD:{relative}"],
        capture_output=True,
        text=True,
        check=False,
    )
    blob = result.stdout.strip()
    return blob if result.returncode == 0 and blob else "absent"


def _is_plan_visibility_refusal(failure: Any) -> bool:
    """Whether a recorded failure is the plan's own visibility refusing.

    The launch raises ``PlanVisibilityError`` while the plan's working tree
    differs from base, or while no commit holds the plan or the named section
    at all. Each of those is cured by committing the plan, so the plan's
    committed content is what releases the back-off; every other refusal keeps
    its back-off untouched.
    """
    return _failure_text(failure).startswith("PlanVisibilityError")


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
    brief = str(node.get("brief_path") or "")
    plan_path = _plan_document_path(pointer)
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
        "code": _service._admit_code_stamp(),
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
    if _launching._discarded_since(pointer):
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


def _declared_scope_paths(pointer: Mapping[str, Any]) -> list[str]:
    """The write paths one request declares, in the repository's own spelling.

    An absolute path inside the request's repository is read relative to it, so
    two requests queue from the same checkout compare equal however each
    declared them. Nothing is resolved: a pair this spelling misses is refused
    by the launch itself, exactly as it was before this comparison existed.
    """
    node = pointer.get("node")
    declared = list(node.get("write_paths") or ()) if isinstance(node, Mapping) else []
    repo = str(pointer.get("repo") or "")
    base = Path(os.path.normpath(repo)) if repo else None
    paths: list[str] = []
    for value in (str(item) for item in declared):
        raw = Path(value).expanduser()
        if raw.is_absolute():
            normalized = Path(os.path.normpath(str(raw)))
            if base is not None:
                try:
                    paths.append(normalized.relative_to(base).as_posix())
                    continue
                except ValueError:
                    pass
            paths.append(normalized.as_posix())
            continue
        paths.append(Path(os.path.normpath(str(raw))).as_posix())
    return paths


def _claim_disposition_of(
    pointer: Mapping[str, Any],
    cache: dict[str, Any],
) -> Any:
    """One live pointer's claim disposition, read at most once per pass."""
    from reckon.crew.node import claim_disposition

    run_id = str(pointer.get("run_id") or "")
    if run_id not in cache:
        cache[run_id] = claim_disposition(pointer)
    return cache[run_id]


def _claim_collision(
    other: Mapping[str, Any],
    ours: Sequence[str],
    *,
    repo: str,
    shared_files: frozenset[str],
    disposition: Callable[[Mapping[str, Any]], Any],
) -> tuple[Any, str] | None:
    """The path ``other`` claims among ``ours``, when its disposition binds."""
    if repo:
        other_repo = str(other.get("repo") or "")
        if other_repo and os.path.normpath(other_repo) != repo:
            return None
    theirs = _declared_scope_paths(other)
    collision = next(
        (
            path
            for path in ours
            if any(
                runs._scopes_overlap(path, claim)
                and not runs._claim_paths_share(shared_files, path, claim)
                for claim in theirs
            )
        ),
        None,
    )
    if collision is None:
        return None
    judged = disposition(other)
    if not judged.binding:
        return None
    return judged, collision


def _refreshed_claim(
    holder: Mapping[str, Any],
    ours: Sequence[str],
    *,
    repo: str,
    shared_files: frozenset[str],
    dispositions: dict[str, Any],
) -> tuple[Any, str, Mapping[str, Any]] | None:
    """Re-read the one pointer that holds a claim, and judge it as of now.

    The live set is read once per pass, and the stage that judges requests
    against it can run for an hour or more, so a holder that landed while the
    pass ran otherwise fences its peers until the next pass begins. One pointer
    read answers whether that claim is still there, at a cost of one file read
    per hold rather than a fresh read of the whole live set.

    ``None`` means the claim no longer holds: its pointer is gone, it no longer
    claims the path, or its own disposition no longer binds. Where the pointer
    is unchanged the pass's cached disposition stands; if the pointer is still
    there and its disposition binds, the request is held exactly as before.
    An unread pointer claims nothing — there is nothing left to read that says
    the path is fenced.
    """
    run_id = str(holder.get("run_id") or "")
    if not run_id:
        return None
    try:
        fresh = runs.read_pointer(run_id)
    except CrewError:
        return None
    if fresh == dict(holder):
        collision = _claim_collision(
            fresh,
            ours,
            repo=repo,
            shared_files=shared_files,
            disposition=lambda written: _claim_disposition_of(written, dispositions),
        )
        return (*collision, fresh) if collision is not None else None
    from reckon.crew.node import claim_disposition

    collision = _claim_collision(
        fresh,
        ours,
        repo=repo,
        shared_files=shared_files,
        disposition=claim_disposition,
    )
    return (*collision, fresh) if collision is not None else None


def _live_claim_hold(
    pointer: Mapping[str, Any],
    live: Sequence[Mapping[str, Any]],
    *,
    dispositions: dict[str, Any],
    shared_files: frozenset[str],
) -> str | None:
    detail = _live_claim_detail(
        pointer, live, dispositions=dispositions, shared_files=shared_files
    )
    return None if detail is None else str(detail["reason"])


def _live_claim_detail(
    pointer: Mapping[str, Any],
    live: Sequence[Mapping[str, Any]],
    *,
    dispositions: dict[str, Any],
    shared_files: frozenset[str],
) -> dict[str, Any] | None:
    """Name a live run whose claim fences one of this request's write paths.

    A launch refused against another run's claim is not cured by waiting: the
    claim releases when its holder settles, so every pass that simply retried it
    spent a whole ``dispatch()`` — project and plan discovery, then the roadmap
    builds — to be told the same thing. Comparing the paths two requests
    declare answers the question in the pass itself, and the slot moves on to
    the next candidate.

    Only a claim whose own disposition binds holds a peer, so a queued, held or
    empty stopped run never holds one. A file the project declares shareable is
    no collision at all, because the launch admits a second claimant there.

    The holder that would fence this request is re-read before the hold is
    recorded. A holder whose pointer the pass read before it landed is exactly
    the long pass's failure: the reading is stale by the time the request is
    judged, so the request is judged again against the pointer's current state
    instead of being held on a claim nobody makes any more.
    """
    ours = _declared_scope_paths(pointer)
    if not ours:
        return None
    here = str(pointer.get("run_id") or "")
    repo = (
        os.path.normpath(str(pointer.get("repo") or "")) if pointer.get("repo") else ""
    )
    for other in live:
        if str(other.get("run_id") or "") == here:
            continue
        held = _claim_collision(
            other,
            ours,
            repo=repo,
            shared_files=shared_files,
            disposition=lambda written: _claim_disposition_of(written, dispositions),
        )
        if held is None:
            continue
        fresh = _refreshed_claim(
            other,
            ours,
            repo=repo,
            shared_files=shared_files,
            dispositions=dispositions,
        )
        if fresh is None:
            continue
        disposition, collision, holder = fresh
        holder_run = str(other.get("run_id") or "unknown")
        exclusive = runs._exclusive_write_pattern(shared_files, collision)
        pattern_reason = f"; exclusive pattern {exclusive!r}" if exclusive else ""
        return {
            "reason": f"run {holder_run!r} holds {collision!r}: {disposition.reason}{pattern_reason}",
            "holder": {
                "run_id": holder_run,
                "node": str((holder.get("node") or {}).get("id") or ""),
                "session": str(holder.get("session") or ""),
                "phase": str(holder.get("phase") or ""),
                "path": collision,
            },
        }
    return None


def recorded_pass_hold(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """Return the last admission hold stored on a queued request."""
    record = pointer.get(RECORDED_HOLD_FIELD)
    return dict(record) if isinstance(record, Mapping) else {}


def _named_runs(record: Mapping[str, Any]) -> list[str]:
    """Return the live runs named by a recorded refusal."""
    holder = record.get("holder")
    named = (
        [str(holder["run_id"])]
        if isinstance(holder, Mapping) and holder.get("run_id")
        else []
    )
    for run_id in record.get("runs") or ():
        if str(run_id) and str(run_id) not in named:
            named.append(str(run_id))
    return named


def _refusal_hold(
    caught: Exception, live: Sequence[Mapping[str, Any]], *, moment: datetime
) -> dict[str, Any] | None:
    """Describe a launch refusal that names the run holding the request."""
    holders = {str(pointer.get("run_id") or ""): pointer for pointer in live}
    if isinstance(caught, ScopeConflict):
        holder_run = str(caught.run_id or "")
        holder = holders.get(holder_run) or {}
        holder_record = {
            "run_id": holder_run,
            "node": str(caught.node_id or ""),
            "session": str(holder.get("session") or ""),
            "phase": str(holder.get("phase") or ""),
            "path": str(caught.claimed_path or ""),
        }
        return {
            "verdict": "refused-scope-conflict",
            "reason": (
                f"the launch was refused on a scope conflict: run {holder_run!r} "
                f"(session {holder_record['session']!r}, phase "
                f"{holder_record['phase']!r}) holds the write path "
                f"{holder_record['path']!r}"
            ),
            "holder": holder_record,
            "recorded_at": moment.isoformat(),
        }
    if isinstance(caught, UnreconciledRuns):
        named = [str(row.get("run_id") or "") for row in caught.runs]
        return {
            "verdict": "refused-unreconciled-runs",
            "reason": (
                f"the launch was refused with {len(named)} unreconciled run(s) "
                f"older than the {caught.grace} grace: {', '.join(named)}"
            ),
            "runs": named,
            "recorded_at": moment.isoformat(),
        }
    return None


def _record_pass_hold(
    pointer: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    *,
    live: Sequence[Mapping[str, Any]],
    moment: datetime,
) -> None:
    """Store a changed holding reason without renewing an unchanged record."""
    if record is None:
        return
    previous = recorded_pass_hold(pointer)
    chosen = dict(record)
    live_ids = {str(candidate.get("run_id") or "") for candidate in live}
    named = _named_runs(previous)
    if (
        chosen.get("verdict") == "held-on-claim"
        and previous.get("verdict") == "refused-scope-conflict"
        and chosen.get("holder") == previous.get("holder")
    ):
        # The current pass found the same binding claim that refused the
        # launch. Keep its first recorded reason and instant.
        chosen = previous
    elif (
        str(chosen.get("verdict") or "") in _RETRY_HOLD_VERDICTS
        and previous.get("verdict") == "refused-unreconciled-runs"
        and named
        and all(run_id in live_ids for run_id in named)
    ):
        # A retry timer cannot settle the named runs. A scope conflict takes
        # this path only after the claim check found no binding collision.
        chosen = previous

    def without_time(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "recorded_at"}

    if without_time(chosen) == without_time(previous):
        return
    chosen["recorded_at"] = moment.isoformat()
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        current[RECORDED_HOLD_FIELD] = chosen
        current["reason"] = str(chosen.get("reason") or "")
        return current

    try:
        runs._mutate_pointer(run_id, stamp)
    except CrewError:
        return


def _is_until_changed_failure(failure: Any) -> bool:
    """Whether a recorded failure is cured by a change, not by waiting.

    A node too large for its lane is cured by splitting it or by a wider lane; a
    node outside its contract is cured by repairing the plan or the brief. Both
    change something the guard fingerprints, so the retry is keyed to that change
    and never to a timer. The capacity refusal names the same condition in its
    opening words, but it is cured by an occupying run finishing, so it is
    excluded and left on the back-off.
    """
    text = _failure_text(failure)
    if "CompetenceLimit:" in text:
        return True
    if _NODE_CONTRACT_REFUSAL not in text:
        return False
    return _CAPACITY_REFUSAL_REMEDY not in text


def _review_record_refusal_subject(pointer: Mapping[str, Any]) -> str:
    """The reviewed run a review's own refused record path names, or empty.

    A run review delivers its record inside the reviewed run's
    ``review-records`` directory, so a declared write path there names the run
    the review is about even when the review's node id no longer resolves one —
    and it is exactly that unresolvable id that leaves the path outside every
    writable root once the reviewed run has left the fleet. The recorded
    refusal must name that same path: a review refused for any other reason is
    not this pass's to retire.

    Both halves are read through the review module's own rules rather than
    repeated here: the run directory comes from the declarations, and the
    landing of each declaration is judged by the same location rule the
    dispatch fence grants a record through, so a change to what counts as a
    review's record path reaches this retirement too.
    """
    from reckon.crew import recovery_review_delivery

    node = pointer.get("node") or {}
    if str(node.get("role") or "") != REVIEW_ROLE:
        return ""
    text = _failure_text(pointer.get("failure"))
    if _NODE_CONTRACT_REFUSAL not in text:
        return ""
    run_directory = recovery_review_delivery._declared_record_directory(pointer)
    if run_directory is None:
        return ""
    for declared in node.get("write_paths") or ():
        if str(declared) not in text:
            continue
        if (
            recovery_review_delivery._delivered_record_location(
                str(declared), run_directory
            )
            is None
        ):
            continue
        return run_directory.name
    return ""


def _landed_review_subject(
    pointer: Mapping[str, Any], *, live: Sequence[Mapping[str, Any]]
) -> str:
    """The reviewed run whose promotion has stranded this review, or empty.

    A review is stranded when three facts hold together: it names a record path
    under a run's ``review-records`` directory that its own refusal reports
    unreachable, that run has no live pointer, and that run's record is
    promoted. A review of a run still in the fleet is admitted as it always
    was, and a review that has never been refused is left alone.
    """
    subject = _review_record_refusal_subject(pointer)
    if not subject:
        return ""
    if any(str(pointer_row.get("run_id") or "") == subject for pointer_row in live):
        return ""
    project = str(pointer.get("project") or "") or None
    if not runs._promoted_record_project(subject, project):
        return ""
    return subject


def _review_dispose_commands(project: str, subject: str) -> tuple[str, str]:
    """The two commands that answer a finding kept against a landed run.

    Rendered by the disposition writer itself, so the command a coordinator is
    handed is one that writer accepts. The review never launched, so no finding
    of its own exists: the index is a placeholder, as the node and reason
    placeholders are, and the coordinator fills it from the findings they are
    acting on.
    """
    from reckon.crew import review_dispositions

    return review_dispositions.dispose_commands(project, subject, "--finding <index>")


def _retire_landed_review(
    pointer: Mapping[str, Any],
    subject: str,
    *,
    moment: datetime,
    result: dict[str, Any],
) -> str:
    """Discard a review whose subject landed before it launched.

    The departure is recorded where every discard is read, with the reason
    naming the landed run, and one obligation names that run and how to dispose
    of any finding the review would have carried. The pointer is only recorded
    as retired once the discard has gone through: a discard that refuses leaves
    the request queued and the next pass tries again, rather than reporting a
    retirement that never happened.
    """
    from reckon.crew import recovery_review_dispatch

    run_id = str(pointer.get("run_id") or "")
    project = str(pointer.get("project") or "")
    reason = (
        f"retired without launching: its subject {subject!r} was promoted "
        f"before the review was admitted, so the reviewed run is no longer in "
        f"the fleet and the grant that resolves the review's record directory "
        f"from its node id names no run directory"
    )
    outcome = recovery_review_dispatch._discard_stranded_review(run_id, reason=reason)
    if project:
        fold, exempt = _review_dispose_commands(project, subject)
        remedy = (
            f"dispose any finding it would have carried with `{fold}` or `{exempt}`"
        )
    else:
        remedy = (
            f"dispose any finding it would have carried against run "
            f"{subject!r} with `reckon crew dispose --project <project> "
            f"--run {subject} ...`"
        )
    detail = (
        f"review {run_id!r} was retired without launching: its subject "
        f"{subject!r} landed before the review was admitted; {remedy}"
    )
    result["obligations"].append({"run_id": run_id, "detail": detail})
    if runs.pointer_path(run_id).is_file():
        _record_retirement(pointer, subject=subject, moment=moment, outcome=outcome)
        return f"{subject}: {outcome}"
    return subject


def _record_retirement(
    pointer: Mapping[str, Any], *, subject: str, moment: datetime, outcome: str
) -> None:
    """Record a retirement on a request whose pointer could not be removed.

    A discard that refused or failed leaves the pointer where it was, so the
    retirement is written onto it: the next pass finds the subject promoted
    again and retires it again, and a reader of the request sees why rather
    than only that it is still queued.
    """
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return
    record = {
        "verdict": "retired-landed-review",
        "reason": (
            f"its subject {subject!r} was promoted before the review was "
            f"admitted: {outcome}"
        ),
        "landed_subject": subject,
        "recorded_at": moment.isoformat(),
    }

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        current[RECORDED_HOLD_FIELD] = record
        current["reason"] = record["reason"]
        return current

    try:
        runs._mutate_pointer(run_id, stamp)
    except CrewError:
        return


def _until_changed_hold(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> str | None:
    """Why a request refused by its own size or contract is left alone.

    The retry back-off this replaces was measured from the pass's start and
    capped at ten minutes, so a refusal that waiting can never cure was retried
    on that timer. One such request was attempted 54 times. Nothing it depends
    on has moved, so no attempt can succeed; the first pass that sees the
    request's plan, brief, configuration or code change releases it.
    """
    failure = pointer.get("failure")
    if not failure or not _is_until_changed_failure(failure):
        return None
    guard = pointer.get("failure_guard")
    if guard is None:
        # Nothing was fingerprinted, so there is nothing to compare and no
        # honest basis for holding: the attempt re-records a guard.
        return None
    if guard != _dependency_guard(pointer, config_for):
        return None
    return (
        "last attempt was refused by the node's own size or contract; "
        "not retried until its plan, brief, route or code changes"
    )


def _service_may_launch(destination: str, *, lane: str) -> bool:
    """Whether this admission service is the one that launches ``destination``.

    A service started without a lane admits every route the configuration
    serves. A service bound to one lane launches that lane's requests; a
    destination on any other lane is left queued there for the service that
    owns it rather than launched by a service whose lane the request has left.
    """
    return not lane or destination == lane


def _candidate_route(config: Mapping[str, Any], backend: str) -> dict[str, str]:
    """The lane and model key a destination backend's own configuration names."""
    entry = (config.get("backends") or {}).get(backend)
    if not isinstance(entry, Mapping):
        return {"backend": backend, "lane": "", "model_key": ""}
    return {
        "backend": str(backend),
        "lane": str(entry.get("lane") or backend),
        "model_key": str(entry.get("model_key") or ""),
    }


def _record_queued_on(run_id: str, route: Mapping[str, str], *, reason: str) -> None:
    """Leave a request queued on another lane, naming the lane it now waits on."""

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        current["backend"] = str(route["backend"])
        current["requested_backend"] = str(route["backend"])
        current["lane"] = str(route.get("lane") or "")
        current["model_key"] = str(route.get("model_key") or "")
        current["lane_move"] = {
            "from_backend": str(current.get("lane_move", {}).get("from_backend") or ""),
            "to_backend": str(route["backend"]),
            "reason": reason,
        }
        return current

    runs._mutate_pointer(run_id, stamp)


def _competence_shortfall(refusal: CompetenceLimit) -> str:
    """The refusal's own shortfall, in tokens, for the obligation it raises."""
    verdict = refusal.verdict
    estimate = verdict.get("estimated_tokens")
    window = verdict.get("window_tokens")
    shortfall = verdict.get("shortfall_tokens")
    if estimate is None or window is None:
        return str(refusal)
    return (
        f"context-fit: the node's estimated {estimate} usable input tokens exceed "
        f"every lane's window, widest {window} tokens, a shortfall of "
        f"{shortfall if shortfall is not None else estimate} tokens"
    )


def _route_competence_refusal(
    pointer: Mapping[str, Any],
    refusal: CompetenceLimit,
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    lane: str,
) -> dict[str, Any]:
    """Ask the picker which lane fits a request its own lane refused, and move it.

    The pass does not judge fit itself: the picker's candidates have already run
    the competence verdict per backend under the lifts in force, and the lane is
    chosen by the same rule a dispatch with no router answer uses, so both paths
    resolve one lane from one candidate set. A destination this service launches
    is launched in the same pass; any other leaves the request queued on that
    lane, named in an obligation. Only when no lane fits does the request stay
    where it was, and the obligation then names the shortfall in tokens rather
    than the lane's last allowance.
    """
    from reckon.crew.picker import snapshot as picker_snapshot
    from reckon.crew.picker.types import PickRequest

    node = _launching._node_from_pointer(pointer)
    project = str(pointer.get("project") or "")
    repository = Path(str(pointer.get("repo") or "."))
    config = dict(
        config_for(pointer)
        if config_for is not None
        else _launching._resolve_lane_config(pointer, select_local=False)
    )
    options = pointer.get("dispatch_options") or {}
    request = PickRequest(
        project,
        node,
        comment=str(options.get("comment") or ""),
        session=str(pointer.get("session") or ""),
    )
    refused_backend = str(pointer.get("backend") or "")
    run_id = str(pointer["run_id"])
    chosen = picker_snapshot.widest_fitting_candidate(
        picker_snapshot.candidates(request, config, repository), refused_backend
    )
    if chosen is not None:
        destination = str(chosen.backend)
        route = _candidate_route(config, destination)
        reason = (
            f"routed off {refused_backend or 'its lane'} after a context-fit "
            "refusal at admission"
        )
        if _service_may_launch(destination, lane=lane):
            moved = _launching.move_queued_run(
                run_id, destination, reason=reason, config=config
            )
            return {"record": moved, "queued_on": "", "obligation": ""}
        _record_queued_on(run_id, route, reason=reason)
        return {
            "record": None,
            "queued_on": destination,
            "obligation": (
                f"routed off {refused_backend or 'its lane'} after a context-fit "
                f"refusal and is queued on lane {destination!r}, which this "
                "admission service does not launch"
            ),
        }
    return {
        "record": None,
        "queued_on": "",
        "obligation": _competence_shortfall(refusal),
    }


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
    if _is_plan_visibility_refusal(failure):
        # A commit moves the plan's blob at HEAD and nothing else, so this is
        # the only comparison that sees the commit which cured the refusal.
        stamped = pointer.get("failure_plan_commit")
        if stamped is not None and stamped != _plan_committed_stamp(pointer):
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
    if _is_plan_visibility_refusal(failure):
        record["failure_plan_commit"] = _plan_committed_stamp(record)
    else:
        # A stamp kept from an earlier plan refusal would release this one on a
        # commit that never touched it.
        record.pop("failure_plan_commit", None)
    return record
