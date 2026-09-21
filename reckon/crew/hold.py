"""A hold carries the evidence that produced it, or it does not hold.

A governor that holds on a figure it cannot vouch for is worse than none: the
hold looks identical either way, so the coordinator routes around it and the
lane is starved by a block nobody can argue with. A hold therefore names four
facts — the group it holds, the figure it rested on, that figure's observation
age, and the pool that observed it — and a hold missing any one of the four is
refused by construction rather than rendered with a blank field that reads like
a hold.

Three refusals keep the hold real rather than merely labelled.

*A figure past its shelf life is re-queried before it is held on.* The hold
path resolves the position through the reader that owns that rule, so a hold
never rests on a window that has already closed, and the figure it carries is
the one that came back rather than the one that provoked the question.

*``unknown`` does not hold.* A re-query that cannot answer leaves the reading's
serving state ``unknown``, and a figure whose state is unknown justifies no
hold — neither as a fresh-looking number nor as a headroom that was never
observed. The asymmetry is deliberate and must not be weakened: a missed hold
costs some quota, while a false hold costs the lane.

*A borrowed figure never justifies a hold.* A reading names the pool it was
observed from. Where that pool is not the group the hold would name, the figure
belongs to a pool this group does not declare — a borrowed probe reading — and
it never becomes that group's position, so it can never be the evidence a hold
rests on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from reckon.crew import staleness

#: The four facts a hold cannot be constructed without, in the order a caller
#: omitting several is told about them.
HOLD_EVIDENCE_FIELDS = ("group", "figure", "age_seconds", "source")


class MissingHoldEvidenceError(ValueError):
    """A hold was built without one of the four facts that make it real.

    Carries the offending field's name on ``field`` so a caller can branch on
    which fact is missing without parsing the message.
    """

    def __init__(self, field: str) -> None:
        super().__init__(
            f"a hold cannot be constructed without its {field}: a hold that "
            "cannot state the evidence it rested on is indistinguishable from "
            "one that holds for no reason"
        )
        self.field = field


@dataclass(frozen=True, slots=True)
class Hold:
    """A block, carrying the four facts that produced it.

    ``figure`` is the utilisation the hold rested on, ``age_seconds`` how old
    that figure was when the hold was taken, and ``source`` the pool it was
    observed from — and ``group`` the pool the hold names. None of the four
    defaults: an omitted one raises ``MissingHoldEvidenceError`` rather than being
    rendered as a blank field.
    """

    group: str | None = None
    figure: float | None = None
    age_seconds: float | None = None
    source: str | None = None
    ceiling: float | None = None

    def __post_init__(self) -> None:
        for field in HOLD_EVIDENCE_FIELDS:
            if _absent(getattr(self, field)):
                raise MissingHoldEvidenceError(field)


def hold_from_position(
    position: Any,
    *,
    probe: staleness.Probe,
    ceiling: float,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Hold | None:
    """Return the hold a group's position justifies, or ``None`` if none does.

    ``position`` is a ``budget_group.GroupPosition``: its ``group`` is the
    declared pool the hold would name, and its ``reading`` is the figure the
    group's position rests on. The position's reading states, under ``source``,
    the pool it was observed from; a figure whose pool is not the group's own
    is borrowed and is refused here before anything else is asked of it.

    The figure is resolved through ``staleness`` so a reading past its
    configured shelf life is re-queried and the hold rests on what comes back.
    ``None`` is the answer in four distinct cases that must not be collapsed
    into one: the position carries no figure; the figure's pool is not the
    group's; the resolved reading's serving state is ``unknown`` — which
    includes a re-query that could not answer; and the figure does not reach
    ``ceiling``. Only a figure owned by the group, known to describe now, and
    at or above the ceiling becomes a ``Hold``.
    """
    group = getattr(position, "group", None)
    reading = getattr(position, "reading", None)
    if not isinstance(group, str) or not group.strip():
        return None
    if not isinstance(reading, Mapping):
        return None

    resolved = staleness.resolve_configured_reading(
        _as_reading(reading),
        probe=probe,
        config=config,
        now=now,
    )
    if _borrowed(resolved.source, group):
        return None
    if resolved.serving_state == staleness.SERVING_STATE_UNKNOWN:
        return None
    if resolved.used_percent is None or resolved.age_seconds is None:
        return None
    if resolved.used_percent < ceiling:
        return None
    return Hold(
        group=group,
        figure=resolved.used_percent,
        age_seconds=resolved.age_seconds,
        source=resolved.source,
        ceiling=ceiling,
    )


def _borrowed(source: str | None, group: str) -> bool:
    """Whether a figure's pool is a group the held group does not declare.

    The position's reading names the pool it was observed from. A figure whose
    pool is blank cannot be shown to be owned, and one naming a pool other than
    the held group is a borrowed probe reading: it belongs to an account this
    group does not declare, so it is never that group's position and never the
    evidence a hold rests on.
    """
    return not isinstance(source, str) or source.strip() != group


def _as_reading(reading: Mapping[str, Any]) -> staleness.Reading:
    """Adapt a position's reading mapping to the staleness reader's input."""
    source = reading.get("source")
    state = reading.get("serving_state")
    return staleness.Reading(
        used_percent=_as_number(reading.get("used_percent")),
        observed_at=_as_moment(reading.get("observed_at")),
        source=source.strip() if isinstance(source, str) else "",
        serving_state=(
            state if isinstance(state, str) else staleness.SERVING_STATE_UNKNOWN
        ),
    )


def _absent(value: object) -> bool:
    """Whether a fact is missing: ``None`` or a blank string, never a zero.

    A figure of ``0.0`` and an age of ``0`` are measurements and stay — only a
    value that is absent (``None``) or blank is refused. Reading presence this
    way rather than by truthiness is what keeps a real zero from being
    mistaken for a missing fact.
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _as_number(value: object) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_moment(value: object) -> datetime | None:
    """Return an observation stamp as an aware instant, or ``None`` if undated."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    return None
