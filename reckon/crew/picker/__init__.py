"""Select a configured backend with hard eligibility checks and typed judgment."""

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from reckon.resources import resource_scan_scope

from . import client, prompts, snapshot
from .types import Candidate, PickRequest, Selection

__all__ = ["Candidate", "PickRequest", "Selection", "pick"]


def _answer(
    payload: dict[str, Any], offered: list[Candidate]
) -> tuple[str, float, dict[str, float]]:
    answer = payload["answers"]["route"]
    choice = answer["choice"]
    keys = {candidate.backend for candidate in offered} | {"hold"}
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
    offered = [candidate for candidate in options if not candidate.reasons]
    excluded = [candidate.as_dict() for candidate in options if candidate.reasons]
    rendered = prompts.render(
        "state.jinja",
        node=request.node,
        capability=request.capability,
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
            row.get("node") == request.node.id and row.get("plan") == request.node.plan
            for row in rows
        ),
    )
    token_estimate = math.ceil(len(rendered) / 4)
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
        call_started = time.perf_counter()
        try:
            questions = json.loads(
                prompts.render("questions.jinja", candidates=offered)
            )
            payload = caller(
                json.loads(rendered), questions, env_path=client.credential_path()
            )
            choice, confidence, probabilities = _answer(payload, offered)
            if choice == "hold":
                action = "hold"
            else:
                selected = next(c for c in offered if c.backend == choice)
        except Exception as exc:  # noqa: BLE001 - every Jev failure must produce a recorded fallback
            # Exception text may contain provider content or credentials; record its type only.
            fallback_reason = f"jev-error: {type(exc).__name__}"
        finally:
            jev_ms = (time.perf_counter() - call_started) * 1000
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
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        decision_source=source,
        comment=request.comment,
        jev_latency_ms=round(jev_ms, 3),
        answering_model=payload.get("model"),
        usage=payload.get("usage") or {},
    )
