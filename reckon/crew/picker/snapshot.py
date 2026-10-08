"""Adapt existing fleet measurements and hard gates to picker candidates."""

import hashlib
import json
import math
import time
from collections import Counter
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from reckon import _backends, budget, capabilities, capability, ledger
from reckon._timestamps import parse_utc
from reckon.crew import (
    budget_reset,
    lane_document,
    paid_lanes,
    recovery,
    resumption,
    routing,
)
from reckon.crew.dispatch import (
    DispatchPlan,
    _dispatch_lane_gate,
    _lane_worker_allowance,
)
from reckon.crew.node import NodeValidation

from .types import Candidate, PickRequest


@dataclass
class BudgetCandidate(Candidate):
    """A candidate with an explanation when its account window is unknown.

    ``stale`` and ``budget_age_s`` describe the age of the reading behind the
    candidate's figures. A stale reading keeps its figures rather than nulling
    them, so the age and flag travel beside the numbers they qualify.
    """

    budget_reason: str | None = None
    stale: bool | None = None
    budget_age_s: float | None = None


def recent_outcomes(
    records: list[dict[str, Any]],
    request: PickRequest,
    backend: str,
    model: str | None,
    *,
    now: datetime,
) -> dict[str, int]:
    cutoff = now - timedelta(days=14)
    counts: Counter[str] = Counter()
    for row in records:
        stamp = parse_utc(
            str(row.get("completed_at") or row.get("dispatched_at") or "")
        )
        if stamp is None or not cutoff <= stamp <= now:
            continue
        if (
            row.get("backend") != backend
            or (row.get("agent") or {}).get("model") != model
        ):
            continue
        if (
            row.get("role") != request.node.role
            or row.get("spec_level") != request.node.spec_level
        ):
            continue
        counts[str(row.get("gate") or "unknown")] += 1
    return {key: counts[key] for key in ("passed", "failed", "not-run", "unknown")}


def estimated_context_tokens(
    node: Any,
    repo: Path,
    *,
    backend_settings: dict[str, Any] | None = None,
    authority: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
) -> int:
    """A node's deterministic input estimate, independent of any lane window.

    The figure is the same measurement the context-fit verdict charges a node
    against -- its standing instruction chain plus the repository files its
    brief loads -- so a request-level estimate and a candidate's own verdict
    agree whenever the standing chain and the dispatch authority are shared.
    ``authority`` is threaded to the file census for the same reason the verdict
    threads it: dispatcher-granted landing paths are exempt only when the census
    can see the grant, so an estimate measured without the authority charges a
    node for its own granted fragment and reads larger than that node's own
    candidate blocks. The standing chain is read for the resolved backend's
    agent layout; a caller with no backend reads the harness-independent chain,
    which is what an unrouted estimate wants.

    ``routing.context_census`` caches the census on the stamps of the files it
    read, so a cold process reuses the figure those files already produced
    rather than re-counting the whole standing chain.
    """

    return routing.context_census(
        node,
        repo,
        backend_settings=backend_settings,
        authority=authority,
        root=root,
    )


def _p90(values: list[float]) -> float:
    """The 90th percentile of a non-empty sample, by nearest-rank."""

    ordered = sorted(values)
    index = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[index]


def _peak_summary(values: list[float]) -> dict[str, Any]:
    """One lane's peak-utilisation block from the measured values it holds."""

    if not values:
        return {
            "peak_utilisation_p50_pct": None,
            "peak_utilisation_p90_pct": None,
            "peak_utilisation_runs": None,
        }
    return {
        "peak_utilisation_p50_pct": round(median(values), 1),
        "peak_utilisation_p90_pct": round(_p90(values), 1),
        "peak_utilisation_runs": len(values),
    }


def _peak_input_utilisation(
    rows: list[dict[str, Any]], backend: str, *, now: datetime
) -> dict[str, Any]:
    """Peak input utilisation of recent passed runs on one lane.

    Only a passed run carries a completed worktree's measurement, and only a
    row whose throughput block recorded a percent contributes. An absent
    reading, or a lane with no such run, leaves every figure null rather than
    a measured zero -- a lane nothing observed is not a lane that used none of
    its window.
    """

    cutoff = now - timedelta(days=14)
    values: list[float] = []
    for row in rows:
        if row.get("gate") != "passed" or row.get("backend") != backend:
            continue
        stamp = parse_utc(
            str(row.get("completed_at") or row.get("dispatched_at") or "")
        )
        if stamp is None or not cutoff <= stamp <= now:
            continue
        block = row.get("throughput")
        value = (block or {}).get("input_utilisation_pct")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return _peak_summary(values)


