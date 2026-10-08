"""Select a configured backend with hard eligibility checks and typed judgment."""

import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew.runs import crew_home
from reckon.resources import resource_scan_scope

from . import client, prompts, snapshot
from .types import Candidate, PickRequest, Selection

__all__ = ["Candidate", "PickRequest", "Selection", "pick"]

#: One JSON line per pick, appended under the crew home. Every production pick
#: writes its per-stage times here, so a pick that spent its budget -- and one
#: that finished after dispatch gave up -- can be attributed to the stage that
#: spent it.
PICK_TIMINGS_LOG = "pick-timings.jsonl"


def _milliseconds(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _dispatch_bound_seconds() -> float:
    """The dispatch timeout a pick's within-bound flag is measured against.

    Read from the dispatcher rather than mirrored, so the flag cannot drift from
    the timeout dispatch actually applies to a pick. The dispatcher is already
    imported on every pick path.
    """
    from importlib import import_module

    dispatch = import_module("reckon.crew.dispatch")
    return float(dispatch.PICKER_DISPATCH_TIMEOUT_SECONDS)


def _record_pick_timings(line: dict[str, Any], *, latency_ms: float) -> None:
    """Append one timing line under the crew home, best effort and bounded.

    A pick must never fail or stall because its own record could not be written,
    so every error is swallowed. One ``os.write`` on an ``O_APPEND`` descriptor
    keeps a small line from interleaving with a concurrent pick's.
    """
    try:
        line["within_bound"] = latency_ms <= _dispatch_bound_seconds() * 1000
        path = crew_home() / PICK_TIMINGS_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(line, separators=(",", ":"), default=str) + "\n").encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
    except Exception:  # noqa: BLE001 - a pick never fails on its own record
        return


def _section_effort_hours(plan_path: Path, section: str) -> float | None:
    """Return a plan section's own declared effort hours, or None.

    Read through the same section record the routing resolver uses, so the
    section a dispatch scoped a node to is the one whose effort is charged to
    it. A section that declares no effort, or a value that is not a positive
    finite figure, resolves to None rather than to a measured zero.
    """

    from reckon.crew.routing import _section_record

    try:
        record, _project = _section_record(plan_path, section)
        hours = float(record.get("effort_hours"))
    except (TypeError, ValueError):
        return None
    return hours if math.isfinite(hours) and hours > 0 else None


def _declared_difficulty(
    request: PickRequest,
    config: dict[str, Any],
    *,
    repo: Path,
    authority: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    """Read the capability and estimate a node's plan section declares.

    A pick is handed a node and the section it names, not the difficulty that
    section declares, so Jev weighs a deep, critical node as one that carries
    nothing. Both facts live in the plan the node names: the same
    section-routing resolver the dispatcher runs yields the capability at the
    attempt count the section has earned, and the estimate is the node's own
    figure, else the effort its section declares, else the plan's declared
    effort hours. The resolution reads the plan and the project ledger, so it
    is timed inside the pick and any failure -- including one for a node that
    names no plan -- leaves the field null rather than failing or stalling the
    pick. A capability the request already carries, or an estimate the node
    itself carries, is never overridden.
    """

    node = request.node

    def plan_repo() -> Path:
        plan = authority.get("plan") if isinstance(authority, dict) else None
        repository = plan.get("repository") if isinstance(plan, dict) else None
        return Path(str(repository)) if repository else repo

    capability = request.capability or None
    # The plan is resolved once and serves both facts: the section record that
    # carries the effort, and the routing resolver that carries the capability.
    plan_path: Path | None = None
    if node.plan.strip() and node.section.strip():
        try:
            from reckon import resources

            resource = resources.resolve_resource(
                plan_repo() / "docs",
                request.project,
                node.plan,
                "plan",
                include_archived=False,
            )
            plan_path = resource.path if resource is not None else None
        except Exception:  # noqa: BLE001 - a missing plan leaves the fields null
            plan_path = None

    hours: float | None = None
    hours_source: str | None = None
    provenance = "unavailable"
    try:
        from reckon.crew.routing import _estimated_hours

        hours, provenance = _estimated_hours(plan_repo(), request.project, node)
    except Exception:  # noqa: BLE001 - the declaration is advisory to a pick
        hours, provenance = None, "unavailable"
    if provenance == "node":
        # The node's own estimate is authoritative over anything the plan says.
        hours_source = "node"
    else:
        if hours is not None:
            hours_source = "plan"
        # A section's own declared effort outranks the plan's total, and it is
        # read whether or not the plan declares a total at all -- most plans do
        # not, and gating this on the plan's provenance drops their sections.
        if plan_path is not None:
            section_hours = _section_effort_hours(plan_path, node.section)
            if section_hours is not None:
                hours, hours_source = section_hours, "section"
    if capability is None and plan_path is not None:
        try:
            from reckon.crew.routing import resolve_section_routing

            resolved = resolve_section_routing(config, node=node, plan_path=plan_path)
            capability = resolved.get("capability") or None
        except Exception:  # noqa: BLE001 - an unresolved capability stays null
            capability = None
    return capability, hours, hours_source


def _answer(
    payload: dict[str, Any], offered: list[Candidate]
) -> tuple[str, float, dict[str, float]]:
    answer = payload["answers"]["route"]
    choice = answer["choice"]
    # Jev answers by the lane-and-model pair each option is offered under.
    keys = {prompts.option_key(candidate) for candidate in offered} | {"hold"}
    confidence = answer["confidence"]
    probabilities = answer["probabilities"]
    if (
        choice not in keys
        or not isinstance(probabilities, dict)
        or set(probabilities) != keys
    ):
        raise ValueError(
            "Jev returned a choice or distribution outside offered candidates"
        )
    for value in [confidence, *probabilities.values()]:
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("Jev returned an invalid confidence or probability")
    if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01):
        raise ValueError("Jev distribution does not sum to one")
    return choice, confidence, probabilities


