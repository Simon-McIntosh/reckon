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

from reckon import budget, capabilities, capability, flight, ledger
from reckon._timestamps import parse_utc
from reckon.crew import (
    budget_group,
    budget_lift,
    budget_reset,
    dispatch_picker,
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
from reckon.crew.review import REVIEW_DIMENSIONS

from . import lane_context
from .types import Candidate, PickRequest


@dataclass
class BudgetCandidate(Candidate):
    """A candidate with an explanation when its account window is unknown.

    ``stale`` and ``budget_age_s`` describe the age of the reading behind the
    candidate's figures. A stale reading keeps its figures rather than nulling
    them, so the age and flag travel beside the numbers they qualify.
    """

    budget_reason: str | None = None
    budget_source: str | None = None
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


def _requirement_level(value: Any) -> str | None:
    """A recorded requirement level, or None when it carries no value."""

    if value is None or value == "":
        return None
    return str(value)


def _recorded_requirement(
    row: Mapping[str, Any],
) -> tuple[str | None, str | None] | None:
    """The (reasoning, risk) a row recorded, or None when it recorded none.

    The requirement a dispatch weighed is stored under
    ``node_definition.capability``; a row written before that recording, or one
    whose capability carries neither level, recorded no requirement and reads as
    ``None`` so it is counted as unrecorded rather than matched by default.
    """

    definition = row.get("node_definition")
    if not isinstance(definition, Mapping):
        return None
    capability = definition.get("capability")
    if not isinstance(capability, Mapping):
        return None
    requirements = capability.get("requirements")
    if not isinstance(requirements, Mapping):
        return None
    reasoning = _requirement_level(requirements.get("reasoning"))
    risk = _requirement_level(requirements.get("risk"))
    if reasoning is None and risk is None:
        return None
    return (reasoning, risk)


def _picked_backend_away(
    row: Mapping[str, Any],
) -> tuple[str, str | None] | None:
    """The backend Jev picked for a row its lineage then moved to another lane.

    A row is a move when Jev rendered a pick for it -- ``picker_selection`` names
    the backend Jev chose -- and the row's lineage records a lane change away
    from that backend, so the run landed on a lane Jev did not pick and its
    ``backend`` is no longer the chosen one. The chosen backend and its model are
    returned so the move can be credited against the candidate Jev chose; a row
    with no pick, or one whose lineage never moved it off its pick, is not a row.
    A row with a lane change but no pick (an explicit route that was later moved)
    has no pick to count against and is not a move.
    """

    selection = row.get("picker_selection")
    if not isinstance(selection, Mapping):
        return None
    chosen = selection.get("backend")
    if not isinstance(chosen, str) or not chosen:
        return None
    if row.get("backend") == chosen:
        return None
    lineage = row.get("lineage")
    if not isinstance(lineage, Mapping):
        return None
    changes = lineage.get("lanes")
    if not isinstance(changes, (list, tuple)) or not changes:
        return None
    for change in changes:
        if isinstance(change, Mapping) and change.get("from_backend") == chosen:
            model = selection.get("model")
            return chosen, model if isinstance(model, str) else None
    return None


def _request_requirement(request: PickRequest) -> tuple[str | None, str | None] | None:
    """The (reasoning, risk) the request names, or None when it names none."""

    requirements = (request.capability or {}).get("requirements")
    if not isinstance(requirements, Mapping):
        return None
    reasoning = _requirement_level(requirements.get("reasoning"))
    risk = _requirement_level(requirements.get("risk"))
    if reasoning is None and risk is None:
        return None
    return (reasoning, risk)


def _candidates_scan(
    records: list[dict[str, Any]],
    request: PickRequest,
    *,
    now: datetime,
    key_counts: MutableMapping[tuple[str, str], dict[str, Counter[str]]] | None = None,
    attempts_out: MutableMapping[str, int] | None = None,
) -> tuple[
    dict[tuple[str, str | None], dict[str, int]],
    dict[str, list[float]],
    dict[tuple[str, str | None], dict[str, Any]] | None,
    dict[tuple[str, str | None], Counter[str]],
    dict[tuple[str, str | None], int],
]:
    """One pass over the ledger rows: every candidate's outcomes and peaks.

    ``recent_outcomes`` and ``_peak_input_utilisation`` each read the whole row
    list once per configured backend, so a pick with several backends walked the
    same rows many times. Both facts come from one row in one pass: the outcome
    counts keyed by (backend, model) and the peak-utilisation values keyed by
    backend. The elision is pure -- every figure equals the per-backend reader's
    own figure for the same rows.

    The same pass also conditions the outcome counts on the requirement each row
    recorded: rows whose recorded (reasoning, risk) equals the request's feed a
    per-candidate block, rows that recorded no requirement count under
    ``unrecorded``, and rows that recorded a different requirement count in
    neither. The block's review-total and wall-time medians are read over the
    matched rows in the same walk, so no figure here costs a second pass. A
    matched row whose review scored every dimension also feeds one list per
    dimension beside the reviews list, and the block counts those rows, so the
    dimension medians rest on one shared sample. The
    launches a matched row took are collected in the same walk too: the row's
    ``attempt`` joins an ``attempts`` list, and a passed row's first-launch
    count is tallied beside it while the row's gate and attempt are still
    together. A request that names no requirement has no difficulty to read
    history at, so the at-requirement block is None and none is built.

    A coordinator's move is read in the same walk. A row whose lineage records a
    lane change away from the backend its ``picker_selection`` chose -- a move --
    is kept out of the receiving candidate's pooled counts and out of its
    at-requirement block, and its gate is tallied under the receiving candidate's
    ``moved_in``. The candidate Jev chose gains one ``moved_off`` for such a
    pick. Both move figures are counted at one difficulty: a moved row recorded
    at a requirement other than the request's counts in neither ``moved_in`` nor
    ``moved_off``, so the receiving candidate's ``moved_in`` always has a
    matching ``moved_off`` on the candidate Jev chose. A request that names no
    requirement has no difficulty to read at, so both figures count every moved
    row. A row with a lane change but no pick (an explicit route that was later
    moved) is not a move and counts under its final lane as before.

    The same walk also tallies the gate vocabulary under the ``lane`` and
    ``model_key`` the row's identity carries, broken down by the model id the
    row ran as. A lane model key re-pointed to a new model id loses its whole
    record when the tally is keyed by the exact id, so this second tally is
    what a candidate reads its key's earlier generations from. The identity
    comes from :func:`ledger.normalize_identity`, so a recorded pair is
    authoritative and a row is never matched by today's lane default: the
    tally is keyed by what the run recorded, never by what its id resolves to
    now. A caller wanting those tallies passes ``key_counts`` and receives them
    in it, the way a caller wanting the context estimate passes ``context_out``
    to ``_fit``; a caller that passes none pays nothing for the tally.

    The same walk counts the node's own attempts -- and, like the key tally,
    does so before any window or role filter, because an attempt happened
    whether or not its row still falls inside the outcome window. A caller that
    passes ``attempts_out`` receives the count under ``"attempts"``; the count
    is taken over every row, so it equals a separate count of the rows matching
    ``node`` and ``plan`` while costing no second walk.
    """

    cutoff = now - timedelta(days=14)
    request_requirement = _request_requirement(request)
    counts: dict[tuple[str, str | None], Counter[str]] = {}
    key_record: MutableMapping[tuple[str, str], dict[str, Counter[str]]] = (
        key_counts if key_counts is not None else {}
    )
    peaks: dict[str, list[float]] = {}
    at_requirement: dict[tuple[str, str | None], dict[str, Any]] | None = (
        {} if request_requirement is not None else None
    )
    moved_in: dict[tuple[str, str | None], Counter[str]] = {}
    moved_off: dict[tuple[str, str | None], int] = {}
    for row in records:
        if attempts_out is not None and (
            row.get("node") == request.node.id and row.get("plan") == request.node.plan
        ):
            attempts_out["attempts"] = attempts_out.get("attempts", 0) + 1
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
        key = (backend, model)
        gate = str(row.get("gate") or "unknown")
        away = _picked_backend_away(row)
        if away is not None:
            # The move's outcome belongs to the pick Jev made, not to the lane
            # it landed on: keep it out of the receiving candidate's pooled and
            # at-requirement counts and tally it under moved_in. Both move
            # figures are counted at the request's requirement: a moved row
            # recorded at another requirement counts in neither, so the pair
            # reads at one difficulty, and a request naming no requirement
            # counts every move.
            recorded = _recorded_requirement(row)
            if request_requirement is None or recorded == request_requirement:
                moved_in.setdefault(key, Counter())[gate] += 1
                moved_off[away] = moved_off.get(away, 0) + 1
            continue
        counts.setdefault(key, Counter())[gate] += 1
        identity = ledger.normalize_identity(row)
        identity_lane = identity.get("lane")
        identity_key = identity.get("model_key")
        if isinstance(identity_lane, str) and isinstance(identity_key, str):
            recorded_model = (row.get("agent") or {}).get("model")
            per_model = key_record.setdefault((identity_lane, identity_key), {})
            per_model.setdefault(
                recorded_model if isinstance(recorded_model, str) else "", Counter()
            )[gate] += 1
        if at_requirement is None:
            continue
        entry = at_requirement.setdefault(
            key,
            {
                "counts": Counter(),
                "unrecorded": 0,
                "reviews": [],
                "walls": [],
                "attempts": [],
                "passed_first_attempt": 0,
                "reviewed": 0,
                "dimension_scores": {name: [] for name in REVIEW_DIMENSIONS},
            },
        )
        recorded = _recorded_requirement(row)
        if recorded is None:
            entry["unrecorded"] += 1
            continue
        if recorded != request_requirement:
            continue
        entry["counts"][gate] += 1
        attempt = row.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
            entry["attempts"].append(attempt)
            if gate == "passed" and attempt == 1:
                entry["passed_first_attempt"] += 1
        review = ledger.review_block(row)
        total = review.get("total") if review is not None else None
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            entry["reviews"].append(float(total))
        scores = review.get("scores") if review is not None else None
        if _fully_scored(scores):
            entry["reviewed"] += 1
            for name in REVIEW_DIMENSIONS:
                entry["dimension_scores"][name].append(float(scores[name]))
        wall = row.get("wall_seconds")
        if isinstance(wall, (int, float)) and not isinstance(wall, bool):
            entry["walls"].append(float(wall))
    return counts, peaks, at_requirement, moved_in, moved_off


def _outcome_counts(counts: Counter[str]) -> dict[str, int]:
    """The fixed outcome vocabulary, absent gates reading zero."""

    return {key: counts[key] for key in ("passed", "failed", "not-run", "unknown")}


def _prior_block(
    entry: Mapping[str, Counter[str]] | None, model: str | None
) -> dict[str, Any] | None:
    """One candidate's prior block: its key's record under an earlier model id.

    ``entry`` is the key's tallies broken down by the model id each run was
    recorded as. The candidate's own model id falls out -- its runs are already
    the candidate's own outcomes -- and every other id's runs are summed. The
    block names those ids so a reader can see which earlier generation the
    prior rests on. None when no run was recorded under the key at another id,
    so a key whose record is entirely its current id carries no prior and its
    candidate renders exactly as it did before prior blocks existed.
    """

    if not entry:
        return None
    own = model or ""
    counts: Counter[str] = Counter()
    ids: list[str] = []
    for recorded_id, tally in entry.items():
        if recorded_id == own or not recorded_id:
            continue
        counts.update(tally)
        ids.append(recorded_id)
    if not any(counts.values()):
        return None
    return {**_outcome_counts(counts), "model_ids": sorted(ids)}


def _fully_scored(scores: Any) -> bool:
    """Whether a review's scores cover every dimension with a real number.

    A review scored before a dimension existed, or one the parser recorded with a
    dimension absent, is not fully scored: its total still counts, but its
    per-dimension figures are taken over a different sample, so it feeds the
    dimension medians not at all. A bool is excluded though it is an ``int``,
    because a ``True`` in a score field is a recorded flag, not a score.
    """

    if not isinstance(scores, Mapping):
        return False
    return all(
        isinstance(scores.get(name), (int, float))
        and not isinstance(scores.get(name), bool)
        for name in REVIEW_DIMENSIONS
    )


def _requirement_block(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """One candidate's gate history at the request's requirement.

    The block restates the outcome vocabulary over the rows whose recorded
    requirement equals the request's, adds ``unrecorded``, the count of rows
    that recorded no requirement, and carries the median review total and wall
    seconds over the matched rows. It also carries ``reviewed``, how many
    matched rows a review scored on every dimension, and
    ``review_dimensions_median``, each dimension's median over exactly those
    rows. A dimension median needs the full sample, so a row that scored fewer
    than every dimension feeds neither the medians nor ``reviewed``, though its
    recorded total still counts toward ``review_total_median``. It also carries
    ``attempts_median``, the median of the launches those rows took, and
    ``passed_first_attempt``, how many of them passed on their first launch, so
    a pass that needed resumes is not weighed as a pass on the first try. A
    candidate with no matched row reads zero gate counts, null medians and a
    zero first-attempt count, never a median over an empty sample.
    """

    entry = entry or {}
    counts = entry.get("counts") or Counter()
    reviews = entry.get("reviews") or []
    walls = entry.get("walls") or []
    attempts = entry.get("attempts") or []
    dimensions = entry.get("dimension_scores") or {}
    reviewed = int(entry.get("reviewed") or 0)
    return {
        **_outcome_counts(counts),
        "unrecorded": int(entry.get("unrecorded") or 0),
        "reviewed": reviewed,
        "review_total_median": round(median(reviews), 1) if reviews else None,
        "review_dimensions_median": (
            {
                name: round(median(dimensions.get(name) or []), 1)
                for name in REVIEW_DIMENSIONS
            }
            if reviewed
            else None
        ),
        "wall_seconds_median": round(median(walls), 1) if walls else None,
        "attempts_median": round(median(attempts), 1) if attempts else None,
        "passed_first_attempt": int(entry.get("passed_first_attempt") or 0),
    }


def _context_block(
    context: dict[str, Any] | None,
    node_tokens: int,
    utilisation: dict[str, Any],
) -> dict[str, Any]:
    """One candidate's weighable context block, reconciled with the gate.

    ``context`` is the context-fit verdict's measurement for this candidate's
    backend, or None when the backend declares no window. The verdict's own
    ``estimated_tokens`` — that backend's standing instruction chain plus the
    repository files the brief loads — is the figure its window gates, so the
    block carries it and ``headroom_pct`` is the share of the gating window
    that figure leaves free. Reading the verdict's figure rather than the
    request's harness-independent one keeps the margin Jev weighs the margin
    the gate measured: a lane whose standing chain is larger than the request's
    would otherwise report headroom the gate had already spent. A backend
    declaring no window gets no gate estimate, so its block keeps the
    request-level ``node_tokens`` and carries a null window and headroom, never
    a zero that would read as a spent window.
    """

    if context is None:
        return {
            "window_tokens": None,
            "estimated_tokens": node_tokens,
            "headroom_pct": None,
            **utilisation,
        }
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
    document = routing.pick_lane_document(
        backend, {} if documents is None else documents
    )
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
    session: str = "",
) -> dict[str, Any]:
    """The selecting fields of one budget view, hashed into its cache name.

    Two calls sharing every field read the same files and the same records and
    share one entry; a changed project, repository, configuration, record count,
    cache mode or session selects a different entry rather than serving a figure
    built for another request. The session is one of those fields because a
    session-scoped lift releases one session's reserves and no other's, so a
    view composed for one session must never be served to a caller asking for a
    different one. The records themselves are keyed by the ledger stamp in the
    value's stamp rather than re-hashed here, because the committed rows a
    caller passes are exactly the rows the ledger files carry.
    """

    return {
        "project": project,
        "repo": str(Path(repo).resolve()),
        "cached_only": cached_only,
        "records": len(records) if records is not None else None,
        "session": session,
        "budget_lifts": capabilities.file_stamp(str(budget_lift.lifts_path())),
        "config": hashlib.sha256(
            json.dumps(config, sort_keys=True, default=str).encode()
        ).hexdigest(),
    }


def _group_source(source: object) -> str:
    """Translate a pre-flight group's source word into the picker's vocabulary.

    A pre-flight group names its figure's provenance as ``document`` for the
    published paid-lanes document, ``account-surface`` for a live account-window
    read, and ``recorded`` for one a run's ledger carries. The picker's staleness
    rule speaks of a ledger reading rather than a recorded one, so the recorded
    word is mapped here rather than taught to a rule that also reads the raw
    state vocabulary.
    """

    if source == budget.WINDOW_SOURCE_DOCUMENT:
        return budget.WINDOW_SOURCE_DOCUMENT
    if source == "account-surface":
        return "account-surface"
    return "ledger"


def _compose_budget_report(
    project: str,
    config: dict[str, Any],
    repo: Path,
    records: list[dict[str, Any]],
    moment: datetime,
    *,
    cached_only: bool,
    session: str = "",
) -> dict[str, Any]:
    """Compose one dated live budget view at a stated moment.

    ``session`` is the dispatching session the view is asked about. It is handed
    to the preflight, which resolves each backend's budget block for that
    session, so a session-scoped lift releases the reserves on the path the
    picker reads. No session is passed as no session, which is how a caller
    naming none behaves as it did before the preflight took one.
    """

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
        session=session or None,
        **({"probe_runner": lambda _: {}} if cached_only else {}),
    )
    by_backend = {entry["backend"]: entry for entry in report["backends"]}
    shelf_seconds = budget.policy(config)["evidence_shelf_life_minutes"] * 60
    document_backed = lane_context._document_backed_backends(report)
    # The cadence the paid-lanes document publishes for its own readings, carried
    # on the report so a later re-age and the return-time block judge a
    # document-backed reading by the same bound this composition did.
    document_shelf_life_seconds = lane_context._document_shelf_life_seconds(document)
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
        for name in group["members"]:
            state = by_backend[name]["state"]
            block = lane_context._budget_block(
                {
                    "source": _group_source(group.get("source")),
                    "observed_at": allowance.get("observed_at"),
                    "suggested_shelf_life_seconds": document_shelf_life_seconds,
                },
                moment=moment,
                shelf_life_minutes=shelf_seconds / 60.0,
                document_backed=str(name) in document_backed,
            )
            state.update(
                source=block["budget_source"],
                observed_at=observed.isoformat(),
                expired=block["stale"],
                headroom="known",
                utilisation_pct=allowance["utilisation"] * 100,
                burn_multiple=allowance["burn_multiple"],
                resets_at=allowance["resets_at"],
                detail=(
                    "recorded account-window reading is stale"
                    if block["stale"]
                    else state.get("detail", "")
                ),
            )
    if document_shelf_life_seconds is not None:
        report["suggested_shelf_life_seconds"] = document_shelf_life_seconds
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
    document_backed = lane_context._document_backed_backends(report)
    document_shelf_life_seconds = lane_context._document_shelf_life_seconds(report)
    for entry in report["backends"]:
        state = entry["state"]
        remaining = state.get("seconds_until_reset")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            state["seconds_until_reset"] = max(0, int(remaining - delta))
        observed = parse_utc(str(state.get("observed_at") or ""))
        if observed is not None and state.get("headroom") == "known":
            # The same source-aware rule the composition and the return-time
            # block use, so a reading re-aged across its bound flips on the same
            # judgment that set it rather than on a second, source-blind one.
            block = lane_context._budget_block(
                state,
                moment=moment,
                shelf_life_minutes=shelf_seconds / 60.0,
                document_backed=str(entry.get("backend")) in document_backed,
                document_shelf_life_seconds=document_shelf_life_seconds,
            )
            state["expired"] = block["stale"]
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
    session: str = "",
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

    ``session`` is the dispatching session the view is composed for. It reaches
    the preflight so a session-scoped lift releases that session's reserves, and
    it is part of the cache key so a figure built for one session is never
    served from cache to another. A caller naming no session reads the figures a
    sessionless preflight produced, unchanged.
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
            project,
            config,
            repo,
            records,
            moment,
            cached_only=cached_only,
            session=session,
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
            [path, capabilities.file_stamp(path)] for path in (value.get("paths") or [])
        ]
        project_name, root = value["ledger"]
        stamps.append(["ledger", ledger.index_stamp(project_name, root)])
        return stamps

    try:
        request_key = _budget_view_request_key(
            project, config, repo, records, cached_only=cached_only, session=session
        )
        name = (
            "budget-view-"
            + hashlib.sha256(
                json.dumps(request_key, sort_keys=True, default=str).encode()
            ).hexdigest()
        )
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


