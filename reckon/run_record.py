"""Composition of a completed run into its durable ledger row.

A promotion turns the facts it can still observe -- the worktree's diff, the
gate log, and the live pointer it is about to delete -- into one record that
outlives the run. This module owns that composition: the rate, pace and lane
measurements a run reports, the budget-group resolution that decides whether a
lane's harness cost is spend, and :func:`build_record`, which assembles the row
the ledger then commits.

The family sits beside :mod:`reckon.ledger` rather than inside it because it
grew past the size one worker can hold at once. The ledger keeps the durable
store -- writing run files, appending and reading the aggregate -- and
re-exports every name here, so a caller that imports a composed name from the
ledger keeps resolving it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# ── Completed runs ──────────────────────────────────────────────────────────

# The ledger key naming the repository paths a worker reported as lying beyond
# its own scope, and the spelling it replaced. A row promoted before the rename
# carries the retired key; every reader maps it on the way in and the writer
# emits only the current one, so an old row and a new one answer alike. The
# manifest field the paths are read from is the worker's own and keeps its name
# here; only the committed row's key is renamed.
OUTSIDE_SCOPE_PATHS = "outside_scope_paths"
RETIRED_OUTSIDE_SCOPE_PATHS = "follow_on_paths"


def normalize_outside_scope_paths(record: Mapping[str, Any]) -> dict[str, Any]:
    """A row carrying its outside-scope paths under the current key.

    The retired key is mapped onto the current one and dropped, so a reader
    finds one key whichever spelling the row was written with, and a later
    write cannot carry the retired key forward. A row that already carries the
    current key — or neither — is copied unchanged.
    """
    if RETIRED_OUTSIDE_SCOPE_PATHS not in record:
        return dict(record)
    mapped = {
        key: value
        for key, value in record.items()
        if key != RETIRED_OUTSIDE_SCOPE_PATHS
    }
    mapped.setdefault(OUTSIDE_SCOPE_PATHS, record[RETIRED_OUTSIDE_SCOPE_PATHS])
    return mapped


def measured_throughput(throughput: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep only the rates a throughput block actually measured, or nothing.

    An unmeasured figure is dropped rather than stored as null, and a block that
    measured nothing at all returns ``None`` so the caller omits the field. The
    distinction is the point: a stored zero, or an empty mapping, reads to every
    later consumer as a rate that was taken and found to be nil, which is a
    different claim from never having had a span to rate tokens against.
    """
    if not throughput:
        return None
    measured = {
        key: value
        for key, value in throughput.items()
        if key != "detail" and value is not None
    }
    if not measured:
        return None
    detail = str(throughput.get("detail") or "")
    if detail:
        measured["detail"] = detail
    return dict(sorted(measured.items()))


def unmeasured_clone_report(reason: str) -> dict[str, str]:
    """The explicit ledger marker for a clone report no revision pair can supply.

    A sha-diff that could not be read, a run that asserted no revision, and a
    shadow that lands no code all reach this state. The marker is a mapping so a
    reader tells it from the empty list a measured-but-matching-nothing run
    carries, which an absent key or an empty list would each hide.
    """
    return {"status": "unmeasured", "reason": str(reason).strip()}


def _clone_report(value: Any) -> list[dict[str, Any]] | dict[str, str]:
    """Normalise a clone report to the list a measured run carries, or its marker."""
    if value is None:
        return unmeasured_clone_report("no clone comparison was run")
    if isinstance(value, Mapping):
        return dict(value)
    return [dict(match) for match in value]


def underivable_duration(reason: str, *, detail: str = "") -> dict[str, str]:
    """Return the explicit ledger marker for duration figures no record can supply."""
    marker = {"status": "underivable", "reason": str(reason).strip()}
    if detail.strip():
        marker["detail"] = detail.strip()
    return marker


