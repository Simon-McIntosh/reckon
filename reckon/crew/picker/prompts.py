"""Render the picker's prompts as one JSON mapping per template.

Every value the picker asks Jev to weigh is assembled into a Python mapping and
handed to a Jinja template whose body is that mapping passed through one
``tojson``. A value therefore travels as JSON data: hostile text in a node's
goal, done-when or orchestrator comment escapes into its own string and cannot
add a key, close a brace or rewrite an instruction. The templates hold only the
static prose -- the routing instructions and the hold guidance -- because that
guidance belongs where a reader can compare it against the questions Jev is
asked, not inline in code.
"""

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
    Undefined,
)

from . import lane_context

TEMPLATES_DIR = Path(__file__).parent / "templates"

#: The fields of the routing judgment Jev is not expected to know by name. Each
#: entry is one line of meaning, so a value the state carries can be weighed
#: rather than guessed at.
_GLOSSARY: dict[str, str] = {
    "spec_level": (
        "How much design latitude the node leaves. exact: the design is fixed "
        "and only the implementation remains. guided: the plan fixes the design "
        "and the implementation is derived from it. open: the node must also "
        "choose the design."
    ),
    "burn_multiple": (
        "Metered spend so far divided by the lane's allowance for its window; "
        "above 1 is ahead of the pace that window plans for."
    ),
    "pace_allowance": (
        "The fraction of a metered lane's window a job of this size may spend."
    ),
    "budget_source": (
        "Where a budget reading came from: an account-surface reading describes "
        "now, a ledger reading carries the age of the run behind it."
    ),
    "budget_age_s": "Seconds since the budget reading was observed; null when unknown.",
    "stale": (
        "A stale reading is a ledger-only budget reading past its shelf life; "
        "its budget_age_s says how old it is. An account-surface reading is "
        "never stale by age."
    ),
    "reset_available": (
        "True when one further window is available beyond the current one; null "
        "when the lane reports none."
    ),
    "days_to_reset": "Days until the lane's window resets; null when unknown.",
    "lanes": (
        "One pressure block per lane, however many models the lane holds. A lane "
        "is a subscription or host whose models share one account window, so its "
        "utilisation, burn, pace allowance, reset, worker slots, congestion and "
        "banked-reset flag are stated here once and are the same for every model "
        "in it. availability is the lane's serving state aggregated over those "
        "models. Read a model's own serving observation from its candidate entry; "
        "read the shared spending policy from its lane."
    ),
    "context": (
        "A candidate's context block. window_tokens is the input window that "
        "gates the lane; estimated_tokens is this node's deterministic input "
        "estimate (standing instructions plus the repository files its brief "
        "loads) measured against that window; headroom_pct is the share of the "
        "window the estimate leaves free. peak_utilisation_p50_pct and "
        "peak_utilisation_p90_pct are the median and 90th-percentile peak input "
        "utilisation of recent passed runs on the lane, with "
        "peak_utilisation_runs behind them. A run whose input approaches its "
        "window risks dying mid-way, so a small headroom is a real risk to be "
        "weighed with pressure. A null figure is unknown, never zero."
    ),
}


@lru_cache(maxsize=1)
def environment() -> Environment:
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(TEMPLATES_DIR / "shared"),
                FileSystemLoader(TEMPLATES_DIR),
            ]
        ),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - JSON prompts, never HTML
    )

    def encode(value: Any) -> str:
        if isinstance(value, Undefined):
            str(value)
        return json.dumps(value, separators=(",", ":"))

    env.filters["json"] = encode
    env.filters["tojson"] = encode
    return env


def _negative_control_declared(node: Any) -> bool:
    """Whether the node declares a control its check must fail against.

    ``none: <reason>`` states that no control applies, so an explicit refusal
    reads the same as an empty field: neither declares one.
    """

    declared = str(getattr(node, "negative_control", "") or "").strip()
    return bool(declared) and not declared.lower().startswith("none")