def _reserved_lane_floor_met(
    declared: Mapping[str, Any], floor: Mapping[str, tuple[str, ...]]
) -> bool:
    """Whether declared requirement values meet every term of a reserved floor.

    Each term names the accepted values for one requirement key, and a value
    meets it when it ranks at or above the weakest accepted value, so a floor
    naming ``elevated`` is met by ``critical``. A key the node declares nothing
    for is not met: an undeclared requirement is an undeclared one, never the
    strength the node was not asked to state.
    """
    from reckon.capability import REQUIREMENT_LEVELS, _rank

    if not floor:
        return True
    for key, accepted in floor.items():
        levels = REQUIREMENT_LEVELS.get(key)
        if not levels:
            return False
        value = declared.get(key)
        if not isinstance(value, str) or value not in levels:
            return False
        bar = min(_rank(item, levels) for item in accepted if item in levels)
        if _rank(value, levels) < bar:
            return False
    return True


def _effective_budget_block(
    config: Mapping[str, Any],
    group: str | None,
    *,
    now: datetime,
    session: str | None,
) -> Mapping[str, Any]:
    """The budget block in force for a declared group, or the config's own.

    A lift in force on the group raises its pace multiple and releases all three
    reserves, and a credits lift rides on the same block as ``credit_lift``.
    Resolving it here, through the resolver a dispatch reads, is what lets the
    picker judge a lane on the same block a dispatch would: a group no lift
    covers is handed back its configured block unchanged, so the picker's
    judgement does not move where no lift is in force.
    """
    from reckon.crew import budget_group as budget_group_module

    if not group:
        return config.get("budget") or {}
    return budget_group_module.effective_block(
        config, str(group), readings=None, now=now, session=session or None
    )


