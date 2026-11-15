"""Settle independent launches while their admission pass retains its lock."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context

from . import holds as _holds
from . import launching as _launching
from . import service as _service
from . import verdicts as _verdicts
from .shared import (
    Any,
    BudgetHold,
    CompetenceLimit,
    LaneHeld,
    LanePaused,
    Mapping,
    ScopeConflict,
    copy,
    runs,
)

#: How many launches one pass sets up at once. Each launch resolves its plan,
#: claims and ledger inside this process, a few gigabytes while it runs, and the
#: service shares one memory allowance with every worker it has launched. A wide
#: pool trades that allowance for setup speed, and past it the kernel reclaims
#: the allowance by killing workers, so the width is kept to what the setup
#: memory of a pass can cost without reaching them.
LAUNCH_POOL_WIDTH = 4


class LaunchBatch:
    """Bound setup work and settle every started launch before leaving a pass.

    Router allowances are reserved before submission and returned on refusal.
    Results are combined only by the admission thread, and each launch inherits
    the pass's read context. The pool limits simultaneous setup work, independently
    of the router's worker allowance.
    """

    def __init__(self, result, remaining):
        self.result = result
        self.remaining = remaining
        self.width = LAUNCH_POOL_WIDTH
        self.pool = ThreadPoolExecutor(
            max_workers=self.width, thread_name_prefix="admit-launch"
        )
        self.pending = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Joining precedes releasing the admission and service pass locks, even
        # when selection raises, so a code replacement cannot cut a launch short.
        self.pool.shutdown(wait=True)
        for future in list(self.pending):
            self._settle(future)

    def _settle(self, future):
        key = self.pending.pop(future)
        outcome = future.result()
        if not outcome["launched"] and self.remaining[key] is not None:
            self.remaining[key] += 1
        for name, value in outcome.items():
            if isinstance(value, list):
                self.result.setdefault(name, []).extend(value)
            else:
                self.result.setdefault(name, {}).update(value)

    def collect_ready(self):
        for future in list(self.pending):
            if future.done():
                self._settle(future)

    def wait_for_capacity(self):
        if len(self.pending) >= self.width:
            wait(self.pending, return_when=FIRST_COMPLETED)
            self.collect_ready()

    def has_pending(self, key):
        return key in self.pending.values()

    def wait_for_allowance(self, key):
        while self.remaining[key] is not None and self.remaining[key] <= 0:
            pending = [f for f, owner in self.pending.items() if owner == key]
            if not pending:
                break
            wait(pending, return_when=FIRST_COMPLETED)
            self.collect_ready()

    def submit(self, key, pointer, **kwargs):
        if self.remaining[key] is not None:
            self.remaining[key] -= 1
        future = self.pool.submit(
            copy_context().run, _attempt_launch, pointer, **kwargs
        )
        self.pending[future] = key


def _attempt_launch(
    pointer: Mapping[str, Any],
    *,
    allowance,
    allowance_source,
    launcher,
    config_for,
    readiness_cache,
    live,
    moment,
    lane,
    tick,
):
    result = {
        "launched": [],
        "held": [],
        "failed": [],
        "verdicts": [],
        "routed": [],
        "moved": [],
        "obligations": [],
    }
    verdicts = result["verdicts"]
    snapshot = copy.deepcopy(dict(pointer))
    first_report = not pointer.get("failure_reported_at")
    record = None
    failure = "dispatch did not return a launched record"
    caught: Exception | None = None
    competence_refusal: CompetenceLimit | None = None
    hold_only = False
    reported_id = str(pointer["run_id"])
    restore_error = None
    # The launch's own stopwatch, and a step name for each span of it
    # this pass can time, so the verdict row says how long the launch
    # took and which of its steps was slowest.
    launch_started = tick()
    launch_steps: dict[str, float] = {}
    # A handover identifies the admitted attempt while its original
    # pointer remains published and dispatch checks the launch.
    step_at = tick()
    runs.write_handover(pointer, admitted_by=_service._admitting_pass_identity())
    _add_step(launch_steps, "handover", tick() - step_at)
    step_at = tick()
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
        _add_step(launch_steps, "dispatch", tick() - step_at)
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
            reported_id, restore_error = _launching._return_claim_refusal_to_queue(
                snapshot, caught, live=live, moment=_service._now()
            )
        # The launch outcome is settled here: either the run's own
        # pointer names the node, or the request has been written back.
        if record is None and restore_error is None:
            runs.clear_handover(pointer)
    if record is None and competence_refusal is not None and restore_error is None:
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
            return result
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
            if isinstance(caught, (BudgetHold, LaneHeld, LanePaused, ScopeConflict))
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
            result.setdefault("restore_failure_detail", {})[reported_id] = restore_error
        return result
    step_at = tick()
    _launching._stamp_admission(
        str(record["run_id"]), snapshot, admitted_at=_service._utc_now()
    )
    runs.clear_handover(pointer)
    _add_step(launch_steps, "stamp", tick() - step_at)
    slowest = _slowest_step(launch_steps)
    verdicts.append(
        _verdicts._verdict_row(
            pointer,
            allowance=allowance,
            allowance_source=allowance_source,
            verdict="launched",
            launch_seconds=round(max(0.0, tick() - launch_started), 3),
            slowest_step=slowest,
            slowest_step_seconds=launch_steps.get(slowest),
        )
    )
    result["launched"].append(str(record["run_id"]))
    return result


def _add_step(steps: dict[str, float], name: str, seconds: float) -> None:
    """Charge ``seconds`` to one named step of a launch, summing repeat visits.

    A launch can attempt its dispatch twice — the second after a lane hold
    rebinds it — so a step can be entered more than once and its spans are
    added rather than replaced.
    """
    steps[name] = round(steps.get(name, 0.0) + max(0.0, seconds), 3)


def _slowest_step(steps: Mapping[str, float]) -> str:
    """The name of the step a launch spent most of its time in."""
    return max(steps, key=lambda name: steps[name]) if steps else ""
