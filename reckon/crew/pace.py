"""Derive a budget group's five-hour allowance from the week it has to last.

A fixed five-hour cap cannot react to load, so the allowance is derived
instead: the fraction of the group's weekly budget still unspent, divided by
the number of five-hour windows left before the deadline. Spend heavily now and
the numerator falls faster than the denominator, so the derivation converges on
exhausting the budget at the deadline from any starting position and needs no
memory of how the week has gone, because the two figures already carry it.

Two tunables bias the curve and neither is a gain on the error:

``drain_lead_hours``
    How long before the weekly reset the budget should be gone, so the week
    ends drained rather than exhausted mid-window with work still queued. The
    deadline is the weekly period less this lead.

``pace_multiple``
    The deliberate lean above linear, applied on top of the derived share. A
    group exactly on pace therefore returns this multiple times the nominal
    share ``5 / D``, not the bare nominal share.

Both are read from the ``budget`` block of resolved flight config, so the bias
and the deadline are data an operator sets rather than constants in the code.

The provider's own five-hour ceiling is a separate number and is applied last:
the derived share paces the week, the ceiling stops a burst. The effective
limit is the lesser of the two, and a ceiling that could not be read is not a
ceiling — absence of a signal never holds a dispatch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

WINDOW_HOURS = 5.0
WEEK_HOURS = 168.0
DEFAULT_DRAIN_LEAD_HOURS = 12.0
DEFAULT_PACE_MULTIPLE = 1.1

# The last window before the deadline is asked for whatever remains rather than
# for a share of it: the deadline is the point the budget should be gone, and a
# denominator of zero windows has no meaningful share to return.
MIN_REMAINING_WINDOWS = 1.0

__all__ = [
    "WEEK_HOURS",
    "WINDOW_HOURS",
    "Allowance",
    "GroupReading",
    "PacePolicy",
    "allowance_for_group",
    "allowances_for_groups",
    "drain_deadline",
    "nominal_share",
    "on_pace_utilisation",
    "policy",
    "remaining_windows",
]


@dataclass(frozen=True, slots=True)
class PacePolicy:
    """The two tunables that bias the derived allowance, as configured."""

    drain_lead_hours: float
    pace_multiple: float


@dataclass(frozen=True, slots=True)
class GroupReading:
    """One budget group's freshest owned reading, in the week's own clocks.

    ``utilisation`` is the fraction of the group's weekly budget already spent,
    ``1.0`` meaning exhausted. ``elapsed_hours`` is measured from the same
    origin as the weekly clock. ``provider_ceiling`` is the group's hard
    five-hour ceiling expressed in the same units as the returned allowance —
    the fraction of the weekly budget the provider will admit in one window —
    and ``None`` where no ceiling could be read, which is not a ceiling of zero.
    """

    group: str
    utilisation: float
    elapsed_hours: float
    week_hours: float = WEEK_HOURS
    provider_ceiling: float | None = None


@dataclass(frozen=True, slots=True)
class Allowance:
    """A group's derived five-hour allowance and the figures behind it.

    Every field is carried so a dispatch row can be read without re-deriving
    anything: the multiple is recorded because it is tuned over a longer
    horizon than one week, and a curve that cannot say which setting produced
    it cannot tune that setting.
    """

    group: str
    utilisation: float
    elapsed_hours: float
    drain_hours: float
    remaining_budget: float
    remaining_windows: float
    pace_multiple: float
    derived: float
    provider_ceiling: float | None
    effective_limit: float
    limited_by: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "utilisation": self.utilisation,
            "elapsed_hours": self.elapsed_hours,
            "drain_hours": self.drain_hours,
            "remaining_budget": self.remaining_budget,
            "remaining_windows": self.remaining_windows,
            "pace_multiple": self.pace_multiple,
            "derived": self.derived,
            "provider_ceiling": self.provider_ceiling,
            "effective_limit": self.effective_limit,
            "limited_by": self.limited_by,
        }


def policy(config: Mapping[str, Any] | None) -> PacePolicy:
    """Read the deadline lead and the pace multiple out of resolved flight config."""
    block = (config or {}).get("budget") or {}
    lead = block.get("drain_lead_hours")
    multiple = block.get("pace_multiple")
    return PacePolicy(
        drain_lead_hours=(DEFAULT_DRAIN_LEAD_HOURS if lead is None else float(lead)),
        pace_multiple=(DEFAULT_PACE_MULTIPLE if multiple is None else float(multiple)),
    )


def drain_deadline(reading: GroupReading, pace: PacePolicy) -> float:
    """Return ``D`` — the weekly period less the configured lead time."""
    return float(reading.week_hours) - float(pace.drain_lead_hours)


def remaining_windows(drain: float, elapsed_hours: float) -> float:
    """Return how many five-hour windows are left before the deadline."""
    return max(
        MIN_REMAINING_WINDOWS, (float(drain) - float(elapsed_hours)) / WINDOW_HOURS
    )


def nominal_share(drain: float) -> float:
    """Return the fixed point of the derivation, ``5 / D`` of the weekly budget.

    A group that has spent exactly its nominal share per elapsed window sits on
    this value before the pace multiple is applied, which is what makes the
    multiple a bias on a self-consistent shape rather than a knob tuning an
    arbitrary one.
    """
    return WINDOW_HOURS / float(drain)


def on_pace_utilisation(elapsed_hours: float, drain: float) -> float:
    """Return the weekly utilisation a group exactly on pace would show."""
    return float(elapsed_hours) / float(drain)


def allowance_for_group(
    reading: GroupReading,
    *,
    config: Mapping[str, Any] | None = None,
    pace: PacePolicy | None = None,
) -> Allowance:
    """Return one group's allowance for the next five-hour window."""
    resolved = pace if pace is not None else policy(config)
    drain = drain_deadline(reading, resolved)
    remaining_budget = max(0.0, 1.0 - float(reading.utilisation))
    windows = remaining_windows(drain, reading.elapsed_hours)
    multiple = float(resolved.pace_multiple)

    derived = remaining_budget * multiple / windows

    ceiling = reading.provider_ceiling
    if ceiling is not None and float(ceiling) < derived:
        effective_limit, limited_by = max(0.0, float(ceiling)), "ceiling"
    else:
        effective_limit, limited_by = derived, "allowance"

    return Allowance(
        group=reading.group,
        utilisation=float(reading.utilisation),
        elapsed_hours=float(reading.elapsed_hours),
        drain_hours=drain,
        remaining_budget=remaining_budget,
        remaining_windows=windows,
        pace_multiple=multiple,
        derived=derived,
        provider_ceiling=None if ceiling is None else float(ceiling),
        effective_limit=effective_limit,
        limited_by=limited_by,
    )


def allowances_for_groups(
    readings: Iterable[GroupReading],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Allowance]:
    """Return one allowance per declared group, each from its own reading."""
    resolved = policy(config)
    return {
        reading.group: allowance_for_group(reading, pace=resolved)
        for reading in readings
    }
