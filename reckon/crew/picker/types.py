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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