def _candidates_scan(
    records: list[dict[str, Any]], request: PickRequest, *, now: datetime
) -> tuple[dict[tuple[str, str | None], dict[str, int]], dict[str, list[float]]]:
    """One pass over the ledger rows: every candidate's outcomes and peaks.

    ``recent_outcomes`` and ``_peak_input_utilisation`` each read the whole row
    list once per configured backend, so a pick with several backends walked the
    same rows many times. Both facts come from one row in one pass: the outcome
    counts keyed by (backend, model) and the peak-utilisation values keyed by
    backend. The elision is pure -- every figure equals the per-backend reader's
    own figure for the same rows.
    """

    cutoff = now - timedelta(days=14)
    counts: dict[tuple[str, str | None], Counter[str]] = {}
    peaks: dict[str, list[float]] = {}
    for row in records:
        stamp = parse_utc(
            str(row.get("completed_at") or row.get("dispatched_at") or "")
        )
        if stamp is None or not cutoff <= stamp <= now:
            continue
        backend = row.get("backend")
        if not isinstance(backend, str):
            continue
        if row.get("gate") == "passed":
            block = row.get("throughput")
            value = (block or {}).get("input_utilisation_pct")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                peaks.setdefault(backend, []).append(float(value))
        if (
            row.get("role") != request.node.role
            or row.get("spec_level") != request.node.spec_level
        ):
            continue
        model = (row.get("agent") or {}).get("model")
        counts.setdefault((backend, model), Counter())[
            str(row.get("gate") or "unknown")
        ] += 1
    return counts, peaks


def _outcome_counts(counts: Counter[str]) -> dict[str, int]:
    """The fixed outcome vocabulary, absent gates reading zero."""

    return {key: counts[key] for key in ("passed", "failed", "not-run", "unknown")}


def _context_block(
    context: dict[str, Any] | None,
    node_tokens: int,
    utilisation: dict[str, Any],
) -> dict[str, Any]:
    """One candidate's weighable context facts, null where unknown.

    ``window_tokens`` is the gating window the context-fit verdict compares an
    estimate against; ``estimated_tokens`` is this node measured against that
    candidate (the verdict's own per-backend estimate where one exists, else
    the once-per-pick figure). ``headroom_pct`` is the share of the window the
    estimate leaves free. A lane declaring no window carries a null window and
    no headroom, never a zero that would read as a spent window.
    """

    if context is None:
        window_tokens = None
        estimated_tokens: int | None = node_tokens
    else:
        window_tokens = context.get("window_tokens")
        estimated_tokens = context.get("estimated_tokens")
    headroom_pct = None
    if window_tokens and estimated_tokens is not None:
        headroom_pct = round(
            100.0 * (window_tokens - estimated_tokens) / window_tokens, 1
        )
    return {
        "window_tokens": window_tokens,
        "estimated_tokens": estimated_tokens,
        "headroom_pct": headroom_pct,
        **utilisation,
    }