def pick(
    request: PickRequest,
    config: dict[str, Any],
    *,
    repo: Path,
    snapshotter: Callable[..., list[Candidate]] = snapshot.candidates,
    caller: Callable[..., dict[str, Any]] = client.ask,
    records: list[dict[str, Any]] | None = None,
    verdict_inputs: dict[str, Any] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    cached_only: bool = False,
    authority: dict[str, Any] | None = None,
) -> Selection:
    """Return one auditable selection; an excluded default cannot bypass gates."""
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    # One slot per timed stage, so a stage that did not run reads null rather
    # than a measured zero.
    stages: dict[str, float | None] = {
        "snapshot_ms": None,
        "capability_ms": None,
        "estimate_ms": None,
        "state_render_ms": None,
        "questions_render_ms": None,
        "jev_ms": None,
    }
    rows = (
        snapshot.ledger.runs(request.project, root=repo) if records is None else records
    )
    view = (
        budget_snapshot
        if budget_snapshot is not None
        else snapshot.budget_view(
            request.project, config, repo, rows, cached_only=cached_only
        )
    )
    # One docs-tree scan serves every candidate's plan lookup in this pick.
    snapshot_started = time.perf_counter()
    with resource_scan_scope():
        options = snapshotter(
            request,
            config,
            repo,
            records=rows,
            verdict_inputs=verdict_inputs,
            budget_snapshot=view,
            cached_only=cached_only,
            authority=authority,
        )
    stages["snapshot_ms"] = _milliseconds(snapshot_started)
    offered = [candidate for candidate in options if not candidate.reasons]
    excluded = [candidate.as_dict() for candidate in options if candidate.reasons]
    # The node's declared capability and estimate are read from the plan it
    # names, inside the pick's own timing: a plan read that fails leaves both
    # null rather than failing the pick, and its cost is attributed to its own
    # stage so a slow resolve is visible rather than charged to the state render.
    capability_started = time.perf_counter()
    capability, estimated_hours, hours_source = _declared_difficulty(
        request, config, repo=repo, authority=authority
    )
    stages["capability_ms"] = _milliseconds(capability_started)
    state_started = time.perf_counter()
    try:
        rendered = prompts.render(
            "state.jinja",
            node=request.node,
            capability=capability,
            estimated_hours=estimated_hours,
            estimated_hours_source=hours_source,
            estimated_context=request.estimated_context,
            comment=request.comment,
            candidates=offered,
            project=request.project,
            records=rows,
            budget_snapshot=view,
            config=config,
            attempts=request.attempts
            if request.attempts is not None
            else sum(
                row.get("node") == request.node.id
                and row.get("plan") == request.node.plan
                for row in rows
            ),
        )
    finally:
        stages["state_render_ms"] = _milliseconds(state_started)
    estimate_started = time.perf_counter()
    token_estimate = math.ceil(len(rendered) / 4)
    stages["estimate_ms"] = _milliseconds(estimate_started)
    probabilities: dict[str, float] = {}
    confidence = None
    fallback_reason = None
    selected = None
    source = "jev"
    payload: dict[str, Any] = {}
    jev_ms = 0.0
    action = "route"
    if not offered:
        fallback_reason = "no-eligible-candidates"
    else:
        try:
            questions_started = time.perf_counter()
            try:
                questions = json.loads(
                    prompts.render("questions.jinja", candidates=offered)
                )
            finally:
                stages["questions_render_ms"] = _milliseconds(questions_started)
            jev_started = time.perf_counter()
            try:
                payload = caller(
                    json.loads(rendered), questions, env_path=client.credential_path()
                )
                choice, confidence, probabilities = _answer(payload, offered)
                if choice == "hold":
                    action = "hold"
                else:
                    selected = next(c for c in offered if prompts.option_key(c) == choice)
            finally:
                jev_ms = (time.perf_counter() - jev_started) * 1000
                stages["jev_ms"] = round(jev_ms, 3)
        except Exception as exc:  # noqa: BLE001 - every Jev failure must produce a recorded fallback
            # Exception text may contain provider content or credentials; record its type only.
            fallback_reason = f"jev-error: {type(exc).__name__}"
    if fallback_reason:
        source = "flight-default"
        action = "fallback"
        selected = next(
            (c for c in offered if c.backend == config.get("default_backend")), None
        )
        if selected is None:
            fallback_reason += "; default-backend-ineligible"
            source = "refused"
            action = "refuse"
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    _record_pick_timings(
        {
            "started_at": started_at,
            "project": request.project,
            "node": request.node.id,
            "session": request.session,
            "latency_ms": latency_ms,
            "outcome": action,
            "decision_source": source,
            "fallback_reason": fallback_reason,
            **stages,
        },
        latency_ms=latency_ms,
    )
    return Selection(
        action=action,
        backend=selected.backend if selected else None,
        family=selected.family if selected else None,
        model=selected.model if selected else None,
        effort=selected.effort if selected else None,
        probabilities=probabilities,
        confidence=confidence,
        jev_model=client.JEV_MODEL,
        fallback_reason=fallback_reason,
        offered=[c.as_dict() | {"reason": "eligible"} for c in offered],
        excluded=excluded,
        rendered_token_estimate=token_estimate,
        latency_ms=latency_ms,
        decision_source=source,
        comment=request.comment,
        jev_latency_ms=round(jev_ms, 3),
        answering_model=payload.get("model"),
        usage=payload.get("usage") or {},
    )