def _candidate_state(candidate: Any) -> dict[str, Any]:
    """One candidate's weighable facts, keyed for the judgment.

    ``lane`` and ``model`` are separate fields beside the candidate's own
    ``backend`` name, so each entry can be weighed both under the whole
    candidate table and under the lane and model a choice names. The four budget
    fields are read only when the candidate carries them -- a candidate without
    a recorded budget reading renders null, never a measured zero.
    """

    return {
        "backend": candidate.backend,
        "lane": candidate.family,
        "model": candidate.model,
        "availability": candidate.availability,
        "utilisation_pct": candidate.utilisation_pct,
        "burn_multiple": candidate.burn_multiple,
        "pace_allowance": candidate.pace_allowance,
        "days_to_reset": candidate.days_to_reset,
        "resets_at": candidate.resets_at,
        "worker_slots": candidate.worker_slots,
        "congestion": candidate.congestion,
        "outcomes": candidate.outcomes,
        "context": getattr(candidate, "context", None),
        "budget_source": getattr(candidate, "budget_source", None),
        "budget_age_s": getattr(candidate, "budget_age_s", None),
        "stale": getattr(candidate, "stale", None),
        "reset_available": getattr(candidate, "reset_available", None),
    }


def build_state(
    *,
    node: Any,
    capability: Any,
    estimated_context: Any,
    comment: Any,
    candidates: Sequence[Any],
    lane: Mapping[str, Any],
    attempts: Any = 0,
) -> dict[str, Any]:
    """Assemble the whole routing judgment as one mapping.

    The mapping is the only source of the rendered state, so the structure holds
    whatever the values are: every candidate is keyed by its backend name and
    every node fact is one field of ``node``.
    """

    return {
        "node": {
            "role": node.role,
            "spec_level": node.spec_level,
            "capability": capability,
            "goal": node.goal,
            "done_when": node.done_when,
            "estimated_context": estimated_context,
            "estimated_hours": node.estimated_hours,
            "attempts": attempts,
            "write_path_count": len(node.write_paths or []),
            "negative_control_declared": _negative_control_declared(node),
        },
        "orchestrator_comment": comment,
        "candidates": {
            candidate.backend: _candidate_state(candidate) for candidate in candidates
        },
        "return_times": lane["return_times"],
        "lanes": lane["lanes"],
        "local_lane": lane["local_lane"],
    }


def option_key(candidate: Any) -> str:
    """The key Jev chooses a candidate by: its lane and model joined as one pair.

    A pick has two linked parts, so an option is named ``<lane>:<model>``. A
    candidate that names no model uses its backend name for the second part,
    which keeps the pair form and keeps every offered key distinct.
    """

    lane = getattr(candidate, "family", None) or getattr(candidate, "backend", "")
    model = getattr(candidate, "model", None) or getattr(candidate, "backend", "")
    return f"{lane}:{model}"


def build_questions(candidates: Sequence[Any]) -> dict[str, Any]:
    """Assemble the questions as one mapping.

    The criteria carry one entry per offered lane-and-model pair plus the
    ``hold`` guidance the template supplies; the entries are values of a
    mapping keyed by that pair, so no candidate name can change the shape of
    the questions. Each entry also names the pair's parts and its backend, so
    a chosen pair resolves to exactly one candidate.
    """

    return {
        "glossary": _GLOSSARY,
        "criteria_entries": {
            option_key(candidate): {
                "backend": candidate.backend,
                "lane": candidate.family,
                "model": candidate.model,
                "effort": candidate.effort,
                "local": candidate.local,
                "meaning": (
                    "Execute the node as this lane and model pair; read the "
                    "lane's shared pressure from the lanes block and this "
                    "pair's own fit and serving observation from the candidate "
                    "table."
                ),
            }
            for candidate in candidates
        },
    }


def render(name: str, **context: Any) -> str:
    if name == "state.jinja":
        # The lane context is built here so every caller of the state render
        # carries the return-time and local-lane blocks without duplicating the
        # derivation. A caller that supplies no project gets null figures.
        lane = lane_context.build(
            node=context.get("node"),
            candidates=context.get("candidates") or [],
            project=context.get("project"),
            records=context.get("records"),
            budget_snapshot=context.get("budget_snapshot"),
            config=context.get("config"),
            now=context.get("now"),
        )
        state = build_state(
            node=context["node"],
            capability=context.get("capability"),
            estimated_context=context.get("estimated_context"),
            comment=context.get("comment"),
            candidates=context.get("candidates") or [],
            lane=lane,
            attempts=context["attempts"] if context.get("attempts") is not None else 0,
        )
        return environment().get_template(name).render(state=state).strip()
    if name == "questions.jinja":
        questions = build_questions(context.get("candidates") or [])
        return (
            environment()
            .get_template(name)
            .render(
                glossary=questions["glossary"],
                criteria_entries=questions["criteria_entries"],
            )
            .strip()
        )
    return environment().get_template(name).render(**context).strip()