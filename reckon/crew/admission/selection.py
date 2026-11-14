"""Choose and admit the requests each lane and session can launch."""

from __future__ import annotations

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
    BudgetHold,
    Callable,
    CompetenceLimit,
    CrewError,
    LaneHeld,
    LanePaused,
    Mapping,
    Path,
    ScopeConflict,
    _dispatch_fleet_gate,
    copy,
    datetime,
    nullcontext,
    parse_utc,
    runs,
    time,
)


def admit_once(
    *,
    launcher: Callable[..., int] | None = None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    lane: str = "",
    run_id: str = "",
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
    ``run_id`` selects one queued request under that lock, skipping queue-wide
    release and recovery. Other pointers supply occupancy and claims only.
    """
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
            runs.sweep_handovers(str(pointer.get("run_id") or "") for pointer in live)
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
        readiness_cache: dict[str, Any] = {}
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

        for index, pointer in enumerate(order):
            session = str(pointer["session"])
            allowance_key = (str(pointer.get("backend") or ""), session)
            if allowance_key not in remaining:
                read, source = _holds._session_allowance_read(
                    pointer, config_for=config_for
                )
                remaining[allowance_key] = read
                allowance_sources[allowance_key] = source
            allowance = remaining[allowance_key]
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
                result.setdefault("hold_detail", {})[str(pointer["run_id"])] = gate_hold
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
            until_changed = _holds._until_changed_hold(pointer, config_for=config_for)
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
            )
            if overlap is not None:
                # An earlier-ranked queued request names a path this one also
                # names, so this launch would be refused against a claim that
                # fences nothing while both wait. It is left exactly as it is —
                # queued, with no failure recorded — and the head of the queue,
                # which has no earlier overlap, is never held this way. An
                # earlier request the pass has already held is not a fence: it
                # cannot launch, so holding this one behind it would stop the
                # lane on a request that is waiting elsewhere.
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
            snapshot = copy.deepcopy(dict(pointer))
            first_report = not pointer.get("failure_reported_at")
            record = None
            failure = "dispatch did not return a launched record"
            caught: Exception | None = None
            competence_refusal: CompetenceLimit | None = None
            hold_only = False
            reported_id = str(pointer["run_id"])
            restore_error = None
            # A handover identifies the admitted attempt while its original
            # pointer remains published and dispatch checks the launch.
            runs.write_handover(
                pointer, admitted_by=_service._admitting_pass_identity()
            )
            try:
                candidate = _launching._launch_queued(
                    pointer,
                    launcher=launcher,
                    config_for=config_for,
                    readiness_cache=readiness_cache,
                )
                record = _launching._require_launched_record(candidate)
            except BudgetHold as exc:
                rebound = _holds._local_fallback(pointer, exc, config_for=config_for)
                if rebound is None:
                    _holds._record_metered_hold(reported_id, exc)
                    caught = exc
                    hold_only = True
                else:
                    snapshot = copy.deepcopy(rebound)
                    try:
                        candidate = _launching._launch_queued(
                            rebound,
                            launcher=launcher,
                            config_for=config_for,
                            backend_override=str(rebound["backend"]),
                            readiness_cache=readiness_cache,
                        )
                        record = _launching._require_launched_record(candidate)
                    except ScopeConflict as rebind_refusal:
                        # The rebind launch publishes the rebound request's
                        # pointer before it checks claims, so a claim taken in
                        # that window refuses it in phase ``starting`` exactly
                        # as the first launch would have been. Same hold, same
                        # restore: the rebound request goes back to the queue
                        # rather than being left starting behind a failure.
                        caught = rebind_refusal
                        hold_only = True
                        failure = f"{type(rebind_refusal).__name__}: {rebind_refusal}"
                    except (BudgetHold, LaneHeld, LanePaused) as fallback_hold:
                        caught = fallback_hold
                        hold_only = True
                        if isinstance(fallback_hold, BudgetHold):
                            _holds._record_metered_hold(reported_id, fallback_hold)
                    except Exception as fallback_error:  # noqa: BLE001 - restore the waiting request
                        caught = fallback_error
                        failure = f"{type(fallback_error).__name__}: {fallback_error}"
            except ScopeConflict as exc:
                # A claim can be taken in the window between the pre-launch
                # check and the launch, and the launch then refuses the request
                # after publishing its pointer in phase ``starting``. That is a
                # hold, not a failed attempt: it names the claim that must
                # settle, and the request keeps its place in the queue until it
                # does. Recording it as a failure would leave the starting
                # pointer behind, where no reaper reads it as a launch to clean
                # up and no pass re-queues it.
                caught = exc
                hold_only = True
                failure = f"{type(exc).__name__}: {exc}"
            except CompetenceLimit as exc:
                # A context-fit refusal is a fact about the lane that was
                # asked, not about the request alone: another lane's window may
                # hold it. The pass falls through to the picker below, after the
                # request has been written back as one queued pointer.
                caught = exc
                competence_refusal = exc
                failure = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - every dispatch failure keeps its request
                caught = exc
                failure = f"{type(exc).__name__}: {exc}"
            finally:
                if record is None and not hold_only:
                    # The back-off is measured from the moment the attempt
                    # failed, which is now, not from the pass's start: a pass
                    # that spent minutes on other requests would otherwise stamp
                    # a retry instant already past, and the next pass would
                    # attempt the same doomed launch again immediately.
                    reported_id, restore_error = _launching._record_queued_failure(
                        snapshot,
                        failure,
                        moment=_service._now(),
                        guard=_holds._dependency_guard(pointer, config_for),
                    )
                elif record is None and isinstance(caught, ScopeConflict):
                    reported_id, restore_error = (
                        _launching._return_claim_refusal_to_queue(
                            snapshot, caught, live=live, moment=_service._now()
                        )
                    )
                # The launch outcome is settled here: either the run's own
                # pointer names the node, or the request has been written back.
                runs.clear_handover(pointer)
            if (
                record is None
                and competence_refusal is not None
                and restore_error is None
            ):
                route = _holds._route_competence_refusal(
                    pointer,
                    competence_refusal,
                    config_for=config_for,
                    lane=lane,
                )
                record = route.get("record")
                if record is not None:
                    # The move launched the request on the lane that fits, in
                    # this pass and in place of the refused one.
                    result["routed"].append(reported_id)
                elif route.get("queued_on"):
                    # Another service owns the lane it now waits on, so the
                    # request keeps its place there and the obligation names
                    # the lane rather than the refusal.
                    result["moved"].append(reported_id)
                    result["obligations"].append(
                        {"run_id": reported_id, "detail": str(route["obligation"])}
                    )
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="moved",
                            detail=f"queued on {route['queued_on']}",
                        )
                    )
                    continue
                else:
                    # No lane's window holds it. The attempt is reported as the
                    # failure it was, and the one obligation names the shortfall
                    # in tokens; the recorded guard is what holds every later
                    # pass until the request itself changes.
                    result["obligations"].append(
                        {"run_id": reported_id, "detail": str(route["obligation"])}
                    )
            if record is None:
                if first_report and not hold_only and restore_error is None:
                    _holds._report_queued_failure(pointer)
                key = (
                    "held"
                    if isinstance(
                        caught, (BudgetHold, LaneHeld, LanePaused, ScopeConflict)
                    )
                    else "failed"
                )
                detail_key = "hold_detail" if key == "held" else "failure_detail"
                if isinstance(caught, ScopeConflict):
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-on-claim",
                            detail=str(caught),
                        )
                    )
                elif caught is not None and isinstance(
                    caught, (BudgetHold, LaneHeld, LanePaused)
                ):
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="held-by-gate",
                            detail=str(caught),
                        )
                    )
                else:
                    verdicts.append(
                        _verdicts._verdict_row(
                            pointer,
                            allowance=allowance,
                            allowance_source=allowance_source,
                            verdict="refused",
                            failure=failure,
                            detail=restore_error or "",
                        )
                    )
                result[key].append(reported_id)
                result.setdefault(detail_key, {})[reported_id] = (
                    str(caught) if caught is not None else failure
                )
                if caught is not None and not isinstance(
                    caught, (BudgetHold, LaneHeld, LanePaused)
                ):
                    _holds._record_pass_hold(
                        pointer,
                        _holds._refusal_hold(caught, live, moment=moment),
                        live=live,
                        moment=moment,
                    )
                if restore_error is not None:
                    result.setdefault("restore_failure_detail", {})[reported_id] = (
                        restore_error
                    )
                continue
            _launching._stamp_admission(
                str(record["run_id"]), snapshot, admitted_at=_service._utc_now()
            )
            verdicts.append(
                _verdicts._verdict_row(
                    pointer,
                    allowance=allowance,
                    allowance_source=allowance_source,
                    verdict="launched",
                )
            )
            result["launched"].append(str(record["run_id"]))
            if allowance is not None:
                remaining[allowance_key] = allowance - 1
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
