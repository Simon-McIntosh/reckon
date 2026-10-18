"""Typed inputs and auditable decisions for backend selection."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from reckon.crew.node import TaskNode


@dataclass
class PickRequest:
    project: str
    node: TaskNode
    capability: dict[str, Any] = field(default_factory=dict)
    estimated_context: int = 0
    comment: str = ""
    session: str = ""
    attempts: int | None = None
    # The coordinator's own reading of the node's difficulty, in the same
    # capability requirement keys the section declares. It travels on the
    # request and renders beside the declared capability, never in place of it,
    # so a pick weighs the two readings where they disagree.
    orchestrator_assessment: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    backend: str
    family: str
    model: str | None
    effort: str | None
    local: bool
    availability: str
    utilisation_pct: float | None
    burn_multiple: float | None
    pace_allowance: float | None
    resets_at: str | None
    worker_slots: int | float | None
    congestion: dict[str, Any] | None
    outcomes: dict[str, int]
    reasons: list[str] = field(default_factory=list)
    days_to_reset: float | None = None
    context: dict[str, Any] | None = None
    # The candidate's gate history at the request's requirement: the same
    # outcome vocabulary over the rows whose recorded requirement matches, plus
    # the review-total and wall-time medians over those rows. A separate block
    # from ``outcomes``, which stays pooled across every difficulty.
    outcomes_at_requirement: dict[str, Any] | None = None
    # A candidate's moves off and onto it. ``moved_in`` is the same gate
    # vocabulary over the rows a coordinator moved away from another pick and
    # onto this candidate, kept out of ``outcomes`` and
    # ``outcomes_at_requirement``; ``moved_off`` is how many of this candidate's
    # own picks a coordinator moved away. Both are read at the request's
    # requirement when it names one, and over every move when it names none, and
    # both are rendered sparsely: ``moved_in`` only when it holds at least one
    # move and ``moved_off`` only when it is greater than zero, so an absent key
    # means no coordinator moved a run onto, or off, this candidate in the
    # window.
    moved_in: dict[str, int] | None = None
    moved_off: int | None = None
    # The operator's declared statement of the work this candidate's model
    # suits, read from the same merged per-model mapping as ``effort``: a
    # model's own declaration overrides one on its lane block, and a lane-level
    # declaration reaches a model declaring none. Declared, never measured, so
    # it is a prior to weigh beside the outcomes rather than in place of them.
    # Null when neither the model nor its lane declares one.
    suits: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Selection:
    backend: str | None
    family: str | None
    model: str | None
    effort: str | None
    probabilities: dict[str, float]
    confidence: float | None
    jev_model: str
    fallback_reason: str | None
    offered: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    rendered_token_estimate: int
    latency_ms: float
    decision_source: str
    comment: str
    action: Literal["route", "hold", "fallback", "refuse"] = "route"
    jev_latency_ms: float = 0.0
    answering_model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    # The exact request Jev was shown, so a hold can be replayed rather than
    # rebuilt from run records. ``rendered_state`` is the state JSON as sent and
    # ``questions`` is the questions mapping as sent; ``template_digest``
    # identifies the templates that produced them. A replay sends the identical
    # request only when all three agree.
    rendered_state: str = ""
    questions: dict[str, Any] = field(default_factory=dict)
    template_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