def _budget_state_from_facts(name: str, state: Mapping[str, Any]) -> budget.BudgetState:
    """Rehydrate a candidate's own state mapping into a ``BudgetState``.

    The picker's view carries each backend's state as a plain mapping, which may
    omit fields a view never measured -- a hand-assembled snapshot states only
    what it reads. Rebuilding the dataclass from the known fields lets the same
    judgement a dispatch runs be applied here without the view having to carry a
    dataclass or every default.
    """
    fields = set(budget.BudgetState.__dataclass_fields__)
    known = {key: value for key, value in state.items() if key in fields}
    known["backend"] = name
    return budget.BudgetState(**known)


def _budget_exclusion_reasons(
    name: str,
    config: Mapping[str, Any],
    group: str | None,
    state: Mapping[str, Any],
    *,
    fresh: bool,
    utilisation: float | None,
    now: datetime,
    session: str | None,
) -> list[str]:
    """The reasons a lane's budget withholds it.

    The budget gate reads the verdict of :func:`reckon.budget.decide` through
    the group's effective block -- the same verdict and the same block a
    dispatch of this purpose takes -- rather than comparing the window against a
    ceiling of its own, so a lane a credits lift opens past a spent window is
    offered here exactly as dispatch admits a dispatch on it. The verdict's own
    reason is quoted, so a reader sees the figure and the branch that withheld
    the candidate.

    A reading past its shelf life is offered for Jev to weigh, never turned into
    a hard refusal, so the gate runs on a fresh reading alone. Where a credits
    lift rides, its pool governs and the window ceiling no longer does: the
    verdict admits or withholds on the credits drawn, and the ceiling comparison
    below is skipped for it. Elsewhere the effective ceiling -- the configured
    ceiling less the reserves dispatch withholds -- is applied even where the
    verdict reads the lane open on an availability observation, so the picker
    refuses a lane a dispatch would refuse.
    """
    if not fresh:
        return []
    block = _effective_budget_block(config, group, now=now, session=session)
    judged = budget.policy({**config, "budget": block})
    credit_lift = "credit_lift" in judged
    verdict = budget.decide(
        _budget_state_from_facts(name, state), judged, purpose="dispatch", now=now
    )
    if verdict.get("held"):
        reason = str(verdict.get("reason") or "the lane's budget withholds it")
        return [f"budget-ceiling: {reason}"]
    if not credit_lift and utilisation is not None:
        limit = budget.effective_ceiling(judged, "dispatch")
        if utilisation >= limit:
            return [
                (
                    f"budget-ceiling: {utilisation:g}% is at or above the "
                    f"{limit:g}% ceiling for a dispatch"
                )
            ]
    return []