def _lane(
    backend: dict[str, Any],
    session: str,
    documents: dict[str, Any] | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    # One lane document per pick: ``documents`` is the pick-scoped cache the
    # candidate loop threads through, so a backend whose document another
    # backend also names reads it once. The parse goes through the routing
    # helper that reads the same published document ``local_lane_load`` reports.
    document = routing.pick_lane_document(backend, {} if documents is None else documents)
    reading = lane_document.read_lane_document(document)
    # The shared allowance helper owns session/new-session/global precedence.
    admission = lane_document.read_lane_admission(document)
    allowance = _lane_worker_allowance(document, session=session)
    slots = allowance.get("allowance")
    if reading["stale"]:
        slots = None
    congestion = {
        key: reading[key] for key in ("running", "waiting", "admission_verdict")
    }
    congestion["stale"] = reading["stale"]
    congestion["slots_state"] = admission["state"]
    return slots, congestion, allowance


# A serving verdict read from a lane's own endpoints document, translated into
# the picker's availability vocabulary. A lane the document shows down cannot
# serve, so its verdict is a hard exclusion; a verdict the reader cannot
# establish is unknown, never a claim the lane is down.
_SERVING_AVAILABILITY = {
    "serving": "served",
    "not-serving": "unavailable",
    "mismatch": "unavailable",
}


def _serving_observation(backend: dict[str, Any]) -> dict[str, Any] | None:
    """A backend's availability read from the document it publishes.

    Only a backend declaring an ``endpoints_document`` answers here. The
    verdict is read with the same reader ``reckon flight`` reports its serving
    column with, so a lane declared serving to that surface is served here.
    The document is a local file and is never read over the network.
    """
    if not backend.get("endpoints_document"):
        return None
    from reckon import flight

    reading = flight._probe_serving(backend)
    verdict = str(reading.get("serving") or "unknown")
    return {
        "status": _SERVING_AVAILABILITY.get(verdict, "unknown"),
        "detail": str(reading.get("serving_detail") or ""),
    }


def _cached_observation(
    project: str,
    backend_name: str,
    config: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """A cached serving observation still within its declared shelf life.

    An absent cache, and one whose age cannot be established or has passed the
    declared shelf life, return None. None of those is evidence a lane cannot
    serve, only that nothing has observed it recently — the caller reads that
    absence as unknown rather than as a refusal.
    """
    observation = resumption._read_lane_probe_cache(project, backend_name)
    if not observation:
        return None
    observed = resumption._parse_stamp(observation.get("observed_at"))
    if observed is None:
        return None
    shelf_minutes = float(
        budget.policy(config).get(
            "evidence_shelf_life_minutes", budget.DEFAULT_SHELF_LIFE_MINUTES
        )
    )
    if (now - observed).total_seconds() > shelf_minutes * 60.0:
        return None
    return observation


def _fit(
    request: PickRequest,
    name: str,
    backend: dict[str, Any],
    repo: Path,
    *,
    verdict_inputs: dict[str, Any] | None = None,
    context_out: dict[str, Any] | None = None,
    authority: Mapping[str, Any] | None = None,
) -> list[str]:
    execution = capability.assess_execution_fit(
        request.node.done_when,
        role=request.node.role,
        execution_capable=backend.get("execution_capable"),
    )
    # The authority travels into the verdict so its context estimate charges the
    # same granted-path set the request-level estimate uses. Without it the
    # verdict exempts a dispatcher-granted landing fragment the request-level
    # estimate (measured with the authority) would also exempt, and the two
    # figures describing one node disagree.
    resolution = DispatchPlan(
        run_id="",
        backend=name,
        launch=backend.get("launch", ""),
        backend_settings=backend,
        node=request.node,
        budget_ceiling="",
        validation=NodeValidation(ok=True),
        execution_fit=execution,
        authority=dict(authority) if authority is not None else None,
    )
    reasons = (
        [] if execution.allowed else ["execution-fit: " + execution.refusal_detail()]
    )
    # The competence verdict measures this backend's context window on every
    # path that reaches its context check and returns the measurement under
    # ``context``. Read it there rather than measuring the same window a second
    # time: the measurement reads the instruction chain, whose repository lookup
    # shells out to git, so a second call costs a second subprocess on the
    # pick's critical path under the dispatch bound.
    competence = routing._competence_verdict(
        resolution=resolution,
        project=request.project,
        repo=repo,
        verdict_inputs=verdict_inputs,
    )
    context = competence.get("context")
    if context is None:
        context = routing._context_fit_verdict(resolution=resolution, repo=repo)
    if context and not context["allowed"]:
        reasons.append("context-fit: " + context["reason"])
    if context and request.estimated_context > context["window_tokens"]:
        reasons.append(
            f"context-fit: requested {request.estimated_context} tokens exceeds {context['window_tokens']}"
        )
    if not competence["allowed"]:
        reasons.append("competence: " + competence["reason"])
    # The verdict measured this backend's window once; hand the measurement to
    # the caller rather than measuring it a second time for the candidate's
    # context block.
    if context_out is not None:
        context_out[name] = context
    return reasons


def _budget_view_input_paths(config: Mapping[str, Any]) -> list[str]:
    """Every non-ledger file one budget view reads, deduplicated for its stamp.

    The paid-lanes document and the banked-reset record are read directly; a
    lane document is included for every configured backend that publishes one,
    so a backend's own load document is part of the figure's input stamp.
    """

    paths = [str(paid_lanes.document_path()), str(budget_reset.state_path())]
    for backend in (config.get("backends") or {}).values():
        if not isinstance(backend, Mapping):
            continue
        lane_path = backend.get("lane_document")
        if lane_path:
            paths.append(str(Path(str(lane_path)).expanduser()))
    return sorted(set(paths))


def _budget_view_request_key(
    project: str,
    config: Mapping[str, Any],
    repo: Path,
    records: list[dict[str, Any]],
    *,
    cached_only: bool,
) -> dict[str, Any]:
    """The selecting fields of one budget view, hashed into its cache name.

    Two calls sharing every field read the same files and the same records and
    share one entry; a changed project, repository, configuration, record count
    or cache mode selects a different entry rather than serving a figure built
    for another request. The records themselves are keyed by the ledger stamp in
    the value's stamp rather than re-hashed here, because the committed rows a
    caller passes are exactly the rows the ledger files carry.
    """

    return {
        "project": project,
        "repo": str(Path(repo).resolve()),
        "cached_only": cached_only,
        "records": len(records) if records is not None else None,
        "config": hashlib.sha256(
            json.dumps(config, sort_keys=True, default=str).encode()
        ).hexdigest(),
    }


def _compose_budget_report(
    project: str,
    config: dict[str, Any],
    repo: Path,
    records: list[dict[str, Any]],
    moment: datetime,
    *,
    cached_only: bool,
) -> dict[str, Any]:
    """Compose one dated live budget view at a stated moment."""

    windows = budget.recorded_windows(project, config, root=repo, records=records)
    document = paid_lanes.read_document()
    published = paid_lanes.document_windows(document, moment=moment)
    # Preflight prefers fresh published figures to recorded ones. Preserve an
    # older published figure only where no run has recorded that account, so
    # the candidate can report its age and stale reason instead of an absence.
    for account, reading in published.items():
        windows.setdefault(account, reading)
    # Every configured backend is budget-probed; nothing is filtered by name, so a
    # refusal can only come from a live serving observation, never a fixed list.
    probeable = list(config.get("backends", {}))
    # The budget view composes state_for and group_pace, including the account's
    # operative window. Consuming its verdict keeps every clock in one authority.
    report = budget.preflight(
        project,
        config,
        root=repo,
        backends=probeable,
        windows=windows,
        records=records,
        now=moment,
        document=document,
        **({"probe_runner": lambda _: {}} if cached_only else {}),
    )
    by_backend = {entry["backend"]: entry for entry in report["backends"]}
    shelf_seconds = budget.policy(config)["evidence_shelf_life_minutes"] * 60
    moment = parse_utc(report["checked_at"])
    for group in report["groups"]:
        allowance = group.get("allowance") or {}
        if allowance.get("state") != budget.OBSERVED:
            for name in group["members"]:
                by_backend[name]["state"]["detail"] = (
                    f"no recorded account-window reading for budget group {group['group']}"
                )
            continue
        observed = parse_utc(str(allowance.get("observed_at") or ""))
        if observed is None or moment is None:
            continue
        stale = (moment - observed).total_seconds() > shelf_seconds
        source = (
            "account-surface" if group.get("source") == "account-surface" else "ledger"
        )
        for name in group["members"]:
            state = by_backend[name]["state"]
            state.update(
                source=source,
                observed_at=observed.isoformat(),
                expired=stale,
                headroom="known",
                utilisation_pct=allowance["utilisation"] * 100,
                burn_multiple=allowance["burn_multiple"],
                resets_at=allowance["resets_at"],
                detail=(
                    "recorded account-window reading is stale"
                    if stale
                    else state.get("detail", "")
                ),
            )
    report["summary"] = budget.summary(report)
    return report


def _reage_budget_report(
    value: Mapping[str, Any],
    moment: datetime,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Refresh a budget view's time-derived fields to a fresh moment.

    The cached report holds the file-derived composition of a budget view. Its
    ages, elapsed fractions, seconds-to-reset and stale flags all describe the
    moment the view was built. A view reused at the moment it was built is
    returned unchanged, so a cached figure equals the figure the same files
    produce; a view reused later is advanced by the interval since that moment
    rather than served stale. The shelf life a stale flag is judged against is
    the configured one, so a reading that ages past it flips on the call that
    crosses it rather than only on the next rebuild.
    """

    report = value["report"]
    built = parse_utc(str(value.get("built_at") or ""))
    delta = (moment - built).total_seconds() if built is not None else 0.0
    # A caller holds whatever this returns. On the zero-delta path it is the
    # cache's own report, so a mutation would rewrite the stored composition and
    # the next read of the same value would serve the mutation. Copy here too,
    # normalised the same way the advancing path normalises, so a reused view
    # equals the figures the same files produce and never aliases the cache.
    report = json.loads(json.dumps(report))
    if delta == 0.0:
        return report
    report["checked_at"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    shelf_seconds = budget.policy(config)["evidence_shelf_life_minutes"] * 60
    for entry in report["backends"]:
        state = entry["state"]
        remaining = state.get("seconds_until_reset")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            state["seconds_until_reset"] = max(0, int(remaining - delta))
        observed = parse_utc(str(state.get("observed_at") or ""))
        if observed is not None and state.get("headroom") == "known":
            state["expired"] = (moment - observed).total_seconds() > shelf_seconds
    waits = [
        entry["state"]["seconds_until_reset"]
        for entry in report["backends"]
        if entry.get("held")
        and isinstance(entry["state"].get("seconds_until_reset"), int)
    ]
    report["resume_after_seconds"] = min(waits) if waits else None
    for group in report["groups"]:
        for clock in (group.get("clocks") or {}).values():
            age = clock.get("age_seconds")
            if isinstance(age, (int, float)) and not isinstance(age, bool):
                clock["age_seconds"] = age + delta
        allowance = group.get("allowance") or {}
        elapsed = allowance.get("elapsed_hours")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            elapsed = elapsed + delta / 3600.0
            allowance["elapsed_hours"] = elapsed
            window_minutes = allowance.get("window_minutes")
            if window_minutes:
                allowance["elapsed_fraction"] = min(
                    1.0, elapsed / (float(window_minutes) / 60.0)
                )
        runway = ((group.get("bar") or {}).get("runway")) or {}
        age = runway.get("age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            runway["age_seconds"] = age + delta
    report["summary"] = budget.summary(report)
    return report


def budget_view(
    project: str,
    config: dict[str, Any],
    repo: Path,
    records: list[dict[str, Any]],
    *,
    cached_only: bool = False,
    now: datetime | None = None,
    cache_root: str | Path | None = None,
) -> dict[str, Any]:
    """Compose one dated live budget view, its file-derived part cached.

    The composition reads the paid-lanes document, the banked-reset record, the
    lane documents and the ledger files, and preflight costs a few hundred
    milliseconds against live state — every one of which a dispatch paid to
    rebuild a figure the same files had already produced. The file-derived
    report is cached on the stamps of those files, so an unchanged set of files
    reuses it, and every time-derived field is recomputed on each call so a
    cached entry never serves a stale age. ``now`` and ``cache_root`` exist so a
    caller can pin the clock and the cache location.
    """

    moment = datetime.now(UTC) if now is None else now
    if cached_only:
        # Budget preflight may refresh an undated refusal with a serving request.
        # Disable that refresh; candidate availability comes from the cache below.
        config = {
            **config,
            "backends": {
                name: {**backend, "budget_check": False}
                for name, backend in config.get("backends", {}).items()
            },
        }

    def compose() -> dict[str, Any]:
        return _compose_budget_report(
            project, config, repo, records, moment, cached_only=cached_only
        )

    def build() -> dict[str, Any]:
        return {
            "report": compose(),
            "built_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "paths": _budget_view_input_paths(config),
            "ledger": [project, str(Path(repo))],
        }

    def stamp_of(value: Mapping[str, Any]) -> list[Any]:
        stamps: list[Any] = [
            [path, capabilities.file_stamp(path)]
            for path in (value.get("paths") or [])
        ]
        project_name, root = value["ledger"]
        stamps.append(["ledger", ledger.index_stamp(project_name, root)])
        return stamps

    try:
        request_key = _budget_view_request_key(
            project, config, repo, records, cached_only=cached_only
        )
        name = "budget-view-" + hashlib.sha256(
            json.dumps(request_key, sort_keys=True, default=str).encode()
        ).hexdigest()
        value = capabilities.cached_pick_input_stamped(
            name, request_key, stamp_of, build, root=cache_root
        )
        return _reage_budget_report(value, moment, config)
    except Exception:  # noqa: BLE001 - the cache is an optimisation, not the authority
        # A cache read or its stamping failing must never surface as this input
        # failing: the picker records a failure against the input that raised,
        # so a neighbouring input's failure would be misattributed to this one.
        # Fall back to the direct composition, which is what the caller had
        # before the cache and which the picker already tolerates.
        return compose()


def candidates(
    request: PickRequest,
    config: dict[str, Any],
    repo: Path,
    *,
    records: list[dict[str, Any]] | None = None,
    availability_cache: dict[tuple[str, str | None], dict[str, Any]] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    verdict_inputs: dict[str, Any] | None = None,
    cached_only: bool = False,
    authority: Mapping[str, Any] | None = None,
    estimate_out: MutableMapping[str, Any] | None = None,
) -> list[Candidate]:
    """Read a fresh snapshot; never dispatch or change routing configuration."""
    now = datetime.now(UTC)
    rows = ledger.runs(request.project, root=repo) if records is None else records
    shared = (
        verdict_inputs
        if verdict_inputs is not None
        else routing.shared_verdict_inputs(request.project, repo)
    )
    view = (
        budget_snapshot
        if budget_snapshot is not None
        else budget_view(request.project, config, repo, rows, cached_only=cached_only)
    )
    # The node's estimate is independent of the candidate. Parsing its plan
    # once keeps a backend census from multiplying identical repository reads.
    shared = {
        **shared,
        "node_estimate": routing._estimated_hours(repo, request.project, request.node),
    }
    # One context estimate per pick. A caller that measured it before the pick --
    # dispatch does, so the figure it weighed was a real census and its stage
    # records the expensive computation rather than the rendered-length division
    # -- leaves it on the request; reuse it here rather than measuring a second
    # time. With no figure in hand the estimate is measured here and its own
    # duration reported, so the pick's stage can time the measurement wherever
    # it ran. Peak utilisation is read once per lane rather than once per row,
    # and the authority is threaded here and into every candidate's verdict so
    # the request-level figure and the block a candidate carries are one
    # estimate, not two measured against different granted-path sets.
    estimate_started = time.perf_counter()
    if request.estimated_context > 0:
        node_context_tokens = request.estimated_context
        estimate_ms: float | None = None
    else:
        node_context_tokens = estimated_context_tokens(
            request.node, repo, authority=authority
        )
        estimate_ms = round((time.perf_counter() - estimate_started) * 1000, 3)
    if estimate_out is not None:
        estimate_out["tokens"] = node_context_tokens
        estimate_out["ms"] = estimate_ms
    # One pass over the rows yields every backend's outcome counts and peak
    # utilisation, and one document cache is threaded through the loop so each
    # lane document is parsed once per pick.
    outcome_counts, peak_values = _candidates_scan(rows, request, now=now)
    lane_utilisation = {
        name: _peak_summary(peak_values.get(name, []))
        for name in config.get("backends", {})
    }
    lane_documents: dict[str, Any] = {}
    budget_by_backend = {row["backend"]: row for row in view["backends"]}
    group_by_backend = {
        member: group for group in view["groups"] for member in group["members"]
    }
    # A review never runs on a backend the flight configuration withdraws from
    # review routing, so the candidate is removed here rather than offered for
    # Jev to weigh: an exclusion is a rule about what cannot run, not a
    # pressure signal. Read through the recovery helper so the key name and its
    # parsing have one source of truth.
    review_excluded: set[str] = set()
    if request.node.role == "review":
        review_excluded = recovery._review_excluded_backends(config)
    result = []
    for name in config.get("backends", {}):
        local = name == config.get("local_backend")
        _, backend = routing.resolve_role_override(
            config,
            request.node.role,
            request.node.spec_level,
            name,
            capability_class=str(request.capability.get("class") or ""),
        )
        model = backend.get("model")
        reasons = []
        if name in review_excluded:
            reasons.append("review-excluded-backend")
        # A routed CLI dispatch cannot execute an in-harness backend: nothing
        # spawns it, because only a coordinator attaching the task it already
        # runs can bind the harness, so the picker removes it rather than
        # offering it for Jev to weigh. The launch is read from the resolved
        # backend, so a role overlay that changes the launch is honoured.
        if backend.get("launch") == recovery.IN_HARNESS_LAUNCH:
            reasons.append("in-harness-backend")
        verdict = budget_by_backend.get(name) or {
            "held": False,
            "state": budget.BudgetState(name).as_dict(),
        }
        state = verdict["state"]
        group = group_by_backend.get(name)
        group_allowance = (group or {}).get("allowance") or {}
        # A reading is carried whether or not it has passed its shelf life: an
        # old figure with its age and stale flag lets Jev weigh it, where a null
        # figure hides the account exactly when it has been idle. Only a reading
        # that is genuinely absent leaves the figures null, with its
        # budget_reason naming why.
        reading_known = state.get("headroom") == "known" and (
            group is None
            or group_allowance.get("state") == budget.OBSERVED
            or group_allowance.get("effective_limit") is not None
        )
        budget_facts = state if reading_known else {}
        stale = bool(state.get("expired")) if reading_known else None
        # The ceiling gate acts on a fresh reading alone: an old figure is
        # offered for Jev to weigh, never turned into a hard refusal.
        fresh = reading_known and not state.get("expired")
        utilisation = state.get("utilisation_pct") if fresh else None
        ceiling = budget.policy(config)["utilisation_ceiling_pct"]
        if utilisation is not None and utilisation >= ceiling:
            reasons.append(f"budget-ceiling: {utilisation:g}% at or above {ceiling:g}%")
        gate = _dispatch_lane_gate(backend)
        if gate["state"] in {"paused", "unreadable"}:
            reasons.append("lane-gate: " + gate["state"])
        slots, congestion, _ = (
            _lane(backend, request.session, lane_documents)
            if local
            else (None, None, {})
        )
        # Already-excluded candidates need no repository census or serving probe.
        context_block = None
        if not reasons:
            contexts: dict[str, Any] = {}
            reasons.extend(
                _fit(
                    request,
                    name,
                    backend,
                    repo,
                    verdict_inputs=shared,
                    context_out=contexts,
                    authority=authority,
                )
            )
            context_block = _context_block(
                contexts.get(name), node_context_tokens, lane_utilisation.get(name, {})
            )
        if reasons:
            availability = "not-probed"
        else:
            cache_key = (name, model)
            observation = (availability_cache or {}).get(cache_key)
            if observation is None:
                # A lane publishing what it serves answers from that document,
                # which is authoritative for it and costs no request.
                observation = _serving_observation(backend)
            if observation is None and cached_only:
                observation = _cached_observation(
                    request.project, name, config, now=now
                )
            if observation is None:
                if cached_only:
                    # A cached pick issues no request. An absent or expired
                    # observation is unknown, never a claim the lane is down,
                    # so the candidate stays offered for Jev to weigh.
                    observation = {"status": "unknown"}
                else:
                    serving_backend = {
                        **config["backends"][name],
                        "model": model,
                        "effort": backend.get("effort"),
                    }
                    observation = resumption.probe_lane_availability(
                        request.project, name, serving_backend, root=repo
                    )
                    if availability_cache is not None:
                        availability_cache[cache_key] = observation
            availability = str(observation.get("status") or "unknown")
            if availability in {"refused", "unavailable", "logged-out"}:
                reasons.append("availability: " + availability)
        try:
            family = "local" if local else _backends.dialect_for(backend).name
        except _backends.BackendError:
            family = str(backend.get("launch") or name)
        reset = parse_utc(str(budget_facts.get("resets_at") or ""))
        days_to_reset = (
            max(0.0, (reset - now).total_seconds() / 86400) if reset else None
        )
        observed = parse_utc(str(state.get("observed_at") or ""))
        budget_age_s = (
            max(0.0, (now - observed).total_seconds())
            if reading_known and observed is not None
            else None
        )
        budget_reason = None
        if group is not None and not budget_facts:
            budget_reason = str(state.get("detail") or "") or (
                "no recorded account-window reading for the candidate's budget group"
            )
        result.append(
            BudgetCandidate(
                backend=name,
                family=family,
                model=model,
                effort=backend.get("effort"),
                local=local,
                availability=availability,
                utilisation_pct=budget_facts.get("utilisation_pct"),
                burn_multiple=budget_facts.get("burn_multiple"),
                pace_allowance=(
                    group_allowance.get("effective_limit") if budget_facts else None
                ),
                resets_at=budget_facts.get("resets_at"),
                days_to_reset=days_to_reset,
                worker_slots=slots,
                congestion=congestion,
                outcomes=_outcome_counts(
                    outcome_counts.get((name, model), Counter())
                ),
                reasons=reasons,
                budget_reason=budget_reason,
                stale=stale,
                budget_age_s=budget_age_s,
                context=context_block,
            )
        )
    return result
