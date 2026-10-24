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
from datetime import UTC, datetime
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

from reckon._timestamps import parse_utc

from . import lane_context
from .lane_context import option_key

TEMPLATES_DIR = Path(__file__).parent / "templates"

#: The declared risk at or above which a lifted metered lane is preferred over
#: the local lane. A lift is the lead saying the lane's window is meant to be
#: spent, and the work worth spending it on is work whose failure is expensive;
#: below this bar the local lane keeps the work while it has room.
LIFT_PREFERENCE_RISK = "elevated"

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
    "suits": (
        "The operator's declared statement of the work this candidate's model "
        "suits, which may include its cost relative to the lane's other models. "
        "Read from the model's own declaration or, when it declares none, from a "
        "lane-level declaration. It is a prior to weigh beside the candidate's "
        "outcomes and never in place of them. Present only on a candidate whose "
        "model or lane declares one; an absent key means none is declared."
    ),
    "burn_multiple": (
        "Metered spend so far divided by the lane's allowance for its window; "
        "above 1 is ahead of the pace that window plans for."
    ),
    "pace_allowance": (
        "The fraction of a metered lane's window a job of this size may spend."
    ),
    "burn": (
        "A metered lane's own burn block. window_rate_pct_per_hour is the "
        "window's utilisation change per hour over the trailing three hours, "
        "read from the lane's recorded readings rather than averaged over the "
        "whole window, so it describes what the lane is spending now and is "
        "present in a window too young for burn_multiple; null when fewer than "
        "two readings lie at least half an hour apart. window_even_share_pct is "
        "the even pace for the coming period -- the window's remaining "
        "percentage over the hours to its reset -- given as a reference to weigh "
        "against the node's priority and difficulty, never applied as a limit. "
        "Under a credits lift the block also carries credit_pool_remaining and "
        "credit_pool_cap (the lift's dispatch stop less the credits drawn since "
        "grant, and its cap), credit_rate_per_hour (the balance's fall per hour "
        "over the same trailing span) and credit_even_share (the pool's even "
        "pace to the lift's end); those four are absent with no credits lift. "
        "runway_hours is the credit pool over the credit rate under a lift, and "
        "the window's remainder over its own rate without one; null when the "
        "rate behind it is null or zero. A lane whose burn is racing ahead of "
        "its even share will exhaust before its reset, and a credits-lifted "
        "lane's pool is spent by priority: it is a budget for the work that "
        "needs the lane, not an even pace to the reset."
    ),
    "budget_source": (
        "Where a budget reading came from: a document reading is taken from the "
        "published paid-lanes document, an account-surface reading describes now, "
        "and a ledger reading carries the age of the run behind it."
    ),
    "budget_age_s": "Seconds since the budget reading was observed; null when unknown.",
    "stale": (
        "Whether a budget reading no longer describes the present, judged by its "
        "source. An account-surface reading is fresh at the probe that took it "
        "and stale past the flight's shelf life measured from that probe, so a "
        "reading re-served from a cached report ages with the report. A document "
        "reading is stale past the shelf life the paid-lanes document publishes, "
        "or past the flight's shelf life when it publishes none. A ledger "
        "reading is stale past the flight's shelf life. budget_age_s says how "
        "old the reading is."
    ),
    "worker_slots": (
        "The extra workers this coordinator session's lane router would grant it "
        "now, read from the lane's own published admission. Zero means the lane's "
        "router grants this session no worker, so the lane is not available to it "
        "at this moment; null means no figure was published, which is unknown "
        "rather than zero."
    ),
    "lift": (
        "A lift in force on the lane's budget group: its form (uncapped, "
        "drain_by or multiple), who granted it, and when it ends. A lane under a "
        "lift is one whose window is meant to be spent, so its burn and pace do "
        "not press it while the lift stands."
    ),
    "lift_preference": (
        "The lane a lift in force makes the preferred home for this node: its "
        "form and scope, the lift block itself, and the risk the node declared "
        "to earn it. Present only when the node's declared or assessed risk is "
        "at or above elevated and exactly one metered lane carrying a lift "
        "serves the node with room. Prefer it for that node -- route there "
        "unless serving state, context fit, worker slots or review exclusion "
        "forbid it -- and null means no lift moves this choice, so well-defined "
        "low-risk work stays local while the local lane has room."
    ),
    "urgency": (
        "The dispatch deadline and reason. due is a UTC instant or null; "
        "seconds_to_due is its remaining time at the pick, or null when no "
        "deadline was given. Compare it with local_lane.expected_wait_s and "
        "local_lane.queue_position (the next estimated position from the "
        "published waiting count; fair-share admission may reorder it). "
        "A reason without a deadline is still "
        "context for the urgency judgment, never permission to bypass fit."
    ),
    "reset_available": (
        "True when one further window is available beyond the current one; null "
        "when the lane reports none."
    ),
    "days_to_reset": "Days until the lane's window resets; null when unknown.",
    "expected_wait_profile_age_s": (
        "The age in seconds of the oldest foreign project's run-time profile "
        "that contributed to expected_wait_s, counted from the instant its "
        "promotion published it; null when no foreign profile contributed. A "
        "pick reads a published profile rather than rebuilding one, so this "
        "names how stale the reading behind the wait is."
    ),
    "lanes": (
        "One pressure block per lane, however many models the lane holds. A lane "
        "is a subscription or host whose models share one account window, so its "
        "utilisation, burn, pace allowance, reset, worker slots, congestion and "
        "banked-reset flag are stated here once and are the same for every model "
        "in it. availability is the lane's serving state aggregated over those "
        "models. Read a model's own serving observation from its candidate entry; "
        "read the shared spending policy from its lane."
    ),
    "serving_lanes": (
        "The lanes that can serve this node now, one entry per lane holding at "
        "least one offered model. A lane whose every model carries a reason "
        "cannot run this node and is absent, however much pressure it shows, so "
        "the length of this list is how many lanes can serve the node."
    ),
    "hold_waits_for": (
        "What a hold waits on. When exactly one lane can serve the node a hold "
        "waits for that lane alone: waiting for any other lane to free changes "
        "nothing, because it cannot run this node. Null when no lane can serve "
        "the node."
    ),
    "outcomes_at_requirement": (
        "A candidate's gate history at this node's requirement: the same passed, "
        "failed, not-run and unknown counts as outcomes, but over only the rows "
        "whose recorded requirement (reasoning and risk) equals this node's. "
        "reviewed is the composition of the sample behind the dimension medians: "
        "how many rows a review scored on every dimension, and "
        "review_dimensions_median is the median of each dimension over exactly "
        "those rows, each scored 0 to 20, so a low dimension shows even when the "
        "total does not. review_total_median and wall_seconds_median are the "
        "median review score and wall time over those rows, null when none was "
        "recorded; review_total_median can rest on a different number of rows "
        "than reviewed, because a total is sometimes withheld and reviews scored "
        "before reuse existed carry a total without it. "
        "attempts_median is the median number of launches those rows took, and "
        "passed_first_attempt is how many of them passed on the first launch; a "
        "launch beyond the first is a resume, and resumes include the repairs "
        "that answer a blocking review, so a pass on the fifth launch is not a "
        "pass on the first. Rows that "
        "recorded no requirement count under unrecorded and are never matched, so "
        "history written before a requirement was recorded reads as unknown "
        "rather than as a match. These counts are the history at this node's "
        "difficulty, not a calibrated ranking."
    ),
    "moved_in": (
        "A candidate's gate history over the runs a coordinator moved onto it "
        "from another lane: a run Jev routed elsewhere, whose lineage records a "
        "lane change onto this candidate. It uses the same passed, failed, "
        "not-run and unknown counts as outcomes, but those runs are kept out of "
        "outcomes and outcomes_at_requirement, because their outcome is the "
        "outcome of the pick they were chosen for, not of this lane's own picks. "
        "Read at the request's requirement when it names one, and over every "
        "move when it names none. Present only when at least one such move "
        "exists; an absent key means no coordinator moved a run onto this "
        "candidate in the window."
    ),
    "moved_off": (
        "How many of this candidate's own picks a coordinator moved away to "
        "another lane. Read at the request's requirement when it names one, and "
        "over every move when it names none. A high moved_off "
        "means coordinators overrode the choice this candidate was picked for, "
        "so the lane never ran the nodes it was chosen for. Present only when it "
        "is greater than zero; an absent key means no coordinator moved this "
        "candidate's pick away in the window."
    ),
    "prior_outcomes": (
        "A candidate's lane model key's earlier generations, rendered only for a "
        "candidate whose own model id carries too few outcomes to weigh on their "
        "own. It uses the same passed, failed, not-run and unknown counts as "
        "outcomes, but over only the runs recorded under this candidate's lane "
        "and model key with a different model id, so a key re-pointed to a new "
        "generation shows the record its predecessor built. model_ids names each "
        "of those earlier ids. Read it as a prior: it is the key's own history "
        "weighed below the candidate's own outcomes, never in place of them, and "
        "it is absent once the candidate's own outcomes are enough to weigh on "
        "their own."
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


def encode(value: Any) -> str:
    """The state's one encoder.

    Every rendered prompt is a JSON document, so one function serialises it and
    a rendered state is exactly what this returns for the mapping
    ``build_state`` assembled. A value the template left undefined raises here
    rather than serialising to a silently missing field.
    """

    if isinstance(value, Undefined):
        str(value)
    return json.dumps(value, separators=(",", ":"))


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
    env.filters["json"] = encode
    env.filters["tojson"] = encode
    return env


def lift_preference(
    candidates: Sequence[Any], declared: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The lifted metered lane this node should prefer, or None.

    A lane qualifies when one of its models is among the candidates offered --
    an offered candidate is one the budget gate left, so the lane has window
    room -- its congestion block carries the lift in force for this request's
    session or project, and it is not the one the pick would otherwise make.
    The preference is stated only for work at or above
    :data:`LIFT_PREFERENCE_RISK`: an ordinary node keeps the local lane, which
    is what the local lane having room is for.

    ``declared`` is the requirement mapping the node or its coordinator
    declared. A node declaring no risk has not stated that the work is worth a
    metered window, so it gets no preference.

    Two lifted lanes are left to the pick's own weighing: this states one
    preference and never ranks lanes against each other.
    """
    from reckon.capability import REQUIREMENT_LEVELS, _rank

    levels = REQUIREMENT_LEVELS.get("risk")
    value = (declared or {}).get("risk")
    if not levels or not isinstance(value, str) or value not in levels:
        return None
    if _rank(value, levels) < _rank(LIFT_PREFERENCE_RISK, levels):
        return None
    lifted: list[tuple[str, str, Mapping[str, Any]]] = []
    for candidate in candidates:
        if getattr(candidate, "local", False):
            continue
        congestion = getattr(candidate, "congestion", None)
        lift = congestion.get("lift") if isinstance(congestion, Mapping) else None
        if not isinstance(lift, Mapping):
            continue
        lifted.append(
            (
                str(getattr(candidate, "family", "") or ""),
                str(getattr(candidate, "backend", "") or ""),
                lift,
            )
        )
    if not lifted or len({lane for lane, _backend, _lift in lifted}) != 1:
        return None
    lane, backend, lift = lifted[0]
    return {
        "lane": lane,
        "backend": backend,
        "risk": value,
        "lift": {
            key: lift.get(key)
            for key in ("id", "form", "scope", "ends_at", "pace_multiple")
        },
    }


def _negative_control_declared(node: Any) -> bool:
    """Whether the node declares a control its check must fail against.

    ``none: <reason>`` states that no control applies, so an explicit refusal
    reads the same as an empty field: neither declares one.
    """

    declared = str(getattr(node, "negative_control", "") or "").strip()
    return bool(declared) and not declared.lower().startswith("none")


def _holds_a_move(moved_in: Any) -> bool:
    """Whether a candidate's ``moved_in`` block holds at least one move.

    ``moved_in`` uses the gate vocabulary, so a candidate no coordinator moved a
    run onto carries a block of zeros. Those carry no information, so the state
    renders neither the block nor ``moved_off`` for such a candidate.
    """

    if not isinstance(moved_in, dict):
        return False
    return any(int(value or 0) > 0 for value in moved_in.values())


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
        # The burn block is rendered only on a metered lane, the way suits is
        # rendered only where one is declared: a lane with no account window has
        # no burn, and a candidate carrying no block adds nothing to the state.
        **({"burn": candidate.burn} if getattr(candidate, "burn", None) else {}),
        "days_to_reset": candidate.days_to_reset,
        "resets_at": candidate.resets_at,
        "worker_slots": candidate.worker_slots,
        "congestion": candidate.congestion,
        "outcomes": candidate.outcomes,
        # The at-requirement block is rendered only when the request named a
        # requirement to read history at; a pick that named none carries no such
        # block, so the state keeps its shape for every pick that does not
        # condition on difficulty.
        **(
            {"outcomes_at_requirement": candidate.outcomes_at_requirement}
            if getattr(candidate, "outcomes_at_requirement", None)
            else {}
        ),
        # The move figures are rendered sparsely, for every pick: moved_in only
        # when a coordinator moved a run onto this candidate, moved_off only
        # when one moved this candidate's pick away, whether or not the request
        # names a requirement. A candidate with no moves in the window carries
        # neither key, so a state with no moves is exactly as large as it
        # was before the move figures existed and the prompt keeps within its
        # size budget. An absent key means no coordinator moved a run onto, or
        # off, this candidate in the window. When the request names a requirement
        # the figures are read at it, and otherwise over every move.
        **(
            {"moved_in": candidate.moved_in}
            if _holds_a_move(getattr(candidate, "moved_in", None))
            else {}
        ),
        **(
            {"moved_off": candidate.moved_off}
            if (getattr(candidate, "moved_off", 0) or 0) > 0
            else {}
        ),
        # The declared statement of what work this model suits is rendered only
        # when the candidate carries one, from its model or its lane, the way
        # moved_in is. A candidate with none declared carries no key, so a
        # state whose candidates all declare none is exactly as large as it was
        # before this fact existed and the prompt keeps within its size budget.
        # An absent key means neither the model nor its lane declares one.
        **({"suits": candidate.suits} if getattr(candidate, "suits", None) else {}),
        # The prior block is rendered only for a candidate whose own model id
        # carries too few outcomes to weigh on their own, the way moved_in is.
        # A candidate standing on its own record carries no key, so a state
        # whose every candidate already has history is exactly as large as it
        # was before prior blocks existed and the prompt keeps within its size
        # budget. An absent key means the candidate carries no prior block.
        **(
            {"prior_outcomes": candidate.prior_outcomes}
            if getattr(candidate, "prior_outcomes", None)
            else {}
        ),
        "context": getattr(candidate, "context", None),
        "budget_source": getattr(candidate, "budget_source", None),
        "budget_age_s": getattr(candidate, "budget_age_s", None),
        "stale": getattr(candidate, "stale", None),
        "reset_available": getattr(candidate, "reset_available", None),
    }


def _serving_lanes(candidates: Sequence[Any]) -> list[str]:
    """The lanes that can serve this node, one entry per lane.

    A lane serves the node when at least one of its models is offered: a
    candidate carrying a reason was removed because it cannot run, so the lanes
    left across the offered candidates are the lanes a pick could send this node
    to. The grouping is the one shared with the lanes block, so a candidate with
    no family counts as a lane here exactly as it does there.
    """

    return list(lane_context.lane_groups(candidates))


def _hold_waits_for(serving_lanes: Sequence[str]) -> str | None:
    """One line naming what a hold waits on, read from the serving lanes.

    With a single serving lane a hold waits for that lane alone, because no
    other can run the node; with several it waits for any one of them to have
    room; with none there is nothing to wait for and the field is null.
    """

    if not serving_lanes:
        return None
    if len(serving_lanes) == 1:
        return f"the {serving_lanes[0]} lane, the only lane that can serve this node"
    return (
        f"any one of {len(serving_lanes)} lanes that can serve this node to have room"
    )


def build_state(
    *,
    node: Any,
    capability: Any,
    estimated_context: Any,
    comment: Any,
    candidates: Sequence[Any],
    lane: Mapping[str, Any],
    attempts: Any = 0,
    estimated_hours: Any = None,
    estimated_hours_source: Any = None,
    orchestrator_assessment: Any = None,
) -> dict[str, Any]:
    """Assemble the whole routing judgment as one mapping.

    The mapping is the only source of the rendered state, so the structure holds
    whatever the values are: every candidate is keyed by its option key -- its
    lane, model and backend -- so the criteria key a pick chooses by finds exactly
    one row, and every node fact is one field of ``node``. ``estimated_hours`` overrides the
    node's own figure when a caller resolved the plan's declaration, and
    ``estimated_hours_source`` names where that figure came from -- the node,
    the section, or the plan. The source is rendered unconditionally, null when
    nothing resolved, so the node's shape does not vary with which facts a pick
    could read.

    ``orchestrator_assessment`` is the coordinator's own difficulty reading. It
    is placed beside the declared ``capability`` rather than in place of it, so
    the whole state is built here and encoded once by one encoder. A pick that
    supplied no reading carries no such key, so the node block keeps its shape.
    """

    serving_lanes = _serving_lanes(candidates)
    declared_urgency = getattr(node, "urgency", None) or {}
    due = declared_urgency.get("due")
    parsed_due = parse_utc(due) if due else None
    node_state: dict[str, Any] = {
        "role": node.role,
        "spec_level": node.spec_level,
        "capability": capability,
        **(
            {"orchestrator_assessment": orchestrator_assessment}
            if orchestrator_assessment
            else {}
        ),
        "goal": node.goal,
        "done_when": node.done_when,
        "urgency": {
            "due": due,
            "seconds_to_due": (
                round((parsed_due - datetime.now(UTC)).total_seconds(), 3)
                if parsed_due is not None
                else None
            ),
            "reason": declared_urgency.get("reason") or None,
        },
        "estimated_context": estimated_context,
        "estimated_hours": (
            node.estimated_hours if estimated_hours is None else estimated_hours
        ),
        "estimated_hours_source": estimated_hours_source,
        "attempts": attempts,
        "write_path_count": len(node.write_paths or []),
        "negative_control_declared": _negative_control_declared(node),
        # How many lanes can serve this node, and what a hold waits on. A node
        # whose other lanes are excluded by role cannot be served by them
        # however much pressure they show, so a hold that does not name the
        # serving lane waits on nothing it can reach. These describe every lane
        # that can serve the node, so they are node-scoped rather than facts of
        # the local lane.
        "serving_lanes": serving_lanes,
        "serving_lane_count": len(serving_lanes),
        "hold_waits_for": _hold_waits_for(serving_lanes),
        # The lane a lift in force makes the preferred home for this node, or
        # null. It is derived here rather than left for the pick to infer from
        # the candidate table so the rule that moves a choice is one function,
        # and so the dispatch half can read back the same answer from the state
        # it recorded.
        "lift_preference": lift_preference(
            candidates,
            {
                **((capability or {}).get("requirements") or {}),
                **(orchestrator_assessment or {}),
            },
        ),
    }
    return {
        "node": node_state,
        "orchestrator_comment": comment,
        "candidates": {
            option_key(candidate): _candidate_state(candidate)
            for candidate in candidates
        },
        "return_times": lane["return_times"],
        "lanes": lane["lanes"],
        "local_lane": lane["local_lane"],
    }


def build_questions(candidates: Sequence[Any]) -> dict[str, Any]:
    """Assemble the questions as one mapping.

    The criteria carry one entry per offered lane, model and backend plus the
    ``hold`` guidance the template supplies; the entries are values of a mapping
    keyed by that option, so no candidate name can change the shape of the
    questions. Each entry also names the option's parts, so a chosen key resolves
    to exactly one candidate. Two candidates under one key cannot be offered
    separately, so offering one would silently drop the other; that is refused
    rather than resolved to whichever candidate happened to be seen first.
    """

    entries: dict[str, Any] = {}
    backends: dict[str, str] = {}
    for candidate in candidates:
        key = option_key(candidate)
        if key in entries:
            raise ValueError(
                f"two candidates are offered under one option key {key!r}: "
                f"{backends[key]!r} and {candidate.backend!r}"
            )
        backends[key] = candidate.backend
        entries[key] = {
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
    return {"glossary": _GLOSSARY, "criteria_entries": entries}


def _render_state(name: str, context: Mapping[str, Any]) -> str:
    """Build the lane context and render the state template.

    The lane context is built here so every caller of the state render carries
    the return-time and local-lane blocks without duplicating the derivation. A
    caller that supplies no project gets null figures. The template load is
    timed as one of the render's reads; the state mapping it renders is built
    exactly as it would be untimed.
    """

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
        estimated_hours=context.get("estimated_hours"),
        estimated_hours_source=context.get("estimated_hours_source"),
        comment=context.get("comment"),
        candidates=context.get("candidates") or [],
        lane=lane,
        attempts=context["attempts"] if context.get("attempts") is not None else 0,
        orchestrator_assessment=context.get("orchestrator_assessment"),
    )
    with lane_context.timed_read("template_load"):
        template = environment().get_template(name)
    return template.render(state=state).strip()


def render(name: str, **context: Any) -> str:
    if name == "state.jinja":
        # The state render's reads go to shared storage, so each is timed and
        # appended beside pick-timings.jsonl; the counter changes no decision and
        # no rendered prompt, so the render with it off is byte-identical.
        if lane_context.state_read_timings_enabled():
            with lane_context.timing_state_reads():
                return _render_state(name, context)
        return _render_state(name, context)
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