def _reserved_lane_exclusion(
    lane: str,
    policy: Mapping[str, Any],
    *,
    declared: Mapping[str, Any],
    attempts: int,
    utilisation: float | None,
    lift_in_force: bool = False,
    waive_tier: bool = False,
) -> str | None:
    """The reason a lane's policy withholds it from this node, or None.

    The lane is withheld unless the node is at the tier the policy names -- its
    declared requirements meet every floor term, or it has already been
    attempted at or above the policy's count -- and its account window is below
    the policy's ceiling. Headroom is read as the lane's own window percentage,
    which is null when the reading is absent or past its shelf life, and an
    unmeasured or stale window is not an empty one: the lane is withheld, so the
    orchestrators' capacity is never spent on a guess. The reason names the lane,
    which term withheld it and the figures it was judged on, so a reader sees
    the policy that removed the candidate rather than a bare absence.

    A lift in force releases the window term, whatever its form. The window it
    measures is the account the orchestrators share, and a lift is the
    operator's decision to spend that account past the pace they reserved it to:
    judging the lane by the window under one would withhold exactly the work the
    lift was granted for. The budget verdict through the same lift-aware block
    governs instead, so a spent credit pool, a drain-by hold or a window spent
    under an uncapped lift still withholds the lane, with the verdict's own
    reason. What remains of the policy is the tier and attempts terms, which are
    what keep an ordinary node on the local lane.

    ``waive_tier`` is the oversize-node waiver: a caller that has established no
    lane outside the policy's coverage can run the node at all waives the tier
    and attempts terms, and every other term -- the window ceiling and the
    budget verdict the caller has already applied -- still withholds the lane.
    """
    floor = policy.get(flight.PICKER_RESERVED_LANE_FLOOR_KEY) or {}
    threshold = policy.get(flight.PICKER_RESERVED_LANE_ATTEMPTS_KEY)
    ceiling = float(policy.get(flight.PICKER_RESERVED_LANE_CEILING_KEY) or 0)
    attempts_met = isinstance(threshold, int) and attempts >= threshold
    if (
        not waive_tier
        and not _reserved_lane_floor_met(declared, floor)
        and not attempts_met
    ):
        return (
            f"reserved-lane: {lane} is offered at its hardest tier "
            f"{sorted(floor)} or after {threshold} attempts; this node declares "
            f"neither (attempts {attempts})"
        )
    if lift_in_force:
        # The lift-aware block governs: a spent credit pool, a pace hold or a
        # window spent past a zeroed reserve is already withheld by the budget
        # verdict, so the window term lets the tier term alone decide.
        return None
    if utilisation is None:
        return (
            f"reserved-lane: {lane} is reserved while the orchestrators it "
            f"shares with it needs it; its window reading is unknown or stale"
        )
    if utilisation >= ceiling * 100:
        return (
            f"reserved-lane: {lane} window is {utilisation:g}% used, at or above "
            f"the {ceiling * 100:g}% reserved-lane ceiling"
        )
    return None