def duration_measurement_state(record: Mapping[str, Any]) -> str:
    """Classify a row's duration figures as measured, underivable, or missing.

    A measured duration is the three-part identity consumers need: wall time,
    model generation time, and machine time.  The latter pair must both be
    numeric and live in the throughput block.  Anything else is underivable
    only when the row carries a reason saying why; an absent reason remains a
    missing measurement so a backfill or a property check can find it.
    """

    def numeric(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    throughput = record.get("throughput")
    if (
        numeric(record.get("wall_seconds"))
        and isinstance(throughput, Mapping)
        and numeric(throughput.get("generation_seconds"))
        and numeric(throughput.get("machine_seconds"))
    ):
        return "measured"
    marker = record.get("duration_measurement")
    if (
        isinstance(marker, Mapping)
        and marker.get("status") == "underivable"
        and str(marker.get("reason") or "").strip()
    ):
        return "underivable"
    return "missing"


def promoted_budget_fallback(
    fallback: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Carry a lane substitution onto the record, or nothing when none happened.

    Absent rather than empty for the same reason as the rate above: a run that
    ran on the lane its caller named and one that was handed over from a held
    lane must not read alike, or a calibration slice credits the substitute's
    work to the backend nobody dispatched to.
    """
    if not fallback:
        return None
    return dict(fallback)


def promoted_pace_row(run_id: str) -> dict[str, Any] | None:
    """Carry the pace row the run's live pointer holds onto the committed row.

    A dispatch writes the allowance it was admitted under onto the run's live
    pointer, and promotion deletes that pointer in the step that appends the
    committed row — so a row composed from the promotion's own arguments would
    drop the pace the run was judged by, and a week of decisions could be
    replayed only by reopening streams the reaper reclaims. The pointer is
    therefore read here, by run id, while the promotion still holds it.

    Absent rather than empty, for the same reason as the fields above: a key
    carrying nothing would assert a pace was recorded for a run that has none,
    and a row for a run whose pointer is already gone has no pace to state.
    """
    from reckon.crew import runs

    try:
        pointer = runs.read_pointer(run_id)
    except runs.CrewError:
        return None
    pace = pointer.get("pace")
    if not isinstance(pace, Mapping) or not pace:
        return None
    return dict(pace)


def promoted_picker_selection(run_id: str) -> dict[str, Any] | None:
    """Read the dispatch-time picker answer before promotion removes its pointer."""
    from reckon.crew import runs

    try:
        pointer = runs.read_pointer(run_id)
    except runs.CrewError:
        return None
    selection = pointer.get("picker_selection")
    return dict(selection) if isinstance(selection, Mapping) else None


def promoted_route_mode(run_id: str) -> str | None:
    """Read how the dispatch chose its backend before promotion removes its pointer.

    Every route mode the dispatch can record is accepted, the overridden-hold
    value among them, so a row built here keeps the mode the pick produced
    rather than reading as one whose mode was never written.
    """
    from reckon.crew import runs
    from reckon.crew.picker.outcomes import OVERRIDDEN_HOLD_ROUTE_MODE

    try:
        pointer = runs.read_pointer(run_id)
    except runs.CrewError:
        return None
    mode = pointer.get("route_mode")
    accepted = {"picker", "shadow", "explicit", OVERRIDDEN_HOLD_ROUTE_MODE}
    return mode if mode in accepted else None


def is_unmetered_backend(backend: str) -> bool:
    """Whether a named backend has no metered per-token price."""
    return str(backend or "").strip() in UNMETERED_BACKENDS


_RESOLVED_FLIGHT_CACHE: dict[tuple[Any, ...], Mapping[str, Any]] = {}


def _flight_layer_key(project: str | None = None) -> tuple[Any, ...]:
    """A cache key naming the flight layer files and their content identity.

    Keyed on the paths and their mtimes rather than on the resolved object, so
    a catalogue or host layer rewritten between reads is re-resolved while the
    repeated lookups within one promotion are cheap. A layer that cannot be
    stat'd contributes its path alone, which stays stable across those reads —
    the cache then holds the empty config a missing layer resolves to.

    ``project`` adds the project layer to the key. A project's own
    ``budget_group`` override lives in that file, so a key that omitted it
    would let one project's resolution answer for a different project, and
    would keep answering from the pre-override config after the file changed.
    """
    from reckon import flight

    paths: list[Path] = [
        flight.shipped_defaults_path(),
        flight.host_config_path(),
        flight.model_catalogue_path(),
    ]
    if project:
        paths.append(flight.project_config_path(project))
    parts: list[Any] = []
    for path in paths:
        resolved = str(path)
        try:
            stat = os.stat(path)
        except OSError:
            parts.append((resolved, None, None))
        else:
            parts.append((resolved, stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


def _resolved_flight_config(project: str | None = None) -> Mapping[str, Any]:
    """The flight config reckon runs under, or ``{}`` when it cannot be read.

    Read lazily and defensively: the config is versioned data in the checkout,
    so a wheel install carries none, and a malformed layer must not turn every
    ledger write into an error. ``FlightConfigError`` covers a layer that does
    not parse — a duplicate key or a schema refusal — and falls back to an
    empty config, which declares no budget group and so leaves every lane
    metered. Resolving the layers rather than reading the catalogue file alone
    is what lets a host, project or override value for a backend's
    ``budget_group`` reach this reader, so which lanes are subscription-billed
    is a routing decision a project or host can change without editing code.

    ``project`` folds in that project's layer, so the override a project writes
    for one of its own lanes is honoured on its rows. Without it only the
    shipped, host and catalogue layers speak — the right answer for a caller
    that has no project, such as a row read without one or a surface pooling
    every project.
    """
    from reckon import flight

    key = _flight_layer_key(project)
    cached = _RESOLVED_FLIGHT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        config = flight.resolve(project).config
    except (flight.FlightConfigError, OSError, ValueError):
        config = {}
    _RESOLVED_FLIGHT_CACHE[key] = config
    return config


def backend_budget_group(backend: str, *, project: str | None = None) -> str | None:
    """The budget group resolved flight config declares for a backend, or ``None``.

    The lookup is delegated to the module that owns the declaration
    (:mod:`reckon.crew.budget_group`), read over resolved config rather than
    the catalogue file alone, so a host or project override of a backend's
    ``budget_group`` is honoured. ``project`` selects the project layer, so a
    project that moves one of its lanes into a group is read that way on its
    own rows. A backend the config does not name declares no group here, so a
    lane whose billing nothing states keeps its cost.
    """
    from reckon.crew import budget_group as budget_group_module

    return budget_group_module.declared_group_for(
        _resolved_flight_config(project), backend
    )


def is_subscription_backend(backend: str, *, project: str | None = None) -> bool:
    """Whether a named backend draws on a group the catalogue bills as a
    flat subscription rather than metering it per token.

    The billing rides the catalogue's ``budget_group`` name: a group whose
    name ends in ``-sub`` (``claude-sub``, ``codex-sub``) is a subscription.
    The lookup reads the resolved config, so a backend it does not name — or
    names without a group — is metered and keeps its recorded cost. ``project``
    folds that project layer in, so one project can move a lane into a
    subscription group and be billed as one there while the same lane stays
    metered on another project's rows.
    """
    group = backend_budget_group(backend, project=project)
    return group is not None and group.endswith(SUBSCRIPTION_GROUP_SUFFIX)


def _label_unmetered_cost(
    budget: Mapping[str, Any], backend: str, *, project: str | None = None
) -> dict[str, Any]:
    """Replace an invented dollar figure with an explicit, flagged absence.

    ``cost_usd`` is not missing from an unmetered run's stream — the harness
    still reports it, priced for a model name that was never the one billed.
    Recording it verbatim would make a free lane look like the dearest one
    on any surface that ranks by cost, silently. Nulling it and flagging the
    null keeps the absence visible instead.

    A subscription-billed lane — one drawing on a budget group the catalogue
    bills as a subscription (``claude-sub``, ``codex-sub``) — is not metered
    per token either, so its harness cost is treated the same way: nulled,
    flagged, and preserved under ``harness_reported_cost_usd`` so a reader
    keeps the figure without it being summed as spend. The unmetered local
    lane keeps its original shape exactly.

    The computed ``notional_cost_usd`` — derived from declared per-million
    rates, never read from the harness — is deliberately outside this flag's
    reach.  An unmetered lane has no dated rate, so no notional figure can
    exist for it, and a figure already present is preserved rather than
    nulled.  ``capabilities._cost_usd`` treats ``cost_usd_imputed`` as
    authoritative over either field, so the flag keeps exactly the meaning it
    has today: a nulled figure, not a computed one.

    ``project`` names the project the row belongs to, so that project's own
    ``budget_group`` override is folded into the resolution and a lane the
    project moved into a subscription group is billed as one on its rows.
    """
    result = dict(budget)
    subscription = is_subscription_backend(backend, project=project)
    if not subscription and not is_unmetered_backend(backend):
        return result
    reported = (result.get("cost_usd"), result.get("cost_usd_cumulative"))
    if not any(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in reported
    ):
        return result
    if subscription:
        result["harness_reported_cost_usd"] = result.get("cost_usd")
        result["harness_reported_cost_usd_cumulative"] = result.get(
            "cost_usd_cumulative"
        )
        result["billing"] = "subscription"
        group = backend_budget_group(backend, project=project)
        if group:
            result["budget_group"] = group
    result["cost_usd"] = None
    result["cost_usd_cumulative"] = None
    result["cost_usd_imputed"] = True
    return result


def build_record(
    *,
    run_id: str,
    plan: str,
    gate: str,
    project: str = "",
    failure_classification: str = "",
    node: str = "",
    node_definition: Mapping[str, Any] | None = None,
    section: str = "",
    role: str = "",
    spec_level: str = "",
    member_id: str = "",
    backend: str = "",
    agent: Mapping[str, Any] | None = None,
    brief: Mapping[str, Any] | None = None,
    plan_link: str = "",
    unplanned_reason: str = "",
    dispatched_at: str = "",
    completed_at: str = "",
    completed_at_source: str = "promotion_time",
    worker_seconds: int | None = None,
    worker_seconds_source: str = "",
    wall_seconds: int | None = None,
    stalled: bool = False,
    time_budget: str = "",
    base_sha: str = "",
    commits: Iterable[str] = (),
    changed_lines: Mapping[str, Any] | None = None,
    tests_added: int | None = None,
    outcome: str = "",
    disposition: str = "",
    manifest_path: str = "",
    scope_changed: bool = False,
    session: str | None = None,
    session_id: str | None = None,
    session_harness: str | None = None,
    session_model: str | None = None,
    budget: Mapping[str, Any] | None = None,
    lane_receipt: Mapping[str, Any] | None = None,
    throughput: Mapping[str, Any] | None = None,
    budget_fallback: Mapping[str, Any] | None = None,
    lineage: Mapping[str, Any] | None = None,
    shadow_patch: str = "",
    unreconciled_override: Mapping[str, Any] | None = None,
    orchestrator_lane_override: Mapping[str, str] | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    suite_delta: Mapping[str, Any] | None = None,
    resume_remedy: Mapping[str, str] | None = None,
    outside_scope_paths: Iterable[str] | None = None,
    predecessor_run: str | None = None,
    dispute_count: int | str | None = None,
    review: Mapping[str, Any] | None = None,
    clone_matches: Iterable[Mapping[str, Any]] | None = None,
    shadow_contaminated: str = "",
    promoted_by: str = "",
) -> dict[str, Any]:
    """Assemble one completed-run record, refusing an unknown gate verdict.

    ``gate_check`` is stored whenever given, independent of ``require_gate_check``
    — a caller that already has the evidence should record it even when not
    asking for enforcement. ``require_gate_check=True`` is what makes a passing
    verdict with no check unfalsifiable-by-construction: it is refused rather
    than stored, naming exactly which of command, exit status, and log
    path/digest is missing.

    The routing-evidence fields record facts read from the worker's own
    manifest at promotion, so a later measure can separate a followup touch
    from a defect without the run directory. They are stored whenever the
    caller supplies them and silently absent otherwise, so a row promoted
    before this instrumentation keeps every existing field; the three new keys
    are additions, never replacements.

    ``disposition`` is the verb a coordinator recorded for why the run's live
    pointer outlived its session. It is stored on the row because promotion
    deletes the pointer, which is the only other copy of it, and is null on a
    run that carried no recorded disposition.

    ``review`` is the compact block a review record reduces to (see the review
    store's reducer), so an attached review survives the loss of the crew
    configuration home on the one durable record that joins to it — the row
    owns the dimensions and their total, and the store owns the verbatim text
    and findings. A row with no review keeps the key at ``None``, which is
    distinct from a parsed review whose dimensions genuinely measure zero.
    """
    verdict = str(gate).strip().lower()
    if verdict not in GATE_VERDICTS:
        raise LedgerError(
            f"gate verdict {gate!r} is not one of {', '.join(GATE_VERDICTS)}; "
            "a gate whose evidence could not be produced is 'not-run'"
        )
    classification = str(failure_classification).strip().lower()
    if classification and classification not in FAILURE_CLASSIFICATIONS:
        raise LedgerError(
            f"failure classification {failure_classification!r} is not one of "
            f"{', '.join(FAILURE_CLASSIFICATIONS)}"
        )
    if require_gate_check and verdict == "passed":
        missing = gate_check_missing_fields(gate_check)
        if missing:
            raise LedgerError(
                "a passing gate requires the check that produced it; missing "
                + ", ".join(missing)
            )
    stored_lineage = None if lineage is None else dict(lineage)
    stored_agent = dict(agent or {})
    # The lane and model key the recorded backend serves, resolved through the
    # catalogue's one name resolver so a stored row carries the pair beside
    # ``backend``. Reading it here keeps a row promoted after a backend name
    # retires attributable to the lane and model it actually ran. A malformed
    # catalogue must not turn a write into an error -- the same fallback the
    # billing resolution takes leaves the pair unset rather than raising out
    # of the ledger, so the row is still written. The catch is the catalogue's
    # own fault vocabulary, not every failure a resolver can raise: an
    # ambiguous name is refused as a ValueError and must surface, so a row is
    # never written with an unset pair standing in for a name two lanes claim.
    from reckon import flight as flight_module

    try:
        stored_identity = normalize_identity(
            {"backend": str(backend), "agent": stored_agent}
        )
    except (flight_module.FlightConfigError, OSError):
        stored_identity = {}
    # Completion consults the same accessor capability derivation will read
    # later, but the verdict lands as its own field rather than mutating the
    # lineage a caller (e.g. shadow dispatch) already holds a copy of.
    is_shadow_lineage = (
        isinstance(stored_lineage, Mapping) and stored_lineage.get("kind") == "shadow"
    )
    stored_shadow_controlled = (
        shadow_controlled(stored_lineage, contaminated=shadow_contaminated)
        if is_shadow_lineage
        else None
    )
    record: dict[str, Any] = {
        "run_id": str(run_id),
        # A brief run names no plan, so its row carries ``plan`` null and the
        # digest of the stored brief it read instead of a plan section. The key
        # stays present on every row so a reader can tell a run with no plan
        # from one whose field was never written.
        "plan": None if brief is not None else str(plan),
        "section": normalize_section(section),
        "node": str(node),
        "node_definition": (None if node_definition is None else dict(node_definition)),
        "role": str(role),
        # A brief run's authority is the stored brief, so its digest and stored
        # path ride the row where a plan run carries only the plan identity.
        # ``plan_link`` names the plan whose product an unplanned landing
        # changed, and ``unplanned_reason`` states why no plan link was given;
        # both are None on a run that needed neither, so a reader can tell an
        # unrecorded value from one never asked for.
        "brief": None if brief is None else dict(brief),
        "plan_link": str(plan_link) or None,
        "unplanned_reason": str(unplanned_reason) or None,
        "spec_level": str(spec_level),
        "member": str(member_id),
        # Routing is a property of the run, not only of the agent description.
        # Keeping it at the record level preserves attribution when a recovered
        # pointer carries no agent block.
        "backend": str(backend),
        "lane": stored_identity.get("lane"),
        "model_key": stored_identity.get("model_key"),
        "local": bool(stored_agent.get("local")),
        "agent": stored_agent,
        "dispatched_at": str(dispatched_at),
        "completed_at": str(completed_at) or _utc_now(),
        "completed_at_source": str(completed_at_source) or "promotion_time",
        "worker_seconds": None if worker_seconds is None else int(worker_seconds),
        "worker_seconds_source": str(worker_seconds_source)
        or ("provided" if worker_seconds is not None else "unavailable"),
        "wall_seconds": None if wall_seconds is None else int(wall_seconds),
        "stalled": bool(stalled),
        "time_budget": str(time_budget),
        "base_sha": str(base_sha),
        "commits": [str(sha) for sha in commits if str(sha).strip()],
        "changed_lines": None if changed_lines is None else dict(changed_lines),
        "tests_added": None if tests_added is None else int(tests_added),
        "gate": verdict,
        "gate_check": _normalized_gate_check(gate_check),
        "suite_delta": None if suite_delta is None else dict(suite_delta),
        "failure_classification": classification or None,
        "outcome": str(outcome),
        # The verb that disposed of the run, read from the live pointer before
        # promotion removes it. The key is present and null on a run that
        # carried no disposition, so a reader can tell an unrecorded verb apart
        # from a row written before the field existed.
        "disposition": str(disposition).strip() or None,
        "manifest_path": str(manifest_path),
        "scope_changed": bool(scope_changed),
        # The crew session that dispatched the run, distinct from ``session_id``
        # (the worker's own harness session). Present as ``None`` when no
        # pointer named one, like the row's other declared-but-unset fields.
        "session": session,
        "session_id": session_id,
        "session_harness": session_harness,
        "session_model": session_model,
        # Whatever headroom the backend reported while this run was in flight.
        # Carried here because the pointer that held it is deleted on promotion,
        # and a pre-flight that has to make a call to learn headroom spends the
        # very resource it is measuring — most often when it is scarcest.
        "budget": _label_unmetered_cost(budget or {}, backend, project=project),
        "lane_receipt": None if lane_receipt is None else dict(lane_receipt),
        "lineage": stored_lineage,
        "shadow_controlled": stored_shadow_controlled,
        "shadow_patch": str(shadow_patch),
        "unreconciled_override": (
            None if unreconciled_override is None else dict(unreconciled_override)
        ),
        "orchestrator_lane_override": (
            None
            if orchestrator_lane_override is None
            else dict(orchestrator_lane_override)
        ),
        "review": None if review is None else dict(review),
        # Every declared field is present on every row, so the clone report is
        # always carried. A run the detector ran over carries the list of
        # matches (empty when its functions copied nothing); a run no revision
        # pair could be measured for carries the unmeasured marker instead, so
        # the two never read alike.
        "clone_matches": _clone_report(clone_matches),
        # What promoted this run. Null on every row a coordinator promoted,
        # "review-acceptance" on a row a clean stored review accepted, and
        # "review-self" on a review run that promoted itself once its stored
        # record parsed, so the three never read alike. Present on every row,
        # like the other declared fields, so a reader can tell an absent value
        # from one never written.
        "promoted_by": str(promoted_by).strip() or None,
        "picker_selection": promoted_picker_selection(run_id),
        "route_mode": promoted_route_mode(run_id),
    }
    # Both of these are absent from a record that has nothing to say about them,
    # which is why they are set after the literal rather than in it. The rate a
    # run generated at and the lane it was handed over to are measurements; a
    # record carrying the key with a null or an empty value would assert that
    # the measurement was taken.
    stored_throughput = measured_throughput(throughput)
    if stored_throughput is not None:
        record["throughput"] = stored_throughput
    stored_fallback = promoted_budget_fallback(budget_fallback)
    if stored_fallback is not None:
        record["budget_fallback"] = stored_fallback
    # The pace the run was admitted under lives on the live pointer, which this
    # promotion deletes, so it is read by run id rather than taken as an
    # argument: a committed row that could not name its own allowance left every
    # dispatch decision replayable only from the stream beside it.
    stored_pace = promoted_pace_row(run_id)
    if stored_pace is not None:
        record["pace"] = stored_pace
    if resume_remedy is not None:
        record["resume_remedy"] = dict(resume_remedy)
    # Contamination is a measurement of the shadow's stream, so a row with no
    # such reading must not read as one that read clean. Only a marked row
    # carries the key.
    if str(shadow_contaminated).strip():
        record["shadow_contaminated"] = str(shadow_contaminated).strip()
    # Routing evidence is present whenever promotion could read it. An empty
    # ``outside_scope_paths`` is a real measurement — the manifest declared no
    # such paths — and stays distinct from an absent key, which is a row never
    # measured; ``dispute_count`` is an integer when the report parsed and
    # stated no corrections, the string "unknown" when it did not read;
    # ``predecessor_run`` is a run id or nothing, since a run with no
    # predecessor must not read identically to one with no base to compare.
    if outside_scope_paths is not None:
        record[OUTSIDE_SCOPE_PATHS] = [str(path) for path in outside_scope_paths]
    if predecessor_run is not None:
        record["predecessor_run"] = str(predecessor_run)
    if dispute_count is not None:
        record["dispute_count"] = dispute_count
    record.setdefault("execution_fit", None)
    record.setdefault("attempt_kind", None)
    record.setdefault("attempt", None)
    # The review tier is written onto the row by promotion, after the record is
    # built, so a row assembled without it carries no tier rather than failing
    # to declare one. The key is always present so the declared schema and the
    # emitted row cannot drift.
    record.setdefault("review_tier", None)
    if duration_measurement_state(record) == "missing":
        reason = (
            "wall_clock_unavailable_at_promotion"
            if record.get("wall_seconds") is None
            else "model_span_unavailable_at_promotion"
        )
        record["duration_measurement"] = underivable_duration(reason)
    else:
        record["duration_measurement"] = None
    record.setdefault("scope_acceptances", None)
    record.setdefault("no_commit", None)
    record.setdefault("commit_resolution", None)
    record.setdefault("worktree_retention", None)
    record.setdefault("boundary_waiver", None)
    record.setdefault("domain_waiver", None)
    record.setdefault("resume_waiver", None)
    record.setdefault("watch_override", None)
    return record


def measurement_exclusion_reason(record: Mapping[str, Any]) -> str | None:
    """Name why a run cannot feed duration consumers, if it cannot."""
    # A shadow that read its primary's answer is void as a measurement no
    # matter what else is true of the row, so the contamination reason lands
    # first: every slice that excludes for any other reason must exclude for
    # this one too.
    if record.get("shadow_contaminated"):
        return "contaminated"
    if record.get("scope_changed"):
        return "scope_changed"
    if record.get("stalled"):
        return "stalled"
    if str(record.get("completed_at_source") or "") not in USABLE_COMPLETION_SOURCES:
        return "unusable_completion"
    return None


def per_run_budget(
    cumulative: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Label cumulative counters and expose their non-negative run deltas."""
    result = dict(cumulative)
    prior_budget = dict((previous or {}).get("budget") or {})
    current_tokens = dict(result.get("tokens") or {})
    prior_tokens = dict(prior_budget.get("tokens") or {})
    measured_tokens: dict[str, Any] = {}
    for name, value in current_tokens.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            measured_tokens[name] = value
            continue
        cumulative_name = f"{name}_cumulative"
        prior_has = cumulative_name in prior_tokens or name in prior_tokens
        prior_value = prior_tokens.get(cumulative_name, prior_tokens.get(name, 0))
        if not isinstance(prior_value, (int, float)) or isinstance(prior_value, bool):
            if prior_has:
                # The prior attempt's reading exists but cannot be measured, so
                # this attempt's share of the thread's cumulative is unknown.
                # Subtracting a zero would present the whole cumulative as this
                # attempt's own work, which is the overcount the fold removes.
                measured_tokens[name] = "unknown"
                measured_tokens[cumulative_name] = value
                continue
            prior_value = 0
        measured_tokens[name] = max(0, value - prior_value)
        measured_tokens[cumulative_name] = value
    if current_tokens:
        result["tokens"] = measured_tokens

    cost = result.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        prior_cost_has = (
            "cost_usd_cumulative" in prior_budget or "cost_usd" in prior_budget
        )
        prior_cost = prior_budget.get(
            "cost_usd_cumulative", prior_budget.get("cost_usd", 0)
        )
        if not isinstance(prior_cost, (int, float)) or isinstance(prior_cost, bool):
            if prior_cost_has:
                # Same as the token counters: an unreadable prior reading makes
                # this attempt's share unknown rather than equal to the total.
                result["cost_usd"] = "unknown"
                result["cost_usd_cumulative"] = cost
                return result
            prior_cost = 0
        result["cost_usd"] = max(0.0, cost - prior_cost)
        result["cost_usd_cumulative"] = cost
    return result


# Imported after the family rather than at module scope: the ledger re-exports
# every name defined here, so a module-scope import would close an import cycle and
# make either module depend on which one an interpreter reaches first. Every name
# below is read at call time, so binding them once the definitions exist is
# equivalent to binding them at the top.
from reckon.ledger import (  # noqa: E402
    FAILURE_CLASSIFICATIONS,
    GATE_VERDICTS,
    SUBSCRIPTION_GROUP_SUFFIX,
    UNMETERED_BACKENDS,
    USABLE_COMPLETION_SOURCES,
    LedgerError,
    _normalized_gate_check,
    _utc_now,
    gate_check_missing_fields,
    normalize_identity,
    normalize_section,
    shadow_controlled,
)
