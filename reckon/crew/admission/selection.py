"""Choose and admit the requests each lane and session can launch."""

from __future__ import annotations

from collections import deque

from reckon.crew.open_run_index import operational_read

from . import holds as _holds
from . import launching as _launching
from . import ordering as _ordering
from . import readiness as _readiness
from . import release as _release
from . import reviews as _reviews
from . import service as _service
from . import verdicts as _verdicts
from .shared import (
    _LANE_GATE_WAITING_STATES,
    HOLD_VERDICTS,
    LAUNCH_FAILED_PHASE,
    QUEUED_PHASE,
    TERMINAL_VERDICTS,
    Any,
    Callable,
    CrewError,
    Mapping,
    Path,
    _dispatch_fleet_gate,
    datetime,
    nullcontext,
    parse_utc,
    runs,
    time,
)


@operational_read
def admit_once(
    *,
    launcher: Callable[..., int] | None = None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    lane: str = "",
    run_id: str = "",
    now: datetime | None = None,
    lock: bool = True,
    budget_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run one admission pass and report what it launched, held and failed.

    The pass reads the fleet gate, then the queued and live pointers. It ranks
    the queued entries by current lane occupancy, reserves the slots its session
    is granted, and launches independent requests concurrently. A held or paused gate launches nothing.
    A queued pointer whose arrival time cannot be read is left out of the
    ordering and named in a verdict, so it costs its own place rather than
    ending the pass for every other request on the lane.
    ``lock`` serialises the pass against a concurrent drainer so one queued run
    launches once; a caller already holding the admit lock passes ``lock=False``.
    ``run_id`` selects one queued request under that lock, skipping queue-wide
    release and recovery. Other pointers supply occupancy and claims only.
    ``budget_seconds`` bounds scheduling further launches; every started launch
    settles before the pass ends. It defaults to the flight config's
    ``admission.pass_budget_seconds``.
    ``clock`` is the monotonic clock the budget and each launch's duration are
    measured on, defaulting to :func:`time.monotonic`.
    """
    tick = clock if clock is not None else time.monotonic
    moment = now or _service._now()
    gate = _dispatch_fleet_gate()
    result: dict[str, Any] = {
        "gate": gate.get("state"),
        "launched": [],
        "held": [],
        "failed": [],
        "requeued": [],
        "verdicts": [],
        "routed": [],
        "moved": [],
        "retired": [],
        "obligations": [],
    }
    local_gated = gate.get("state") in _LANE_GATE_WAITING_STATES
    if local_gated and not run_id:
        _verdicts._record_gated_verdicts(
            result, gate=gate, lane=lane, config_for=config_for
        )
    from reckon.crew.plan_review import review_read_pass

    with (
        _service._admit_lock() if lock else nullcontext(),
        _readiness.readiness_pass(),
        review_read_pass(),
        _reviews.plan_review_pass(),
    ):
        from . import interrupted

        result["recovered"] = interrupted.recover()
        if run_id:
            selected = runs.read_pointer(run_id)
            if selected.get("phase") != QUEUED_PHASE:
                raise CrewError(
                    f"run {run_id!r} is {selected.get('phase')!r}; "
                    "named admission requires a queued request"
                )
            live = runs.list_live()
            queued = [selected]
        else:
            result["requeued"] = _launching._requeue_admitted_failures(
                runs.list_live(phase=LAUNCH_FAILED_PHASE), verdicts=result["verdicts"]
            )
            live = runs.list_live()
            # A launch past its bound holds a claim no worker backs. Releasing it
            # here, before anything is ordered, keeps the lanes and the review
            # reflex from reading a stranded span as work in flight; the released
            # run is re-read so it costs no place in the ordering this pass.
            result["released_stranded"] = _launching._release_stranded_launches(
                live, verdicts=result["verdicts"]
            )
            if result["released_stranded"]:
                live = runs.list_live()
            # Readiness is re-read here, on every pass: a request waiting on its
            # section's refs moves to ``queued`` the moment the last ref lands and
            # to ``blocked-upstream`` the moment one is closed as abandoned or
            # failed, with no coordinator turn in between. This is what lets a
            # batch queued in one call launch in dependency order.
            waits_started = time.monotonic()
            upstream = _release.advance_waiting_requests(live)
            _verdicts._record_readiness_verdicts(
                result, live=live, lane=lane, config_for=config_for
            )
            result["released_plan_review"] = _reviews._release_plan_review_holds(
                live, config_for=config_for
            )
            result["waits_seconds"] = round(time.monotonic() - waits_started, 3)
            result["promoted_upstream"] = upstream["promoted"]
            result["blocked_upstream"] = upstream["blocked"]
            result["upstream_obligations"] = upstream["obligations"]
            queued = [
                pointer for pointer in live if pointer.get("phase") == QUEUED_PHASE
            ]
        if lane:

            def included(pointer: Mapping[str, Any]) -> bool:
                backend = str(pointer.get("backend") or "")
                if backend == lane:
                    return True
                config = (
                    config_for(pointer)
                    if config_for is not None
                    else _launching._resolve_lane_config(pointer, select_local=False)
                )
                # The installed local drainer also admits every metered route.
                return (
                    lane == str(config.get("local_backend") or "") and backend != lane
                )

            excluded_by_lane = [pointer for pointer in queued if not included(pointer)]
            queued = [pointer for pointer in queued if included(pointer)]
        else:
            excluded_by_lane = []
        disk_hold = _service.node_disk_admission_hold() if queued else None
        if disk_hold is not None:
            reason = str(disk_hold["reason"])
            result["obligations"].append(disk_hold)
            for pointer in queued:
                identifier = str(pointer.get("run_id") or "")
                result["held"].append(identifier)
                _holds._record_pass_hold(
                    pointer, {"verdict": "held-by-gate", "reason": reason},
                    live=live, moment=moment,
                )
                result["verdicts"].append(_verdicts._verdict_row(
                    pointer, allowance=0, allowance_source="node disk reserve",
                    verdict="held-by-gate", detail=reason,
                ))
            result["action"] = "gated"
            return result
        if local_gated and run_id and queued:
            pointer = queued[0]
            config = (
                config_for(pointer)
                if config_for is not None
                else _launching._resolve_lane_config(pointer, select_local=False)
            )
            if str(pointer.get("backend") or "") == str(
                config.get("local_backend") or ""
            ):
                result["held"].append(run_id)
                result["verdicts"].append(
                    _verdicts._verdict_row(
                        pointer,
                        allowance=None,
                        allowance_source="fleet gate",
                        verdict="held-by-gate",
                        detail=str(gate.get("detail") or gate.get("state")),
                    )
                )
                result["action"] = "gated"
                return result
        if local_gated:
            queued = [
                pointer
                for pointer in queued
                if str(pointer.get("backend") or "")
                != str(
                    (
                        config_for(pointer)
                        if config_for is not None
                        else _launching._resolve_lane_config(
                            pointer, select_local=False
                        )
                    ).get("local_backend")
                    or ""
                )
            ]
        # A request this pass's lane filter leaves out is otherwise invisible:
        # no row, no hold and no attempt, so a coordinator reading the request
        # cannot tell it apart from one nothing has looked at. The attempt is
        # recorded here, naming the lane the request waits on, so a queued
        # request on any lane carries the reason no pass has launched it.
        for pointer in excluded_by_lane:
            other = str(pointer.get("backend") or "")
            record = {
                "verdict": "held-on-other-lane",
                "reason": (
                    f"the request is queued on lane {other or 'the default lane'!r} "
                    f"and this drainer serves {lane!r}, so no pass it runs will "
                    "launch it; a drainer for that lane is what admits it"
                ),
                "recorded_at": moment.isoformat(),
            }
            _holds._record_pass_hold(pointer, record, live=live, moment=moment)
            result["verdicts"].append(
                _verdicts._verdict_row(
                    pointer,
                    allowance=None,
                    allowance_source="this drainer serves another lane",
                    verdict="held-on-other-lane",
                    detail=record["reason"],
                )
            )
            result["held"].append(str(pointer.get("run_id") or ""))
            result.setdefault("hold_detail", {})[str(pointer.get("run_id") or "")] = (
                record["reason"]
            )
            result["excluded_by_lane"] = [
                *result.get("excluded_by_lane", ()),
                str(pointer.get("run_id") or ""),
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
        order = (
            placeable
            if run_id
            else _ordering.fair_share_admission_order(
                placeable, live, now=moment, config_for=config_for
            )
        )
        remaining: dict[tuple[str, str], float | None] = {}
        allowance_sources: dict[tuple[str, str], str] = {}
        # The router's own verdict for each session read this pass, the sessions
        # whose full reading has already been waited on, and how many launches
        # each session has already been given from this pass's grant. All three
        # are per pass: a verdict describes the reading it came with, a session
        # is waited on once per pass however many requests it is holding, and a
        # session that has spent part of its grant is not read as full -- the
        # zero is this pass's own arithmetic, not the lane's reading.
        router_verdicts: dict[tuple[str, str], str] = {}
        room_waits: set[tuple[str, str]] = set()
        launches_by_key: dict[tuple[str, str], int] = {}
        verdicts: list[dict[str, Any]] = result["verdicts"]
        for pointer in unplaceable:
            allowance, source = _holds._session_allowance_read(
                pointer, config_for=config_for
            )
            _holds._record_pass_hold(
                pointer,
                {
                    "verdict": "unplaced",
                    "reason": "the request carries no readable queued_at, so the lane cannot place it",
                },
                live=live,
                moment=moment,
            )
            verdicts.append(
                _verdicts._verdict_row(
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
        # One claim disposition per live pointer per pass, and one shareable
        # file list per project, so the pre-dispatch claim check below costs a
        # path comparison rather than a git read of every live run.
        claim_dispositions: dict[str, Any] = {}
        shared_files: dict[tuple[str, str], frozenset[str]] = {}
        # One readiness cache for the whole pass, handed to every launch's
        # dispatch. A launch derives a project's roadmap to judge its waits, and
        # the next launch in the same project reads what this one already
        # derived instead of rebuilding it: admission throughput, not the lane,
        # is what caps how full the lane gets otherwise.
        readiness_cache: dict[str, Any] = _readiness.ReadinessCache()
        result["admissible_age_seconds"] = _verdicts._admissible_age_seconds(
            order, moment=moment, gated=local_gated, config_for=config_for
        )

        def record_hold(
            pointer: Mapping[str, Any], verdict: str, reason: str, **extra: Any
        ) -> None:
            _holds._record_pass_hold(
                pointer,
                {"verdict": verdict, "reason": reason, **extra},
                live=live,
                moment=moment,
            )

        # The deadline bounds starting additional launches. In-flight work is
        # settled under the pass lock; requests left by the deadline keep their
        # place for the next pass.
        budget = (
            budget_seconds
            if budget_seconds is not None
            else _launch_budget_from_flight()
        )
        deadline = tick() + budget
        budget_spent = False

        # Imported here: attempts imports launching, service and verdicts at its
        # top, and that chain loads this module, so a module-level import is a
        # cycle that leaves every fresh reckon process unable to start.
        from .attempts import LaunchBatch

        with LaunchBatch(result, remaining) as launches:
            candidates = deque(enumerate(order))
            deferred = []
            filling = True
            while candidates or deferred:
                if not candidates:
                    # Consider every session's free slots before waiting on a
                    # reservation another launch may return after a refusal.
                    candidates.extend(deferred)
                    deferred.clear()
                    filling = False
                index, pointer = candidates.popleft()
                launches.collect_ready()
                launches.wait_for_capacity()
                if tick() >= deadline:
                    budget_spent = True
                    break
                session = str(pointer["session"])
                allowance_key = (str(pointer.get("backend") or ""), session)
                if allowance_key not in remaining:
                    read, source, verdict = _holds._session_room_read(
                        pointer, config_for=config_for
                    )
                    remaining[allowance_key] = read
                    allowance_sources[allowance_key] = source
                    router_verdicts[allowance_key] = verdict
                allowance = remaining[allowance_key]
                if allowance is not None and allowance <= 0:
                    if filling:
                        # A grant that is exhausted here may be the whole of what
                        # holds this session, and it may clear before this pass
                        # ends. Every candidate held on it is deferred to the
                        # second sweep, where one bounded wait per session reads
                        # the router again; holding them here would end the pass
                        # on a reading of one instant.
                        deferred.append((index, pointer))
                        continue
                    launches.wait_for_allowance(allowance_key)
                    allowance = remaining[allowance_key]
                    if allowance is not None and allowance <= 0:
                        # Only a grant this pass has not spent is read as the
                        # lane's reading of full. A zero this pass's own launches
                        # produced is our arithmetic on a figure the lane already
                        # published, and re-reading it would grant the session a
                        # second helping of the same slots.
                        room = None
                        if not launches_by_key.get(allowance_key):
                            room = _wait_for_lane_room(
                                pointer,
                                config_for=config_for,
                                tick=tick,
                                deadline=deadline,
                                waited=room_waits,
                                key=allowance_key,
                                router_verdict=router_verdicts.get(allowance_key, ""),
                            )
                        if room is not None:
                            allowance, room_source = room
                            remaining[allowance_key] = allowance
                            allowance_sources[allowance_key] = room_source
                    if tick() >= deadline:
                        budget_spent = True
                        break
                allowance_source = allowance_sources[allowance_key]
                landed_subject = _holds._landed_review_subject(pointer, live=live)
                if landed_subject:
                    # A review whose subject landed before it launched can never be
                    # admitted, so no lane state — a congested gate included — may
                    # keep it queued. It is retired here rather than held, before
                    # any hold this pass could record for it.
                    result["retired"].append(str(pointer["run_id"]))
                    detail = _holds._retire_landed_review(
                        pointer, landed_subject, moment=moment, result=result
                    )
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="retired-landed-review",
                            detail=detail,
                        )
                    )
                    continue
                if allowance is not None and allowance <= 0:
                    result["held"].append(str(pointer["run_id"]))
                    record_hold(
                        pointer,
                        "held-for-allowance",
                        f"the lane's router grants session {session!r} no extra "
                        f"worker slot ({allowance_source})",
                    )
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-for-allowance",
                        )
                    )
                    continue
                gate_hold = _holds._lane_gate_hold(pointer, config_for=config_for)
                if gate_hold is not None:
                    # The gate would refuse the launch the release below feeds into,
                    # so the request is left where it is: releasing it would emit a
                    # withdrawal and a re-queue for a state that never moved. The
                    # session-allowance hold above is the same question's other half.
                    result["held"].append(str(pointer["run_id"]))
                    result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                        gate_hold
                    )
                    record_hold(pointer, "held-by-gate", str(gate_hold))
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-by-gate",
                            detail=gate_hold,
                        )
                    )
                    continue
                shared_key = (
                    str(pointer.get("project") or ""),
                    str(pointer.get("repo") or ""),
                )
                if shared_key not in shared_files:
                    shared_files[shared_key] = (
                        runs._shared_write_paths(shared_key[0], Path(shared_key[1]))
                        if all(shared_key)
                        else frozenset()
                    )
                claim_detail = _holds._live_claim_detail(
                    pointer,
                    live,
                    dispositions=claim_dispositions,
                    shared_files=shared_files[shared_key],
                )
                if claim_detail is not None:
                    claim_hold = str(claim_detail["reason"])
                    # A live run's claim on a path this request writes is not cured
                    # by waiting out a back-off: the attempt could only be refused
                    # again, at the cost of a whole dispatch. The request keeps its
                    # place, and the pass admits the next candidate.
                    result["held"].append(str(pointer["run_id"]))
                    result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                        claim_hold
                    )
                    record_hold(
                        pointer,
                        "held-on-claim",
                        claim_hold,
                        holder=claim_detail["holder"],
                    )
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-on-claim",
                            detail=claim_hold,
                        )
                    )
                    continue
                until_changed = _holds._until_changed_hold(
                    pointer, config_for=config_for
                )
                if until_changed is not None:
                    result["held"].append(str(pointer["run_id"]))
                    result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                        until_changed
                    )
                    record_hold(pointer, "held-until-changed", str(until_changed))
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-until-changed",
                            detail=until_changed,
                        )
                    )
                    continue
                backoff_hold = _holds._retry_backoff_hold(
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
                    record_hold(pointer, "held-on-retry-backoff", str(backoff_hold))
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-on-retry-backoff",
                            detail=backoff_hold,
                            retry_at=str(pointer.get("retry_at") or ""),
                        )
                    )
                    continue
                overlap = _holds._earlier_overlapping_request(
                    pointer,
                    order[:index],
                    moment=moment,
                    claims=claims,
                    fenced=_holds._non_binding_holds(verdicts),
                    shared=shared_files[shared_key],
                )
                if overlap is not None:
                    # An earlier-ranked queued request names a path a merge cannot
                    # reconcile — a plan document, generated state, a cumulative
                    # evidence record, a figure directory or a dependency lock —
                    # and this one names it too, so this launch would be refused
                    # against a claim that fences nothing while both wait. Source
                    # paths are not a reason to wait: the worktrees isolate the
                    # edits and the merge reconciles them. It is left exactly as it
                    # is — queued, with no failure recorded — and the head of the
                    # queue, which has no earlier overlap, is never held this way.
                    # An earlier request the pass has already held is not a fence:
                    # it cannot launch, so holding this one behind it would stop
                    # the lane on a request that is waiting elsewhere.
                    result["held"].append(str(pointer["run_id"]))
                    result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                        f"held behind earlier queued request "
                        f"{str(overlap.get('run_id') or 'unknown')!r} over a shared path"
                    )
                    record_hold(
                        pointer,
                        "held-behind-earlier-request",
                        f"an earlier ranked request "
                        f"{str(overlap.get('run_id') or 'unknown')!r} names a write "
                        "path this request also names, so it goes first",
                    )
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-behind-earlier-request",
                            detail=str(overlap.get("run_id") or ""),
                        )
                    )
                    continue
                review_owed = _reviews._plan_review_owed(pointer, config_for=config_for)
                if review_owed is not None and _reviews._hold_for_plan_review(
                    pointer, review_owed
                ):
                    # The plan's own units moved under the request since it was
                    # queued. Held, not failed: the request keeps its place, its
                    # waits and its stored brief, and the pass that finds a stored
                    # review covering those units releases it without a
                    # coordinator turn.
                    result["held"].append(str(pointer["run_id"]))
                    result.setdefault("hold_detail", {})[str(pointer["run_id"])] = (
                        review_owed
                    )
                    record_hold(pointer, "held-on-plan-review", str(review_owed))
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-on-plan-review",
                            detail=review_owed,
                        )
                    )
                    continue
                reread_refusal = _launching._launch_reread_refusal(pointer)
                if reread_refusal:
                    # The request moved after this pass read it as queued, so it is
                    # left exactly as it now stands rather than launched from a
                    # reading that has gone stale. Its wait is not a slot hold, so
                    # the stall record names it for a coordinator instead.
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="refused-before-launch",
                            detail=reread_refusal,
                        )
                    )
                    continue
                launches_by_key[allowance_key] = (
                    launches_by_key.get(allowance_key, 0) + 1
                )
                launches.submit(
                    allowance_key,
                    pointer,
                    allowance=allowance,
                    allowance_source=allowance_source,
                    launcher=launcher,
                    config_for=config_for,
                    readiness_cache=readiness_cache,
                    live=live,
                    moment=moment,
                    lane=lane,
                    tick=tick,
                )
        ranks = {str(pointer["run_id"]): index for index, pointer in enumerate(order)}
        result["launched"].sort(key=lambda identity: ranks.get(identity, len(order)))
        verdicts.sort(key=lambda row: ranks.get(str(row["run_id"]), len(order)))
        result["launch_budget_seconds"] = budget
        result["launch_budget_exhausted"] = budget_spent
        # What each role got from this pass and the oldest it left waiting, so a
        # reader can see whether a backlog is draining or the other role is
        # taking the seats it needs.
        result["role_cadence"] = _ordering.role_cadence(
            order, launched=result["launched"], now=moment
        )
        # The queue's own liveness, recorded after the pass has decided what to
        # do with each request: a request this pass held on purpose is not
        # stalled, because the hold names the reason it waits. What remains is a
        # request the lane could have advanced and did not.
        held_this_pass = {
            str(row.get("run_id") or "")
            for row in result["verdicts"]
            if str(row.get("verdict") or "") in HOLD_VERDICTS | TERMINAL_VERDICTS
        }
        held_this_pass.update(result.get("restore_failure_detail", {}))
        result["stalls"] = _verdicts._record_stalls(
            order,
            moment=moment,
            gated=local_gated,
            live=live,
            config_for=config_for,
            claim_dispositions=claim_dispositions,
            shared_files=shared_files,
            held=held_this_pass,
            result=result,
        )
        result["action"] = (
            "admitted" if result["launched"] else "gated" if local_gated else "idle"
        )
        # Every row a pass records carries the queue's age beside its own
        # verdict, so a row that says a request was held also says how old the
        # queue it was held in was. The figure is the pass's, not the row's, and
        # is set here once rather than threaded through every verdict site.
        admissible_age = result.get("admissible_age_seconds")
        for row in result["verdicts"]:
            row.setdefault("admissible_age_seconds", admissible_age)
        if not local_gated:
            _record_admission_positions(
                order, verdicts, moment=moment, ordered=not bool(run_id)
            )
        return result


# How long one pass waits for a lane that granted no worker slot to publish room
# again, and the cadence it re-reads on. A pass used to end on the reading it
# took, so a full reading held every queued request until the next pass -- 45 to
# 185 seconds on the fleet -- while the lane's own grants cleared in seconds. The
# wait is bounded because a full reading that persists is a lane that really is
# saturated, and the pass must still return to do the rest of its work.
LANE_ROOM_WAIT_SECONDS = 30.0
LANE_ROOM_POLL_SECONDS = 1.0


def _wait_for_lane_room(
    pointer: Mapping[str, Any],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    tick: Callable[[], float],
    deadline: float,
    waited: set[tuple[str, str]],
    key: tuple[str, str],
    router_verdict: str,
) -> tuple[float | None, str] | None:
    """Wait, bounded, for a lane that granted no slot to publish room again.

    Returns the allowance the later reading published together with its source,
    or ``None`` when the lane is still full when the wait ends, the session was
    already waited on this pass, or the lane is paused. Three things keep this
    one wait per session per pass rather than a poll loop that ends with the
    pass:

    * a session already waited on this pass is not waited on again, so a queue
      of twelve requests costs one wait, not twelve;
    * the wait never outlasts the pass's own launch deadline, so a lane cannot
      hold a pass open past the budget the pass was given;
    * a paused lane is not waited on at all. A full reading is an instant that
      clears as the requests the router already holds complete, while a pause
      is the lane stating it takes no work, and a later reading of the same
      pause is not worth a wait. A re-read that comes back paused ends the wait
      for the same reason.

    The read is a fresh one. The figure this pass cached is the reading being
    waited out, so reusing it would answer the fresh question with the stale
    instant, and no wait would ever end for a lane that reopened.

    The wait pauses on the service's own pause seam, so a caller driving this
    pass with a stub pause keeps it fast without changing what the pass
    decides.
    """
    if router_verdict == "paused" or key in waited:
        return None
    seconds = min(LANE_ROOM_WAIT_SECONDS, deadline - tick())
    if seconds <= 0:
        return None
    waited.add(key)
    # The config is resolved once here rather than per read: a lane document
    # sitting on a shared filesystem is re-read every poll, while the mounts and
    # lane declarations behind the config do not move inside one wait.
    config = (
        config_for(pointer)
        if config_for is not None
        else _launching._resolve_lane_config(pointer)
    )

    def frozen(_pointer: Mapping[str, Any]) -> Mapping[str, Any]:
        return config

    for _poll in range(max(1, int(seconds // LANE_ROOM_POLL_SECONDS))):
        _service._pause(LANE_ROOM_POLL_SECONDS)
        allowance, source, verdict = _holds._session_room_read(
            pointer, config_for=frozen
        )
        if verdict == "paused":
            return None
        if allowance is None or allowance > 0:
            return allowance, source
    return None


def _launch_budget_from_flight() -> float:
    """The seconds one admission pass may spend launching, from flight.

    Read once per pass rather than per launch, so a pass's deadline is one
    figure the whole loop is charged against.
    """
    from reckon import flight

    return flight.admission_pass_budget_seconds(flight.resolve().config)


def _record_admission_positions(
    order: list[Mapping[str, Any]],
    verdicts: list[dict[str, Any]],
    *,
    moment: datetime,
    ordered: bool,
) -> None:
    """Persist this pass's order and verdict without reviving a removed pointer.

    The rank key starts with the computed position: fair-share and roadmap
    ranking have already been applied. Arrival time and identity bind that key
    to this queue snapshot without deriving a second ordering for the reader.
    A named admission makes no queue-wide order and keeps the stored position.
    An unchanged verdict keeps its receipt time, so repeatedly observing the
    same hold leaves the pointer unchanged. Holding reasons remain in the
    hold record rather than being duplicated in the attempt receipt.
    """
    by_run = {row["run_id"]: row for row in verdicts}
    for position, pointer in enumerate(order, 1):
        run_id = str(pointer["run_id"])
        verdict = by_run.get(run_id)
        if verdict is None or verdict["verdict"] == "launched":
            continue

        attempt = {key: verdict[key] for key in ("run_id", "verdict")}
        updates: dict[str, Any] = {}
        if ordered:
            updates["queue_position"] = position
            updates["rank_key"] = [position, pointer["queued_at"], run_id]

        def stamp(
            current: dict[str, Any], updates=updates, pointer=pointer
        ) -> dict[str, Any]:
            _launching._require_live_writeback(pointer)
            if current.get("phase") == QUEUED_PHASE:
                current.update(updates)
            return current

        try:
            current = runs.read_pointer(run_id)
            previous = current.get("last_admission") or {}
            same_attempt = {
                key: value for key, value in previous.items() if key != "recorded_at"
            } == attempt
            if same_attempt and all(
                current.get(key) == value for key, value in updates.items()
            ):
                continue
            updates["last_admission"] = {
                **attempt,
                "recorded_at": moment.isoformat(),
            }
            runs._mutate_pointer(run_id, stamp)
        except CrewError:
            # A promoted or discarded request no longer belongs to this pass.
            continue
        except OSError as exc:
            verdict["queue_record_error"] = str(exc)
