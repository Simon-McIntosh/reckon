# ruff: noqa: I001, UP035
from __future__ import annotations
import hashlib
import json
import os
import shutil
import socket
import threading
from collections import (
    defaultdict,
)
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
)
from reckon import (
    _backends,
    flight,
    ledger,
)
from reckon._timestamps import (
    parse_iso,
    parse_utc,
)
from reckon.crew import (
    budget_group,
)
from reckon.crew import (
    lane_document as _lane_document,
)
from reckon.crew import (
    summary,
)
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    BudgetHold,
    CrewError,
    TaskNode,
    parse_duration,
    placement_query_undeclared,
    placement_requirement_node_local,
    placement_requirement_unmet,
)
from reckon.crew.refusals import (
    format_refusal,
)
from reckon.crew.reserve import (
    admit_windows as reserve_admit_windows,
)
from reckon.crew.review import (
    review_store_root,
)
from reckon.crew.routing import (
    resolve_role_override,
)
from reckon.crew.runs import (
    _utc_now,
    crew_home,
    delivery_roots,
    list_live,
    reports_dir,
)



def _actionable_budget_hold(
    verdict: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> BudgetHold:
    """Name when one backend's hold lifts and what refreshes its evidence."""
    from reckon import budget as budget_module

    hold = dict(verdict)
    backend = str(hold.get("backend") or "unknown")
    state = hold.get("state")
    state = state if isinstance(state, Mapping) else {}
    resets_at = state.get("resets_at")
    if resets_at:
        timing = f"the stated reset at {resets_at} lifts this hold"
    else:
        bound = float(
            budget_module.policy(config).get(
                "evidence_shelf_life_minutes",
                budget_module.DEFAULT_SHELF_LIFE_MINUTES,
            )
        )
        stamp = state.get("observed_at")
        observed = parse_utc(str(stamp))
        if observed is None:
            timing = (
                f"the evidence age is unknown against the {bound:g} minute "
                "shelf-life bound because its refusal carries no readable time"
            )
        elif bound <= 0:
            timing = (
                f"the evidence is dated {stamp}, but the {bound:g} minute "
                "shelf-life bound disables ageing"
            )
        else:
            moment = parse_utc(_utc_now())
            assert moment is not None, "the repository clock is not ISO-8601"
            age_minutes = max(0.0, (moment - observed).total_seconds() / 60.0)
            lifts_at = (observed + timedelta(minutes=bound)).astimezone(UTC)
            lift_stamp = lifts_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            timing = (
                f"the evidence is {age_minutes:.1f} minutes old against the "
                f"{bound:g} minute shelf-life bound, and ageing lifts this hold "
                f"at {lift_stamp}"
            )
    refresh = f"a served turn on backend {backend!r} refreshes this evidence"
    reason = str(hold.get("reason") or "budget evidence holds the lane")
    hold["reason"] = f"{reason}; {timing}; {refresh}"
    return BudgetHold(hold)


def _live_runs_on_backend(
    backend_name: str, *, exclude_run_ids: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Return non-terminal live pointers claiming a backend, newest run id last.

    ``exclude_run_ids`` drops runs by identity. A dispatch that has already
    published its own claim counts the lane's other occupants, never itself:
    its reservation carries no worker yet, so counting it would refuse a
    dispatch that fits under the ceiling by exactly one.
    """
    excluded = set(exclude_run_ids)
    return [
        pointer
        for pointer in list_live()
        if str(pointer.get("backend") or "") == backend_name
        and str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        and str(pointer.get("run_id") or "") not in excluded
    ]


def _live_runs_across_backends(
    *, exclude_run_ids: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Non-terminal live pointers on every backend, newest run id last.

    The reservation roster is not a backend's: one allocation admits every
    placed worker of the host as a step, whichever backend dispatched it, so the
    population the roster is counted over is taken across backends. The lane
    bound beside it stays one backend's, because a served lane is consumed by
    every caller that sends it a request, placed or not.
    """
    excluded = set(exclude_run_ids or ())
    return [
        pointer
        for pointer in list_live()
        if str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        and str(pointer.get("run_id") or "") not in excluded
    ]


def _refuse_over_reservation_roster(
    backend: Mapping[str, Any],
    occupying: list[dict[str, Any]],
    project: str | None = None,
) -> None:
    """Refuse a dispatch past the placement reservation's roster cap.

    The cap is the roster's alone: under ``--overlap`` the scheduler enforces
    nothing inside the allocation, so the only thing standing between the
    reservation and an oversubscribed node is this check. It applies to a
    backend whose workers are placed into the reservation and only while a
    reservation is actually held — an unplaced backend has no roster of ours,
    and a host with no reservation has nothing to oversubscribe.

    The record is one shared allocation's, and the count is the fleet's. One
    reservation admits every project's and every backend's placed workers, so
    every run actually placed inside it occupies the same roster whichever
    project or backend dispatched it, and a run whose record names no placement
    is unbounded by it. The population handed in is the host-wide live set and
    the seat selection applied here is the same one the hold's reach statement
    counts with, so the projects the reach names are exactly the runs this guard
    counts. A placed run holds its seat only while its worker can still hold
    memory — so a finished-but-unpromoted run, a blocked run awaiting resume,
    and a run waiting on an external condition with no live process all hold
    none. The lane bound above this counts one backend's population and should,
    because a served lane is consumed by every caller that sends it a request,
    placed or not; an allocation is consumed only by the workers running inside
    it as steps, wherever they came from.
    """
    from reckon import flight
    from reckon.crew import placement as placement_module

    if flight.placement_for(backend) is None:
        return
    if not placement_module.read_reservation(project):
        return
    occupants = placement_module.roster_occupants(occupying)
    refusal = placement_module.reservation_roster_refusal(len(occupants))
    if refusal is None:
        return
    # The seats, not the population handed in: with the population host-wide a
    # reader would otherwise be shown every live run on the machine, most of
    # which are not inside the allocation at all.
    occupying_ids = [str(pointer.get("run_id") or "unknown") for pointer in occupants]
    raise CrewError(f"{refusal} Occupying runs: {', '.join(occupying_ids) or 'none'}.")


def _refuse_over_concurrency_ceiling(
    backend_name: str,
    backend: Mapping[str, Any],
    project: str | None = None,
    *,
    exclude_run_ids: Iterable[str] = (),
) -> None:
    """Refuse a dispatch that would exceed whichever resource bound is binding.

    A physical resource that is already spent must not be asked to carry one
    more worker: the harness retry budget is fixed and reckon passes no retry
    configuration, so once an overcommitted resource refuses long enough a 429
    turns from a pause at the protocol into a dead print-mode worker — measured,
    a sixth concurrent worker on the local lane killed two already-running runs
    after ten 429 retries. Adding work destroyed work, so the only reliable
    remedy is not to create the overload.

    Which resource bounds the lane is read rather than assumed. The cores a
    placement's reservation admits and the login memory slice the coordinator
    still lives inside are the candidates, and the refusal names the one that
    ran out with its measured value. The retired roster key is still carried by
    the bound model so a reader can find it, but it never binds. The check
    happens before any worktree or worker exists and never touches a run
    already in flight — a finished run holds no slot (its phase is terminal),
    and terminating one to admit a new one would reproduce the harm this exists
    to prevent.

    Every bound is user data or a host reading. A bound that cannot be read
    admits: an unstated reservation and an unreadable cgroup can neither of
    them justify refusing work.
    """
    occupying = _live_runs_on_backend(backend_name, exclude_run_ids=exclude_run_ids)
    # The cores bound and the roster refusal are consumed by the workers placed
    # inside the shared reservation, not by every run on one backend: an
    # unplaced run runs outside the allocation and holds no core of it, and a
    # placed run of another backend is still a step inside the same allocation.
    # So both are measured against the reservation's own roster population,
    # taken across backends with the one selection the hold's reach statement
    # counts with, while the lane bound above stays this backend's own.
    reservation_occupancy: int | None = None
    roster_pointers: list[dict[str, Any]] = []
    if flight.placement_for(backend) is not None:
        from reckon.crew import placement as placement_module

        roster_pointers = _live_runs_across_backends(exclude_run_ids=exclude_run_ids)
        reservation_occupancy = len(placement_module.roster_occupants(roster_pointers))
    bounds = summary.concurrency_bounds(
        backend,
        occupancy=len(occupying),
        login_slice=summary.read_login_slice(),
        reservation_occupancy=reservation_occupancy,
    )
    binding = summary.binding_bound(bounds)
    if binding is not None and not binding.admits_one_more:
        run_ids = [str(pointer.get("run_id") or "unknown") for pointer in occupying]
        raise CrewError(
            format_refusal(
                "D09",
                summary.bound_refusal_text(
                    binding, backend_name=backend_name, occupying=run_ids
                ),
            )
        )
    # A placed backend's workers run inside the one shared reservation, and
    # under --overlap the scheduler admits whatever is asked, so the
    # reservation's own roster is a real limit rather than a formality: it is
    # the only bound nothing else enforces on the fleet's behalf. It is counted
    # over the host-wide population, because a placed worker of any backend is
    # a step inside the same allocation.
    _refuse_over_reservation_roster(backend, roster_pointers, project)


def _refuse_against_the_bookend_reserve(
    *,
    config: Mapping[str, Any] | None,
    role: str | None,
    pace_record: Mapping[str, Any],
) -> None:
    """Refuse a dispatch the bookend reserve withholds the window's fraction from.

    The figure is the dispatch's own pace row — the one composed before this
    refusal and carried on the run record — so the reading a caller is refused
    against and the reading a later replay judges the decision from are one
    reading rather than two that could disagree.

    Only a lane declaring a wallet is judged. A lane carrying no wallet has no
    window to reserve a share of, and its row reports no reading by
    construction rather than a reading that failed; refusing there would read
    an absent wallet as an unreadable window and would bar every unmetered
    lane from implementation work.

    The row says which periods the source published. An unpublished period has
    no reserve to judge; a published period whose reading failed is unreadable
    and still refuses work outside the bookend roles.
    """
    if pace_record.get("group") is None:
        return
    verdict = reserve_admit_windows(
        budget_group.reserve_block_for_group(
            (config or {}).get("budget") or {},
            config,
            str(pace_record.get("group")),
        ),
        role=role,
        clocks=pace_record.get("clocks") or {},
        lane=str(pace_record.get("lane") or ""),
    )
    if verdict["admitted"]:
        return
    raise CrewError(format_refusal("D10", verdict["reason"]))


def _resolved_token_budget(
    config: Mapping[str, Any], backend: Mapping[str, Any]
) -> int | None:
    """Return a node's default token budget: role overlay first, fence fallback.

    Mirrors the time-budget resolution: the backend argument is the effective
    settings after the role and spec-level overlays are folded in, so the first
    candidate already carries any overlay-declared value. A value that does
    not coerce to a positive integer is unset rather than a refusal, so a bad
    declaration degrades to the wall-clock fence instead of blocking dispatch.
    """
    for candidate in (
        backend.get("token_budget"),
        (config.get("fences") or {}).get("token_budget"),
    ):
        if candidate is None or candidate == "":
            continue
        try:
            budget = int(candidate)
        except (TypeError, ValueError):
            continue
        if budget > 0:
            return budget
    return None


def _dispatch_lane_observation(
    project: str,
    *,
    root: str | Path | None,
    config: Mapping[str, Any],
    backend_name: str,
    backend: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the quota position used to admit one metered dispatch.

    This is deliberately the read-only half of the budget gate. A dry run must
    make the same lane-declaration decision as a real dispatch without writing
    a preflight history row merely because a caller asked what would happen.
    """
    from reckon import budget as budget_module

    try:
        recorded = budget_module.latest_recorded(project, root=root, config=config)
        state = budget_module.state_for(
            backend_name,
            backend,
            recorded=recorded.get(backend_name),
            unattributed=recorded.unattributed,
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "headroom": "unknown",
            "utilisation_pct": None,
            "observed_at": None,
            "detail": f"the dispatch-time budget reading was unavailable: {exc}",
        }
    return state.as_dict()


def _unmetered_dispatch_alternatives(
    config: Mapping[str, Any], *, role: str, spec_level: str
) -> list[str]:
    """Name configured unmetered backends that can resolve this node."""
    alternatives: list[str] = []
    for candidate in sorted((config.get("backends") or {}), key=str):
        candidate_name = str(candidate)
        if not ledger.is_unmetered_backend(candidate_name):
            continue
        try:
            _resolved_name, settings = resolve_role_override(
                config, role, spec_level, candidate_name
            )
        except CrewError:
            continue
        if settings.get("launch") in ("cli", "in-harness"):
            alternatives.append(candidate_name)
    return alternatives


def _lane_declaration_evidence(
    *,
    declared_backend: str,
    resolved_backend: str,
    observation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Pair the caller's lane choice with the position read at dispatch."""
    measured = observation or {}
    return {
        "backend": declared_backend or None,
        "resolved_backend": resolved_backend,
        "metered": not ledger.is_unmetered_backend(resolved_backend),
        "utilisation_pct": measured.get("utilisation_pct"),
        "observed_at": measured.get("observed_at"),
        "read_at": _utc_now(),
        "headroom": measured.get("headroom"),
    }


def _lane_declaration_finding(
    *,
    backend_name: str,
    observation: Mapping[str, Any],
    alternatives: Iterable[str],
) -> dict[str, str]:
    """Explain how to make an undeclared budget-checked route explicit."""
    utilisation = observation.get("utilisation_pct")
    figure = (
        "unknown"
        if not isinstance(utilisation, (int, float)) or isinstance(utilisation, bool)
        else f"{float(utilisation):g}%"
    )
    candidates = ", ".join(repr(name) for name in alternatives) or "none configured"
    return {
        "property": "fully-specified",
        "detail": (
            f"resolved backend {backend_name!r} is metered, but the caller declared "
            f"no lane; window utilisation read at dispatch is {figure}; unmetered "
            f"backends that would serve this node: {candidates}. Pass --backend "
            f"{backend_name} to declare this metered lane, or name one of the "
            "unmetered alternatives"
        ),
    }


# Committed runs of one node shape a lane must carry before its rework-charged
# cost can separate it from another lane. Below this the advisory says the
# evidence is too thin to name a lane rather than ranking one off a run or two;
# it is the same floor the shape-conditioned lane evidence module uses.
_LANE_ADVISORY_MINIMUM_SAMPLES = 10

# The horizon a projection is compared against when a node declares no time
# budget of its own, so a burn never reads as safe merely for want of a bound.
_LANE_ADVISORY_DEFAULT_HORIZON_SECONDS = 25 * 60


def _lane_advisory_lane(run: Mapping[str, Any]) -> str:
    """Name the lane a committed run was served on, or the empty string."""
    backend = str(run.get("backend") or "").strip()
    if backend:
        return backend
    agent = run.get("agent")
    if isinstance(agent, Mapping):
        return str(agent.get("backend") or "").strip()
    return ""


def _lane_advisory_costs(
    runs: Iterable[Mapping[str, Any]], *, role: str, spec_level: str
) -> dict[str, dict[str, Any]]:
    """Rework-charged input per durable node for every lane on one shape.

    The derivation is the one the routing figures use: a run counts as
    reworked when a later run on the same plan re-touches paths it declared,
    and a lane's cost is the median worker-plus-coordinator input over one
    minus its rework rate. Rework detection, the charged-input reader and the
    exclusion reasons are taken from the same module that derives the routing
    surface, so this carry cannot drift from the figure a reader sees there.

    A shape whose runs were all served on one lane therefore reports one lane,
    and a lane with too few usable runs to charge is reported with its sample
    depth and no cost rather than a cost drawn from a handful of runs.
    """
    from reckon import capabilities as capabilities_module

    usable = [
        run
        for run in runs
        if str(run.get("role") or "") == role
        and str(run.get("spec_level") or "") == spec_level
        and capabilities_module._routing_outcome_exclusion(run) is None
    ]
    later_paths: dict[str, list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
    for index, run in enumerate(usable):
        plan = str(run.get("plan") or "").strip()
        paths = capabilities_module._write_paths(run)
        if plan and paths:
            later_paths[plan].append((index, paths))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, run in enumerate(usable):
        lane = _lane_advisory_lane(run)
        if not lane:
            continue
        paths = capabilities_module._write_paths(run)
        plan = str(run.get("plan") or "").strip()
        reworked = bool(paths and plan) and any(
            later > index and capabilities_module._paths_overlap(paths, other)
            for later, other in later_paths.get(plan, ())
        )
        worker = capabilities_module._input_tokens(run)
        coordinator = capabilities_module._coordinator_input_tokens(run)
        charged = (
            worker + coordinator
            if worker is not None and coordinator is not None
            else None
        )
        grouped[lane].append({"reworked": reworked, "charged_input": charged})

    evidence: dict[str, dict[str, Any]] = {}
    for lane, observations in grouped.items():
        samples = len(observations)
        reworked = sum(bool(item["reworked"]) for item in observations)
        rework_rate = reworked / samples
        inputs = [
            float(item["charged_input"])
            for item in observations
            if item["charged_input"] is not None
        ]
        median_input = capabilities_module._median_or_none(inputs)
        evidence[lane] = {
            "samples": samples,
            "rework_rate": round(rework_rate, 6),
            "input_samples": len(inputs),
            # The floor sits on the observations the charged median is actually
            # drawn from, not on the usable runs beside them: a run with no
            # paired worker-and-coordinator reading contributes nothing to the
            # cost, so counting it toward the floor would clear a cost built
            # from a handful of readings. ``_charged_cost`` also refuses a
            # median of None, which is the same population stated as zero.
            "cost_per_durable_node": (
                capabilities_module._charged_cost(median_input, rework_rate)
                if len(inputs) >= _LANE_ADVISORY_MINIMUM_SAMPLES
                else None
            ),
        }
    return evidence


def _lane_advisory_cheaper_lane(
    runs: Iterable[Mapping[str, Any]],
    *,
    resolved_lane: str,
    role: str,
    spec_level: str,
    configured_lanes: Iterable[str],
) -> dict[str, Any]:
    """Name the lane measured rework serves this shape on more cheaply, or none.

    Only lanes the flight configures are named, so the clause always points at
    something a caller could actually route to. A lane whose rework-charged
    cost is not measured -- too few runs, or no paired coordinator reading --
    is never guessed at: the clause states the shortfall instead, because a
    recommendation drawn from one or two runs would read as evidence.
    """
    evidence = _lane_advisory_costs(runs, role=role, spec_level=spec_level)
    candidates = {
        name
        for name in evidence
        if name in set(configured_lanes) or name == resolved_lane
    }
    measured = {
        name: evidence[name]
        for name in candidates
        if evidence[name]["cost_per_durable_node"] is not None
    }
    resolved = evidence.get(resolved_lane)
    if resolved_lane not in measured:
        chargeable = resolved["input_samples"] if resolved else 0
        return {
            "lane": None,
            "state": "insufficient_evidence",
            "resolved_cost_per_durable_node": None,
            "candidates": sorted(measured),
            "detail": (
                f"the rework-charged cost of {resolved_lane!r} for {role!r} at "
                f"{spec_level!r} is not measured: {chargeable} usable run(s), "
                f"{_LANE_ADVISORY_MINIMUM_SAMPLES} needed, so no lane can be "
                "named cheaper on this evidence"
            ),
        }
    cheapest = min(measured, key=lambda name: measured[name]["cost_per_durable_node"])
    if cheapest == resolved_lane:
        return {
            "lane": None,
            "state": "none_cheaper",
            "resolved_cost_per_durable_node": resolved["cost_per_durable_node"],
            "candidates": sorted(measured),
            "detail": (
                f"no configured lane serves {role!r} at {spec_level!r} more "
                f"cheaply on measured rework than {resolved_lane!r} "
                f"({resolved['cost_per_durable_node']:g} input tokens per "
                "durable node)"
            ),
        }
    chosen = measured[cheapest]
    return {
        "lane": cheapest,
        "state": "measured",
        "resolved_cost_per_durable_node": resolved["cost_per_durable_node"],
        "cost_per_durable_node": chosen["cost_per_durable_node"],
        "rework_rate": chosen["rework_rate"],
        "samples": chosen["samples"],
        "candidates": sorted(measured),
        "detail": (
            f"measured rework puts {cheapest!r} at "
            f"{chosen['cost_per_durable_node']:g} input tokens per durable node "
            f"for {role!r} at {spec_level!r} against {resolved_lane!r} at "
            f"{resolved['cost_per_durable_node']:g}, over {chosen['samples']} "
            f"run(s) at a {chosen['rework_rate']:.3f} rework rate"
        ),
    }


def _lane_advisory_ledger_runs(
    project: str, ledger_root: str | Path | None
) -> list[dict[str, Any]]:
    """Read one project's committed runs in promotion order, or nothing.

    An absent ledger is the ordinary state of a project that has run no workers,
    so it reads as no evidence rather than an error: the advisory then states
    that no lane can be named instead of refusing the dispatch over it.
    """
    try:
        data, _version = ledger.load(project, root=ledger_root)
    except (OSError, ValueError, ledger.LedgerError):
        return []
    return [dict(run) for run in data.get("runs") or [] if isinstance(run, Mapping)]


def _lane_advisory_horizon_seconds(node: TaskNode) -> int:
    """The node's own fence as a horizon, falling back to the default bound."""
    declared = str(node.time_budget or "").strip()
    if declared:
        try:
            return int(parse_duration(declared))
        except CrewError:
            pass
    return _LANE_ADVISORY_DEFAULT_HORIZON_SECONDS


def _lane_advisory_instant(value: object) -> datetime | None:
    """Parse an ISO instant from a budget reading, or None when unreadable."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dispatch_lane_advisory(
    *,
    backend_name: str,
    metered: bool,
    observation: Mapping[str, Any] | None,
    node: TaskNode,
    cheaper_lane: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Carry a lane's trajectory beside the routing, refusing nothing.

    The advisory exists because a coordinator learns its lane's trajectory only
    if it goes looking, and the one moment it is certainly not looking is while
    it dispatches. It therefore rides on the dispatch: the utilisation, burn
    multiple, projected exhaustion and reset are read from the same window
    reading the dispatch already took, and the projection is compared against
    the horizon of the work in hand. Nothing here refuses, holds or reroutes --
    the payload records the position and the resolved backend is untouched.

    ``state`` is ``emitted`` only when a metered lane's projection precedes the
    node's horizon, which is the moment the advice would change a decision;
    otherwise the carry is ``quiet`` and names why, so a silent payload is
    never mistaken for a lane that was checked and found safe.
    """
    measured = observation or {}
    horizon_seconds = _lane_advisory_horizon_seconds(node)
    moment = now or datetime.now(UTC)
    horizon_ends_at = moment + timedelta(seconds=horizon_seconds)
    projection = _lane_advisory_instant(measured.get("projected_exhaustion_at"))
    precedes: bool | None = None
    if projection is not None:
        precedes = projection <= horizon_ends_at
    carry = {
        "state": "quiet",
        "detail": "",
        "backend": backend_name,
        "metered": metered,
        "utilisation_pct": measured.get("utilisation_pct"),
        "burn_multiple": measured.get("burn_multiple"),
        "projected_exhaustion_at": measured.get("projected_exhaustion_at"),
        "resets_at": measured.get("resets_at"),
        "seconds_until_reset": measured.get("seconds_until_reset"),
        "observed_at": measured.get("observed_at"),
        "horizon_seconds": horizon_seconds,
        "horizon_ends_at": horizon_ends_at.isoformat(),
        "precedes_horizon": precedes,
        "cheaper_lane": cheaper_lane,
    }
    utilisation = measured.get("utilisation_pct")
    burn = measured.get("burn_multiple")
    if not metered:
        carry["detail"] = (
            f"{backend_name!r} is unmetered, so it has no window to exhaust; "
            "the local lane's scarcity is throughput, which it does not publish"
        )
        return carry
    if projection is None:
        carry["detail"] = (
            f"no projected exhaustion is available for {backend_name!r}; "
            "the burn projection needs a numeric utilisation bounded by a "
            "known window, and without one no horizon comparison is made"
        )
        return carry
    if not precedes:
        carry["detail"] = (
            f"{backend_name!r} is projected to exhaust at "
            f"{measured.get('projected_exhaustion_at')}, which is after this "
            f"node's {horizon_seconds}s horizon ending "
            f"{horizon_ends_at.isoformat()}"
        )
        return carry
    carry["state"] = "emitted"
    carry["detail"] = (
        f"{backend_name!r} sits at {utilisation}% utilisation burning "
        f"{burn}x, projected to exhaust at "
        f"{measured.get('projected_exhaustion_at')} -- before this node's "
        f"{horizon_seconds}s horizon ending {horizon_ends_at.isoformat()}; the "
        f"window resets at {measured.get('resets_at')}"
    )
    return carry


def _lane_reading_unknown(*, detail: str) -> dict[str, Any]:
    """Advisory carry for a lane reading the dispatch could not trust."""
    return {
        "state": "unknown",
        "headroom": "unknown",
        "binding_observed": "unknown",
        "mean_context": "unknown",
        "generating": "unknown",
        "waiting": "unknown",
        "throughput": _lane_document.blank_throughput(detail=detail),
        "observed_at": None,
        "age_seconds": None,
        "suggested_shelf_life_seconds": None,
        "detail": detail,
    }


def _metric_number(value: object) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _lane_reading_carry(
    document: Mapping[str, Any] | None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Strictly parse one lane reading document into its advisory carry.

    A lane reading document is a JSON object a lane publishes about itself:
    ``headroom`` and ``mean_context`` as numbers, ``binding_observed`` naming
    the window observed binding, ``observed_at`` stamping when the reading was
    taken, and an optional ``suggested_shelf_life_seconds`` for how long the
    reading stays trustworthy. Parsing is strict, because the quiet failure
    runs toward apparent headroom: a missing or malformed figure is withheld
    as ``unknown`` naming which one it was, a field is never resolved to zero,
    and a reader that cannot understand the instrument says so rather than
    guessing. Strictness is per field rather than per document — what a
    reading does carry is still measured, and discarding it along with the
    field that failed serves nobody. What collapses the whole carry is a
    defect in the reading ITSELF: no document, one that will not parse, a
    missing or unintelligible ``observed_at``, or a reading older than its
    stated shelf life, whose age is then stated so a stale figure is never
    carried as if it were current.

    ``binding_observed`` is consumed as the document's own field and is never
    re-derived from whether the dispatch waited or was preempted — the carry
    takes no such inputs, so the only source of the flag is the document.

    The carry also answers what the lane is carrying and how fast it is going,
    because those two decide where the next node goes and a dispatcher that has
    to ask a second view for them is the compensation this carry exists to
    replace. ``generating`` is the lane's count of requests actively
    generating, ``waiting`` its count of requests queued behind them, and
    ``throughput`` the achieved rate with the vintage and the denominator that
    make it interpretable -- see ``lane_document.read_lane_throughput``. A document that
    publishes no count leaves that count ``unknown``, never zero, so an
    unmeasured lane and an idle one do not read alike.
    """
    if document is None:
        return _lane_reading_unknown(detail="no lane document")
    if not isinstance(document, Mapping):
        return _lane_reading_unknown(
            detail=f"lane document is {type(document).__name__}, not a JSON object"
        )
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    # The document's own keys -- the stamp it carries, the mean context, the
    # constraint observed binding and its shelf life -- are resolved by the
    # lane-document reader, so the dispatch holds no spelling of them and
    # cannot drift from the one reader that owns them.
    fields = _lane_document.read_lane_reading_fields(document)
    stamp = fields["observed_at"]
    if stamp is None:
        return _lane_reading_unknown(detail=fields["detail"])
    # Retained rather than routed through ``reckon._timestamps.parse_iso``: the
    # refusal quotes the parser's own exception text, which the shared parser
    # swallows to return ``None``, and the reading is strict enough to report
    # why a stamp was rejected.
    try:
        observed = datetime.fromisoformat(stamp)
    except ValueError as exc:
        return _lane_reading_unknown(
            detail=f"'observed_at' {stamp!r} is not an ISO-8601 timestamp: {exc}"
        )
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age = now - observed
    if age.total_seconds() < 0:
        return _lane_reading_unknown(
            detail=f"'observed_at' {stamp!r} lies in the future"
        )
    # Each figure is withheld on its own. A document that omits one still
    # measured the others, and the timestamp beside them is what makes any of
    # them usable, so collapsing the reading over a single absent field
    # discards measurements the lane did take. The lane omits its concurrency
    # ceiling and nulls its headroom while the pool drains, which is precisely
    # when a reader needs the running count and the mean context.
    headroom = _metric_number(
        _lane_document.read_lane_document(document).get("headroom")
    )
    mean_context = fields["mean_context"]
    binding = fields["binding_observed"]
    shelf = fields["shelf_life_seconds"]
    if shelf is not None and shelf > 0 and age.total_seconds() > shelf:
        carry = _lane_reading_unknown(
            detail=(
                f"reading is {age.total_seconds():.0f}s old, older than its "
                f"{shelf:g}s shelf life"
            )
        )
        carry["age_seconds"] = int(age.total_seconds())
        carry["suggested_shelf_life_seconds"] = shelf
        return carry
    unreadable = [
        name
        for name, value in (
            ("headroom", headroom),
            ("mean_context", mean_context),
        )
        if value is None
    ]
    age_seconds = int(age.total_seconds())
    counts = _lane_document.read_lane_counts(document)
    return {
        "state": "fresh",
        "headroom": "unknown" if headroom is None else headroom,
        "generating": counts["generating"],
        "waiting": counts["waiting"],
        "throughput": _lane_document.read_lane_throughput(
            document, reading_stamp=stamp, reading_age_seconds=age_seconds, now=now
        ),
        # The field names WHICH constraint binds, and a lane with no such
        # constraint has nothing to name rather than nothing to report.
        "binding_observed": "unknown" if binding is None else binding,
        "mean_context": "unknown" if mean_context is None else mean_context,
        "observed_at": stamp,
        "age_seconds": age_seconds,
        "suggested_shelf_life_seconds": shelf,
        "detail": (
            ""
            if not unreadable
            else "lane document carries no numeric "
            + " or ".join(f"{name!r}" for name in unreadable)
        ),
    }


def _dispatch_lane_reading(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Read the resolved lane's published reading and carry it, refusing nothing.

    A backend may declare ``lane_document``, a path to the local JSON the lane
    publishes about itself. The dispatch reads it strictly and attaches the
    carry to the plan as advisory data. No value in the document refuses,
    holds or reroutes a dispatch — the reading is carried so a consumer can see
    what the lane reported beside the routing decision, never instead of it.
    An absent declaration, an unreadable file, or an unparsable document
    collapses the carry to ``unknown`` naming the reason rather than to a
    figure.

    Two facts elsewhere do withhold a dispatch, and neither is a value the
    reading carries: the gate file's ``paused`` field, read from the backend's
    declared ``gate_document``, and a lane document whose published
    ``router_generation_gate.config_path`` differs from that declared path.
    Every other value of the lane document still withholds nothing, so the
    carry and the plan agree.
    """
    declared = backend.get("lane_document")
    if not declared:
        return _lane_reading_unknown(detail="backend declares no lane document")
    path = Path(str(declared)).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _lane_reading_unknown(
            detail=f"lane document {str(path)!r} cannot be read — {exc}"
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return _lane_reading_unknown(
            detail=f"lane document {str(path)!r} is not valid JSON — {exc}"
        )
    if not isinstance(payload, Mapping):
        return _lane_reading_unknown(
            detail=f"lane document {str(path)!r} is not a JSON object"
        )
    return _lane_reading_carry(payload)


# The states in which the lane gate withholds a dispatch rather than launching.
# A gate that says paused and a gate that cannot be answered both wait, because
# a gate nobody can confirm has ended is not an open one.
_LANE_GATE_WAITING_STATES = frozenset({"paused", "unreadable"})

# The gate file's own keys: a JSON boolean and an optional reason string.
_LANE_GATE_PAUSED_KEY = "paused"
_LANE_GATE_REASON_KEY = "reason"

# The path the lane document publishes under its gate block, compared against
# the backend's declared path so a pause written to a file the dispatch is not
# reading cannot pass unseen.
_LANE_GATE_CONFIG_PATH_KEY = "config_path"

# Each of the two gate reads has its own deadline rather than one budget shared
# between them, because a single stat on this filesystem has taken 8.5 s.
LANE_GATE_READ_DEADLINE_SECONDS = 5.0


class LanePaused(CrewError):  # noqa: N818 - named as the dispatch states it, beside BudgetHold
    """A dispatch waits on the lane gate rather than launching.

    Distinct from a refusal for the same reason a budget hold is: nothing was
    created and nothing is wrong with the node, so a caller retries once the
    gate opens rather than reshaping the work. The gate object the decision was
    taken from rides the exception, so every surface reports the same state the
    dispatch read.
    """

    def __init__(self, gate: Mapping[str, Any]) -> None:
        self.gate = dict(gate)
        super().__init__(
            str(gate.get("detail") or "").strip()
            or (f"the lane gate is {gate.get('state')!r} at {gate.get('gate_path')!r}")
        )


class LaneHeld(LanePaused):
    """A dispatch holds because the lane's own router grants it no worker slot.

    The lane is answering and its own arithmetic leaves this coordinator
    session no room, so the node is held rather than refused: nothing was
    created, the node is still ready, and the caller retries when the router's
    next reading grants a slot. The allowance decision that produced the hold
    rides the exception, so every surface reports the figure the router
    published rather than a second opinion about it.
    """


class _GateReadDeadline(Exception):  # noqa: N818 - an internal marker, not a raised API
    """A gate or lane-document read did not answer inside its own deadline."""


def _gate_text_reader(path: Path) -> str:
    """Read one gate-related file as text; an indirection a test can replace."""
    return path.read_text(encoding="utf-8")


def _read_text_under_deadline(
    path: Path, *, timeout: float, reader: Callable[[Path], str] | None = None
) -> str:
    """Read ``path`` under its own deadline, raising on timeout or ``OSError``.

    The read runs on a daemon thread so a stalled filesystem cannot hold the
    dispatch open: the deadline passing raises rather than waiting, and the
    abandoned thread dies with the process. The thread re-raises whatever the
    read raised, so a clean file-not-found stays distinguishable from a
    permission error, which the gate rule reads differently.
    """
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["text"] = (reader or _gate_text_reader)(path)
        except BaseException as exc:  # noqa: BLE001 - re-raised to the caller
            outcome["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise _GateReadDeadline(f"the read of {str(path)!r} exceeded {timeout:g}s")
    if "error" in outcome:
        raise outcome["error"]
    return str(outcome.get("text") or "")


def _gate_paths_agree(declared: Path, published: Path) -> bool:
    """Whether two gate paths name the same file once normalised."""
    return os.path.abspath(os.path.expanduser(str(declared))) == os.path.abspath(
        os.path.expanduser(str(published))
    )


def _gate_path_check(backend: Mapping[str, Any], declared: str) -> dict[str, Any]:
    """Compare the declared gate path against the lane document's published one.

    The declaration is a copy of a path the router derives, so it can drift. A
    mismatch waits, naming both paths, because a pause written to the file the
    dispatch is not reading would otherwise pass unseen. The lane document is
    read only for this comparison, and a declaration with nothing to compare
    against — no lane document, one that cannot be read inside its deadline one
    that is not a JSON object, one publishing no gate block, or one publishing
    no ``config_path`` — records ``skipped`` with its reason and never holds a
    dispatch on its own.
    """
    lane_declared = backend.get("lane_document")
    if not lane_declared:
        return {
            "state": "skipped",
            "detail": "backend declares no lane document to publish a config_path",
        }
    lane_path = Path(str(lane_declared)).expanduser()
    try:
        text = _read_text_under_deadline(
            lane_path, timeout=LANE_GATE_READ_DEADLINE_SECONDS
        )
    except _GateReadDeadline as exc:
        return {"state": "skipped", "detail": str(exc)}
    except OSError as exc:
        return {
            "state": "skipped",
            "detail": f"lane document {str(lane_path)!r} cannot be read — {exc}",
        }
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return {
            "state": "skipped",
            "detail": f"lane document {str(lane_path)!r} is not valid JSON — {exc}",
        }
    if not isinstance(payload, Mapping):
        return {
            "state": "skipped",
            "detail": f"lane document {str(lane_path)!r} is not a JSON object",
        }
    gate_block = payload.get(_lane_document.GATE_KEY)
    if not isinstance(gate_block, Mapping):
        return {
            "state": "skipped",
            "detail": "lane document publishes no router_generation_gate block",
        }
    published = gate_block.get(_LANE_GATE_CONFIG_PATH_KEY)
    if not isinstance(published, str) or not published.strip():
        return {
            "state": "skipped",
            "detail": ("lane document publishes no router_generation_gate.config_path"),
        }
    published_path = Path(published.strip()).expanduser()
    if _gate_paths_agree(Path(declared), published_path):
        return {"state": "matched", "detail": ""}
    return {
        "state": "mismatch",
        "detail": (
            f"declared gate document {declared!r} differs from the lane "
            f"document's published config_path {str(published_path)!r}"
        ),
    }


def fleet_gate_path() -> Path:
    """The shared crew document that can hold launches on every backend."""
    return crew_home() / "fleet-gate.json"


def _fleet_gate_text_reader(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dispatch_fleet_gate() -> dict[str, Any]:
    """Read the fleet gate under the same deadline as a backend gate."""
    path = fleet_gate_path()
    try:
        text = _read_text_under_deadline(
            path,
            timeout=LANE_GATE_READ_DEADLINE_SECONDS,
            reader=_fleet_gate_text_reader,
        )
    except FileNotFoundError:
        base = {"state": "open", "paused": False, "reason": None, "detail": ""}
    except (_GateReadDeadline, OSError) as exc:
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": f"fleet gate {str(path)!r} cannot be read — {exc}",
        }
    else:
        try:
            base = _gate_rows_from_payload(json.loads(text), path)
        except ValueError as exc:
            base = {
                "state": "unreadable",
                "paused": None,
                "reason": None,
                "detail": f"fleet gate {str(path)!r} is not valid JSON — {exc}",
            }
    if base["state"] == "paused":
        base["detail"] = f"fleet gate is paused: {base['reason'] or 'no reason given'}"
    elif base["state"] == "unreadable" and "fleet gate" not in base["detail"]:
        base["detail"] = f"fleet gate {str(path)!r}: {base['detail']}"
    return {
        "gate": "fleet",
        "gate_path": str(path),
        **base,
        "path_check": "fleet-wide",
        "path_check_detail": "",
    }


def _require_fleet_gate_open() -> dict[str, Any]:
    """Read the shared gate at the last boundary before starting a worker."""
    gate = _dispatch_fleet_gate()
    if gate["state"] in _LANE_GATE_WAITING_STATES:
        raise LanePaused(gate)
    return gate


def _dispatch_lane_gate(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Read the fleet gate, then the gate file a backend declares.

    The gate file is the authority for whether the lane admits work: a JSON
    object whose ``paused`` is a boolean, with an optional ``reason`` string.
    The rule this returns one row of:

    * no ``gate_document`` declared — ``not-declared``; the dispatch proceeds,
      which is a host or backend with no router;
    * the file reads as an object with ``paused`` true — ``paused``;
    * the file reads with ``paused`` false or the key absent — ``open``;
    * the read fails with a clean file-not-found and nothing else —
      ``declared-but-missing``; a mistyped path must not read like a quiet host
      with no router;
    * any other failure — a read past its deadline, a permission error, a file
      that is not a JSON object, a non-boolean ``paused``, or a declared path
      that differs from the lane document's published ``config_path`` —
      ``unreadable``, with the defect named.

    ``paused`` and ``unreadable`` are the two rows that withhold a dispatch;
    every other row lets it proceed. The lane document is read only for the
    path comparison, and a comparison that cannot be made records ``skipped``
    in ``path_check`` without holding the dispatch on its own.
    """
    fleet_gate = _dispatch_fleet_gate()
    if fleet_gate["state"] in _LANE_GATE_WAITING_STATES:
        return fleet_gate
    declared = str(backend.get("gate_document") or "").strip()
    if not declared:
        return {
            "state": "not-declared",
            "gate_path": None,
            "paused": None,
            "reason": None,
            "detail": "",
            "path_check": "not-declared",
            "path_check_detail": "",
        }
    path = Path(declared).expanduser()
    try:
        text = _read_text_under_deadline(path, timeout=LANE_GATE_READ_DEADLINE_SECONDS)
    except FileNotFoundError:
        base: dict[str, Any] = {
            "state": "declared-but-missing",
            "paused": None,
            "reason": None,
            "detail": "gate path declared but missing",
        }
    except _GateReadDeadline as exc:
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": str(exc),
        }
    except OSError as exc:
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": f"gate document {str(path)!r} cannot be read — {exc}",
        }
    else:
        try:
            payload = json.loads(text)
        except ValueError as exc:
            base = {
                "state": "unreadable",
                "paused": None,
                "reason": None,
                "detail": f"gate document {str(path)!r} is not valid JSON — {exc}",
            }
        else:
            base = _gate_rows_from_payload(payload, path)
    check = _gate_path_check(backend, declared)
    if check["state"] == "mismatch":
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": check["detail"],
        }
    return {
        "gate_path": declared,
        **base,
        "path_check": check["state"],
        "path_check_detail": check["detail"],
    }


def _gate_rows_from_payload(payload: object, path: Path) -> dict[str, Any]:
    """Resolve the gate row a parsed gate-file payload stands for."""
    if not isinstance(payload, Mapping):
        return {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": f"gate document {str(path)!r} is not a JSON object",
        }
    paused = payload.get(_LANE_GATE_PAUSED_KEY)
    reason = payload.get(_LANE_GATE_REASON_KEY)
    reason_text = reason.strip() if isinstance(reason, str) and reason.strip() else None
    if _LANE_GATE_PAUSED_KEY in payload and not isinstance(paused, bool):
        return {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": (
                f"gate document {str(path)!r} carries a non-boolean "
                f"'paused' ({paused!r})"
            ),
        }
    if paused is True:
        return {"state": "paused", "paused": True, "reason": reason_text, "detail": ""}
    return {"state": "open", "paused": False, "reason": reason_text, "detail": ""}


# The flight-config key a backend sets to declare that its lane carries this
# deployment's orchestrators. Declared, never inferred from the backend's name:
# the orchestrator role is a property of the deployment, so a rule matching on
# a name breaks the moment an orchestrator runs elsewhere and misses an alias
# that points at one. A lane that declares nothing serves no orchestrator and
# is dispatchable as before.
ORCHESTRATOR_LANE_DECLARATION_KEY = "serves_orchestrators"

# What the stop does to a dispatch that resolves to a declaring lane: it
# records rather than refuses. A refusal does not remove the work, it moves it
# onto whatever lane remains, and that is only safe while the receiving lane
# serves it reliably: the locally served lane's mid-turn death rate is stated
# for the window before its repair and has not been re-measured after it, so a
# refusal landing there loses the dispatch rather than relocating it. The
# record still removes the silent case — the lane, why it is fenced and the
# discharge all reach the run's record and the payload.
ORCHESTRATOR_LANE_STOP_SEVERITY = "recorded"


def _orchestrator_lane_discharge_candidates(
    config: Mapping[str, Any], *, role: str, spec_level: str
) -> list[str]:
    """Name configured lanes that serve no orchestrator and can resolve the node."""
    candidates: list[str] = []
    backends = config.get("backends") or {}
    for candidate in sorted(backends, key=str):
        candidate_name = str(candidate)
        settings = backends.get(candidate_name)
        if not isinstance(settings, Mapping):
            continue
        if settings.get(ORCHESTRATOR_LANE_DECLARATION_KEY):
            continue
        try:
            _resolved, effective = resolve_role_override(
                config, role, spec_level, candidate_name
            )
        except CrewError:
            continue
        if effective.get("launch") in ("cli", "in-harness"):
            candidates.append(candidate_name)
    return candidates


def _dispatch_orchestrator_lane_stop(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    config: Mapping[str, Any],
    role: str,
    spec_level: str,
) -> dict[str, Any]:
    """Report a resolved lane that declares it serves this deployment's orchestrators.

    One subscription runs every orchestrator here, so background work placed on
    the same lane spends the capacity the sessions that dispatch, merge, promote
    and record need. The end state is not a slow node: a lane saturated there
    stops every session at once, including the ones that would have noticed, and
    work already in flight is then unreachable by the only processes that could
    reconcile it.

    The declaration is read from the resolved backend, so ``--local``, an
    explicit ``--backend``, a role overlay and a budget fallback all reach it,
    and a lane that declares nothing is dispatchable exactly as before. The
    stop is composed in resolution, before a run directory, a live pointer or a
    worktree exists, so it cannot be lost to a failure part way through the
    writes that follow.

    ``severity`` states what the stop does with the launch. It is ``recorded``
    rather than ``refused`` because the lane a refusal would push this node
    onto is not measurably reliable at the effort such work needs, so refusing
    would trade a lane that is too busy for a lane that does not finish. The
    record names the lane, why it is fenced and the discharge, and it reaches
    the run's own record and the dispatch payload either way.
    """
    if not backend.get(ORCHESTRATOR_LANE_DECLARATION_KEY):
        return {
            "state": "not-declared",
            "severity": None,
            "lane": backend_name,
            "detail": (
                f"resolved lane {backend_name!r} declares no orchestrator role, "
                "so it is dispatchable as before"
            ),
            "discharge": "",
        }
    candidates = _orchestrator_lane_discharge_candidates(
        config, role=role, spec_level=spec_level
    )
    if candidates:
        discharge = (
            "route this node to a lane that serves no orchestrator, declared "
            "with --backend: " + ", ".join(repr(name) for name in candidates)
        )
    else:
        discharge = (
            "no configured lane that serves no orchestrator can resolve this "
            "node; add a backend that declares no orchestrator role and route "
            "the node to it"
        )
    return {
        "state": "declared",
        "severity": ORCHESTRATOR_LANE_STOP_SEVERITY,
        "lane": backend_name,
        "detail": (
            f"resolved lane {backend_name!r} declares "
            f"{ORCHESTRATOR_LANE_DECLARATION_KEY}. Do not dispatch background "
            "work to an orchestrator lane: it runs the orchestrators; background "
            "work there costs orchestrator capacity, and saturating it stops "
            "every session rather than one node"
        ),
        "discharge": discharge,
    }


def _orchestrator_lane_stop_line(stop: Mapping[str, Any]) -> str:
    """Render the stop as the one line a run's warnings carry."""
    return f"{stop['detail']}; {stop['discharge']}"


def _path_is_tmpfs(path: str | Path) -> bool:
    """Return whether a path resolves beneath a tmpfs or ramfs mount."""
    target = Path(path).expanduser().resolve()
    best: tuple[int, str] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        fields, separator, trailing = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        trailing_parts = trailing.split()
        if len(parts) < 5 or not trailing_parts:
            continue
        mount = Path(parts[4].replace("\\040", " ")).resolve()
        if target == mount or target.is_relative_to(mount):
            candidate = (len(mount.parts), trailing_parts[0])
            if best is None or candidate[0] > best[0]:
                best = candidate
    return bool(best and best[1] in {"tmpfs", "ramfs"})


# A declared endpoint is probed before launch, so the probe is bounded: a slow
# or absent router must refuse the placement rather than hold the dispatch open.
_REQUIREMENT_PROBE_TIMEOUT_SECONDS = 3.0


def _is_node_local_path(path: str | Path) -> bool:
    """Whether a path lives on this node's own storage rather than shared.

    The per-user runtime directory is named first because it is the case that
    reads as present: it exists on every node, so a launch against it succeeds
    and the worker then finds different bytes on the node it runs on. The mount
    table is the general answer beneath it, covering any tmpfs the host mounts
    for scratch.
    """
    text = str(Path(str(path)).expanduser())
    if text == "/run/user" or text.startswith("/run/user/"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        runtime_root = str(Path(runtime).expanduser())
        if text == runtime_root or text.startswith(runtime_root.rstrip("/") + "/"):
            return True
    return _path_is_tmpfs(text)


def _endpoint_answers(endpoint: str) -> tuple[bool, str]:
    """Whether a host:port endpoint accepts a connection, and why not.

    Bounded, because the check is a precondition of the launch rather than part
    of it: a router that is slow to answer must refuse the placement rather
    than hold the dispatch open.
    """
    host, separator, port_text = str(endpoint).strip().rpartition(":")
    if not separator or not host:
        return False, "not a host:port endpoint"
    try:
        port = int(port_text)
    except ValueError:
        return False, f"{port_text!r} is not a port"
    try:
        with socket.create_connection(
            (host, port), timeout=_REQUIREMENT_PROBE_TIMEOUT_SECONDS
        ):
            return True, "reachable"
    except OSError as exc:
        return False, f"{host}:{port} refused the connection — {exc}"


def check_placement_requirements(
    placement: Mapping[str, Any] | None, *, backend_name: str
) -> None:
    """Refuse a placement before launch when something it declares is invisible.

    Coordinator-side and ahead of every side effect, because the check decides
    whether the launch is worth making: a requirement the target node cannot
    see fails inside the worker and reads as a worker defect. A node-side check
    would need the node to start, which is the thing being refused.

    Only the declaration is read — no scheduler is invoked and no job is
    submitted — so a dry run reaches the verdict a real dispatch reaches. A
    backend declaring no placement is untouched, which keeps every backend
    that declares none launching exactly as before.
    """
    if not isinstance(placement, Mapping) or not placement:
        return
    scheduler = str(placement.get("scheduler") or "")
    queries = flight.placement_scheduler_queries(placement)
    if scheduler and set(queries) != {"state_query", "reason_query"}:
        raise CrewError(
            placement_query_undeclared(backend=backend_name, scheduler=scheduler)
        )
    for name, path, endpoint in _placement_requirement_targets(placement):
        if path is None and endpoint is None:
            raise CrewError(
                placement_requirement_unmet(
                    backend=backend_name,
                    placement=placement,
                    name=name,
                    detail="the requirement declares neither a path nor an endpoint",
                )
            )
        if path is not None:
            if _is_node_local_path(path):
                raise CrewError(
                    placement_requirement_node_local(
                        backend=backend_name,
                        placement=placement,
                        name=name,
                        path=path,
                    )
                )
            if not Path(path).expanduser().exists():
                raise CrewError(
                    placement_requirement_unmet(
                        backend=backend_name,
                        placement=placement,
                        name=name,
                        detail=f"path {path!r} does not exist",
                    )
                )
            continue
        assert endpoint is not None
        reachable, why = _endpoint_answers(endpoint)
        if not reachable:
            raise CrewError(
                placement_requirement_unmet(
                    backend=backend_name,
                    placement=placement,
                    name=name,
                    detail=f"endpoint {endpoint!r} is unreachable — {why}",
                )
            )


def _placement_requirement_targets(
    placement: Mapping[str, Any],
) -> Iterable[tuple[str, str | None, str | None]]:
    """Yield each declared requirement as (name, path, endpoint).

    An entry declaring neither target is yielded with both absent rather than
    skipped, so a malformed declaration is refused by the caller instead of
    silently passing a check it never ran.
    """
    for index, entry in enumerate(flight.placement_requirement_entries(placement)):
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or f"requirement {index + 1}")
        path = str(entry["path"]) if entry.get("path") else None
        endpoint = str(entry["endpoint"]) if entry.get("endpoint") else None
        yield name, path, endpoint


def _resolved_write_paths(
    backend: Mapping[str, Any], *, run_directory: Path
) -> list[str]:
    """Return a role's default write scope, or [] when it declares none.

    A role's ``write_paths`` are relative and resolve against this dispatch's
    own run directory rather than the repository, so the shipped default names
    no host-specific location and grants no reach into repository source. A
    node that declares its own write_paths is never touched here.
    """
    declared = backend.get("write_paths")
    if not declared:
        return []
    return [str((run_directory / str(entry)).resolve()) for entry in declared]


def _require_write_paths_in_authority(
    node: TaskNode, authority: Mapping[str, Any]
) -> None:
    """Confine writes to resolved repositories or durable delivery roots."""
    work_repo = Path(str(authority["write"]["repository"])).resolve()
    repository_roots: list[Path] = []
    for value in authority.get("repositories") or (work_repo,):
        root = Path(str(value)).expanduser().resolve()
        if root not in repository_roots:
            repository_roots.append(root)
    if work_repo not in repository_roots:
        repository_roots.append(work_repo)
    roots = delivery_roots()
    allowed_roots = (*repository_roots, *roots)
    for declared in node.write_paths:
        raw = Path(declared).expanduser()
        resolved = (raw if raw.is_absolute() else work_repo / raw).resolve()
        if any(resolved.is_relative_to(root) for root in allowed_roots):
            continue
        repositories = ", ".join(str(root) for root in repository_roots)
        raise CrewError(
            f"write path {declared!r} resolves outside the authorised work repository "
            f"{work_repo}, every other repository registered by the dispatch authority "
            f"({repositories}), and Reckon delivery directories {', '.join(str(root) for root in roots)}; "
            "the repository containing this path is missing from "
            "mounts.json or outside the resolved plan authority"
        )


def _sandbox_reachability(
    node: TaskNode,
    *,
    backend: Mapping[str, Any],
    repository: Path,
    run_directory: Path,
) -> tuple[tuple[Path, ...] | None, list[dict[str, str]]]:
    """Resolve writable roots and report declared paths outside every grant."""
    roots = _backends.sandbox_write_roots(
        backend,
        repository=repository,
        run_directory=run_directory,
        reports_directory=reports_dir(),
        manifest_path=node.manifest_path,
        review_store_directory=review_store_root(),
    )
    if "execution_capable" not in backend:
        return roots, []
    tier = str(backend.get("sandbox") or _backends.READ_ONLY)
    unreachable = [
        str(path)
        for path in node.write_paths
        if not _backends.sandbox_can_write(
            path,
            repository=repository,
            write_roots=roots,
        )
    ]
    grants = "unrestricted" if roots is None else ", ".join(str(root) for root in roots)
    return roots, [
        {
            "property": "scoped",
            "detail": (
                f"write path {path!r} is unreachable in resolved sandbox tier "
                f"{tier!r}; writable grants: {grants or 'none'}"
            ),
        }
        for path in unreachable
    ]


def _fence_write_roots(
    *,
    backend: Mapping[str, Any],
    repository: str | Path,
    run_directory: str | Path,
    manifest_path: str | Path | None,
    worktree: str | Path | None,
    declared_write_paths: Iterable[str],
) -> tuple[Path, ...]:
    """Return every root a fenced launch re-binds writable.

    The fence seals each protected path and re-binds only the roots it is
    handed, so a tier's own write roots are not enough on their own: an
    ``unrestricted`` tier is unrestricted only while nothing seals the machine.
    Under the fence the delivery stores a restricted tier gets are named here
    too, together with every declared write path outside the worktree, so a
    worker delivers into the same place whichever tier it runs on.
    """
    return _backends.fenced_write_roots(
        backend,
        repository=repository,
        run_directory=run_directory,
        reports_directory=reports_dir(),
        review_store_directory=review_store_root(),
        manifest_path=manifest_path,
        declared_write_paths=declared_write_paths,
        worktree=worktree,
    )


def _brief_digest(source: str | Path) -> str:
    """Return the sha256 of a brief's stored bytes.

    The digest joins a promoted row to the exact text its worker read, so it is
    taken over the file's bytes rather than a parsed form: a brief is its own
    authority text, and its spelling is part of what the worker was told. A
    brief that cannot be read is refused here, where the caller still has the
    path it named, rather than inside the worker.
    """
    path = Path(source).expanduser()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CrewError(f"the brief {source!r} is not readable: {exc}") from exc
    return hashlib.sha256(data).hexdigest()


def _store_brief(directory: Path, source: str) -> Path:
    """Copy a brief into the run directory and return the stored path.

    The brief is durable authority, so its copy lives beside the run's own
    record — under the configuration home, never inside the worktree — and the
    pointer names that copy, because the source path a coordinator handed in
    may be a scratch file no later reader can open. The suffix is kept so the
    stored file reads as the document it was.
    """
    destination = directory / f"brief{Path(source).suffix or '.md'}"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source).expanduser(), destination)
    except OSError as exc:
        raise CrewError(f"the brief {source!r} could not be stored: {exc}") from exc
    return destination


def _brief_record(node: TaskNode) -> dict[str, str] | None:
    """Return the pointer's brief block, or None for a plan-carried node."""
    if not node.brief.strip():
        return None
    return {
        "sha256": node.brief_sha256,
        "path": node.brief_path or node.brief,
        "source_path": node.brief,
    }


def _brief_text(node: TaskNode) -> str:
    """Return the brief text the composed prompt carries verbatim.

    The stored copy is read when dispatch has made one, so the prompt and the
    bytes the pointer names are the same document even if the source moved
    between the digest and the composition.
    """
    if not node.brief.strip():
        return ""
    source = node.brief_path or node.brief
    try:
        return Path(source).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise CrewError(f"the brief {node.brief!r} is not readable: {exc}") from exc