def _reserved_fill_window_utilisation(
    group: Mapping[str, Any] | None,
    view: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    now: datetime,
) -> float | None:
    """The group's own fresh fill-window percentage, never its account overage.

    The fill window is read through :func:`reckon.pace_report.fill_window_clock`,
    so an account that publishes one operative window and neither named clock
    is judged on the window it does report rather than treated as absent: the
    same figure the pace row fills its five-hour clock with. The freshness rules
    stay the picker's own -- the shelf life, taken from the published document
    for a document-sourced group, and a reset that has not already passed.
    """
    from reckon import pace_report

    if not isinstance(group, Mapping):
        return None
    clock = pace_report.fill_window_clock(group, now=now)
    if not isinstance(clock, Mapping) or clock.get("state") != budget.OBSERVED:
        return None
    used = clock.get("utilisation")
    observed = parse_utc(str(clock.get("observed_at") or ""))
    reset = parse_utc(str(clock.get("resets_at") or ""))
    if (
        isinstance(used, bool)
        or not isinstance(used, (int, float))
        or not math.isfinite(used)
        or used < 0
        or observed is None
        or reset is None
        or reset <= now
    ):
        return None
    shelf = budget.policy(config)["evidence_shelf_life_minutes"] * 60
    if group.get("source") == budget.WINDOW_SOURCE_DOCUMENT:
        published = view.get("suggested_shelf_life_seconds")
        if (
            isinstance(published, (int, float))
            and not isinstance(published, bool)
            and published > 0
        ):
            shelf = float(published)
    if (now - observed).total_seconds() > shelf:
        return None
    return float(used) * 100.0


def _group_name(group: Any, config: Mapping[str, Any] | None) -> str | None:
    """A view group row's declared name.

    A group row names itself where the composer resolved one. A row that does
    not is matched to the declaration by its members, so a row carrying only
    members and an allowance still reaches the burn block derived for the
    wallet those members share.
    """

    if not isinstance(group, Mapping):
        return None
    declared_name = group.get("group")
    if declared_name:
        return str(declared_name)
    members = tuple(str(member) for member in (group.get("members") or ()))
    for name, declared_members in budget_group.declared_groups(config).items():
        if tuple(declared_members) == members:
            return str(name)
    return None


OVERSIZE_CONTEXT_WAIVER_NOTE = "offered because no open lane fits the node's context"


def _excluded_only_on_context(reasons: list[str]) -> bool:
    """Whether context fit is the whole of why a candidate was removed."""
    return bool(reasons) and all(
        reason.startswith("context-fit: ") for reason in reasons
    )


def _candidate_availability(
    request: PickRequest,
    name: str,
    config: Mapping[str, Any],
    backend: Mapping[str, Any],
    model: str | None,
    repo: Path,
    *,
    availability_cache: dict[tuple[str, str | None], dict[str, Any]] | None,
    cached_only: bool,
    now: datetime,
) -> str:
    """One lane's serving status, from its document, the cache, then the probe."""
    cache_key = (name, model)
    observation = (availability_cache or {}).get(cache_key)
    if observation is None:
        # A lane publishing what it serves answers from that document, which is
        # authoritative for it and costs no request.
        observation = _serving_observation(backend)
    if observation is None and cached_only:
        observation = _cached_observation(request.project, name, config, now=now)
    if observation is None:
        if cached_only:
            # A cached pick issues no request. An absent or expired observation
            # is unknown, never a claim the lane is down, so the candidate stays
            # offered for Jev to weigh.
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
    return str(observation.get("status") or "unknown")


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
    """Read candidate measurements, refreshing a qualifying reserved window."""
    now = datetime.now(UTC)
    rows = ledger.runs(request.project, root=repo) if records is None else records
    # The same declared tier and attempt count govern the probe and the gate.
    reserved = flight.reserved_lane_policies(config)
    declared_attempts = getattr(request, "attempts", None)
    # The node's attempt count is taken in the one pass the scan already makes
    # over the rows rather than in a walk of its own, so a pick reads the ledger
    # list exactly once whatever the backend count.
    attempts_out: dict[str, int] = {}
    declared_requirements = {
        **(request.capability.get("requirements") or {}),
        **(getattr(request, "orchestrator_assessment", None) or {}),
    }
    shared = (
        verdict_inputs
        if verdict_inputs is not None
        else routing.shared_verdict_inputs(request.project, repo)
    )
    view = (
        budget_snapshot
        if budget_snapshot is not None
        else budget_view(
            request.project,
            config,
            repo,
            rows,
            cached_only=cached_only,
            session=request.session,
        )
    )
    # pick() supplies a live view it built just before this scan. Keep its
    # reference current so the rendered state and candidate gate agree after a
    # probe; fixture snapshots without a check time remain read-only inputs.
    live_snapshot = budget_snapshot is None or isinstance(
        budget_snapshot.get("checked_at"), str
    )
    if live_snapshot and not cached_only and request.node.role != "review":
        for family, policy in reserved.items():
            floor = policy.get(flight.PICKER_RESERVED_LANE_FLOOR_KEY) or {}
            if not _reserved_lane_floor_met(declared_requirements, floor):
                continue
            for name, backend in (config.get("backends") or {}).items():
                if (
                    backend.get("derived_from")
                    or dispatch_picker.lane_family(config, name, backend) != family
                ):
                    continue
                group = next(
                    (item for item in view["groups"] if name in item["members"]),
                    None,
                )
                if _reserved_fill_window_utilisation(
                    group, view, config, now=now
                ) is None and paid_lanes.refresh_reserved_window(
                    config, name, backend, root=repo, moment=now
                ):
                    refreshed = budget_view(
                        request.project,
                        config,
                        repo,
                        rows,
                        cached_only=cached_only,
                        session=request.session,
                    )
                    if budget_snapshot is not None:
                        budget_snapshot.clear()
                        budget_snapshot.update(refreshed)
                        view = budget_snapshot
                    else:
                        view = refreshed
                break
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
    key_counts: dict[tuple[str, str], dict[str, Counter[str]]] = {}
    (
        outcome_counts,
        peak_values,
        requirement_counts,
        moved_in_counts,
        moved_off_counts,
    ) = _candidates_scan(
        rows, request, now=now, key_counts=key_counts, attempts_out=attempts_out
    )
    # The scan counted the node's own rows in the pass above; a request that
    # declares its attempt count keeps that figure instead.
    attempts = (
        declared_attempts
        if declared_attempts is not None
        else attempts_out.get("attempts", 0)
    )
    # A candidate stands on its own model id's record once it holds this many
    # outcomes; below it the candidate also reads its lane model key's earlier
    # generations as a prior. Read from the routing config so a host or project
    # layer retunes it, with the shipped figure when no layer declares one.
    prior_threshold = (config.get("routing") or {}).get("prior_outcome_threshold")
    if not isinstance(prior_threshold, int) or isinstance(prior_threshold, bool):
        prior_threshold = 5
    lane_utilisation = {
        name: _peak_summary(peak_values.get(name, []))
        for name in config.get("backends", {})
    }
    lane_documents: dict[str, Any] = {}
    budget_by_backend = {row["backend"]: row for row in view["backends"]}
    group_by_backend = {
        member: group for group in view["groups"] for member in group["members"]
    }
    # Each metered lane's burn is derived once, for every group the view names,
    # rather than per candidate: the readings behind it are the project's
    # recorded series, and reading them once per model would re-scan the same
    # stores for each model sharing one account. A candidate is handed its
    # group's block below, so no figure is computed in the picker.
    burn_by_group = budget.burn_blocks(
        request.project,
        config=config,
        groups=[
            name
            for name in (_group_name(group, config) for group in view["groups"])
            if name
        ],
        now=now,
    )
    # A review never runs on a backend the flight configuration withdraws from
    # review routing, so the candidate is removed here rather than offered for
    # Jev to weigh: an exclusion is a rule about what cannot run, not a
    # pressure signal. Read through the recovery helper so the key name and its
    # parsing have one source of truth.
    review_excluded: set[str] = set()
    if request.node.role == "review":
        review_excluded = recovery._review_excluded_backends(
            config, session=request.session
        )
    variants = []
    lanes = config.get("lanes") or {}
    for name, declared in config.get("backends", {}).items():
        if declared.get("derived_from"):
            continue
        lane = lanes.get(name) if isinstance(lanes, Mapping) else None
        models = lane.get("models") if isinstance(lane, Mapping) else None
        if isinstance(models, Mapping) and models:
            variants.extend((name, key, model) for key, model in models.items())
        else:
            variants.append((name, None, None))
    result = []
    # The reserved candidates a policy withheld, with the readings their policy
    # was judged on, so the set-wide oversize waiver below can re-judge each of
    # them without re-reading a window or a budget.
    reserved_withheld: list[dict[str, Any]] = []
    for name, model_key, _model_settings in variants:
        local = name == config.get("local_backend")
        # A lane model's row is built through the same two steps dispatch takes:
        # the model's own lane entry is selected, then the role overlay resolves
        # against it. Building it any other way would show Jev settings the pick
        # does not launch with.
        candidate_config = (
            flight.select_lane_model(config, name, name, model_key)
            if model_key is not None
            else config
        )
        _, backend = routing.resolve_role_override(
            candidate_config,
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
        # The budget gate reads the verdict a dispatch would take, through the
        # group's lift-aware block, rather than a ceiling of its own: a lane a
        # credits lift opens past a spent window is offered here exactly as
        # dispatch admits a dispatch on it, and one the lift's credits have
        # spent is withheld with the credit-cap reason the verdict names. The
        # reserved-lane term below resolves for itself whether any lift governs,
        # since a reading this gate declines to judge is the case where that
        # release matters most.
        budget_reasons = _budget_exclusion_reasons(
            name,
            config,
            group.get("group") if isinstance(group, Mapping) else None,
            state,
            fresh=fresh,
            utilisation=utilisation,
            now=now,
            session=request.session,
        )
        reasons.extend(budget_reasons)
        gate = _dispatch_lane_gate(backend)
        if gate["state"] in {"paused", "unreadable"}:
            reasons.append("lane-gate: " + gate["state"])
        # The session's own worker figure is read for every lane whose router
        # publishes a document, not only the local one: a metered lane's router
        # grants or withholds the session's workers exactly as the local one
        # does, and a route that lands on a lane granting this session nothing
        # is the case the guidance and the re-ask below exist to catch. The
        # figure is read through the one allowance helper the dispatch path
        # reads, so there is no second reader of it.
        slots, congestion, _ = (
            _lane(backend, request.session, lane_documents)
            if local or backend.get("lane_document")
            else (None, None, {})
        )
        # A lift in force on the lane's budget group is a fact of the lane's
        # account, so it rides beside the router's occupancy and is read from
        # the same group allowance the candidate's budget facts come from.
        lane_lift = (
            group_allowance.get("lift")
            if isinstance(group_allowance, Mapping)
            else None
        )
        if lane_lift:
            congestion = {**(congestion or {}), "lift": lane_lift}
        family = dispatch_picker.lane_family(config, name, backend)
        # A lane a policy reserves is offered to a non-review node only at the
        # tier the policy names and only while the lane's own window has
        # headroom, and it is removed here rather than left for Jev to weigh:
        # the policy is a rule about when the orchestrators' lane may be spent,
        # not a pressure signal, and a candidate carrying it is never offered as
        # a fallback either. The reading is the window percentage this candidate
        # already carries, so a reserved lane and the lane's own block are
        # judged on one figure. Whether any lift governs the lane's group is
        # resolved here, for the pick's session, whatever the freshness of the
        # reading: the no-window case is exactly where the release matters, and
        # a stale reading is the one the budget gate declines to judge.
        if family in reserved and request.node.role != "review":
            lift_in_force = "lift_id" in _effective_budget_block(
                config,
                group.get("group") if isinstance(group, Mapping) else None,
                now=now,
                session=request.session,
            )
            reserved_utilisation = (
                utilisation
                if not live_snapshot
                else _reserved_fill_window_utilisation(group, view, config, now=now)
            )
            withheld = _reserved_lane_exclusion(
                family,
                reserved[family],
                declared=declared_requirements,
                attempts=attempts,
                utilisation=reserved_utilisation,
                lift_in_force=lift_in_force,
            )
            if withheld:
                reasons.append(withheld)
                reserved_withheld.append(
                    {
                        "backend": name,
                        "backend_settings": backend,
                        "model": model,
                        "family": family,
                        "utilisation": reserved_utilisation,
                        "lift_in_force": lift_in_force,
                        "availability_cache": availability_cache,
                    }
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
            availability = _candidate_availability(
                request,
                name,
                config,
                backend,
                model,
                repo,
                availability_cache=availability_cache,
                cached_only=cached_only,
                now=now,
            )
            if availability in {"refused", "unavailable", "logged-out"}:
                reasons.append("availability: " + availability)
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
        budget_source = state.get("source") if reading_known else None
        own_outcomes = _outcome_counts(outcome_counts.get((name, model), Counter()))
        # The candidate's lane model key pairs with the lane the resolved
        # backend serves, so a key re-pointed to this model id reads the runs
        # recorded under the same lane and key at an earlier id. A candidate
        # already holding the threshold of its own outcomes carries no prior.
        candidate_lane = backend.get("lane") or name
        candidate_key = model_key if model_key is not None else backend.get("model_key")
        prior_block = (
            _prior_block(key_counts.get((candidate_lane, candidate_key)), model)
            if isinstance(candidate_key, str)
            and sum(own_outcomes.values()) < prior_threshold
            else None
        )
        result.append(
            BudgetCandidate(
                backend=name,
                family=family,
                model=model,
                effort=backend.get("effort"),
                suits=backend.get("suits"),
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
                outcomes=own_outcomes,
                outcomes_at_requirement=(
                    None
                    if requirement_counts is None
                    else _requirement_block(requirement_counts.get((name, model)))
                ),
                moved_in=_outcome_counts(moved_in_counts.get((name, model), Counter())),
                moved_off=moved_off_counts.get((name, model), 0),
                reasons=reasons,
                budget_reason=budget_reason,
                budget_source=budget_source,
                stale=stale,
                budget_age_s=budget_age_s,
                context=context_block,
                prior_outcomes=prior_block,
                burn=(
                    None
                    if local or group is None
                    else burn_by_group.get(_group_name(group, config) or "")
                ),
            )
        )
    # A node too large for every lane no policy reserves would be refused
    # outright, and its work never enters the queue. That decision belongs here
    # because only this function holds the whole set: when no unreserved
    # candidate is left and every unreserved one was removed on context fit
    # alone, the tier and attempts terms of a reserved lane's policy are waived
    # for this node. Nothing else is: the window ceiling and the budget verdict
    # were already applied when the candidate's reasons were built, and a
    # candidate held by one of those is left exactly as it was. A candidate the
    # waiver does admit is measured on its own context fit -- a reserved lane
    # whose window the node also exceeds cannot run it either -- and probed for
    # serving like any other offered candidate, since an offer that skipped the
    # probe would be the only one in the set that did.
    unreserved = [candidate for candidate in result if candidate.family not in reserved]
    if (
        reserved_withheld
        and unreserved
        and all(_excluded_only_on_context(c.reasons) for c in unreserved)
    ):
        for withheld_lane in reserved_withheld:
            candidate = next(
                (
                    c
                    for c in result
                    if c.backend == withheld_lane["backend"]
                    and c.model == withheld_lane["model"]
                ),
                None,
            )
            if candidate is None:
                continue
            still_withheld = _reserved_lane_exclusion(
                withheld_lane["family"],
                reserved[withheld_lane["family"]],
                declared=declared_requirements,
                attempts=attempts,
                utilisation=withheld_lane["utilisation"],
                lift_in_force=withheld_lane["lift_in_force"],
                waive_tier=True,
            )
            if still_withheld:
                continue
            contexts: dict[str, Any] = {}
            fit_reasons = _fit(
                request,
                candidate.backend,
                withheld_lane["backend_settings"],
                repo,
                verdict_inputs=shared,
                authority=authority,
                context_out=contexts,
            )
            if fit_reasons:
                candidate.reasons[:] = fit_reasons
                continue
            candidate.reasons.clear()
            candidate.context = _context_block(
                contexts.get(candidate.backend),
                node_context_tokens,
                lane_utilisation.get(candidate.backend, {}),
            )
            availability = _candidate_availability(
                request,
                candidate.backend,
                config,
                withheld_lane["backend_settings"],
                candidate.model,
                repo,
                availability_cache=withheld_lane["availability_cache"],
                cached_only=cached_only,
                now=now,
            )
            if availability in {"refused", "unavailable", "logged-out"}:
                candidate.reasons.append("availability: " + availability)
                continue
            candidate.availability = availability
            candidate.note = OVERSIZE_CONTEXT_WAIVER_NOTE
    return result
