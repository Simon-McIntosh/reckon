"""Reserve a fraction of each five-hour window for the bookend roles.

A local repair is verified by a short metered review, and those reviews are
exactly what an audit wave starves: the wave ahead of them spends the window.
So the window is not one pool. A fraction of a window is reserved for the
review and verify roles and is unavailable to implementation work at any bar.

The reserve holds from the start of the window rather than only when the window
is nearly full. That is a property of the arithmetic here and not of a branch:
a non-bookend role's ceiling is the window ceiling less the reserve outright, so
the withheld fraction is gone before any work is dispatched, at every
utilisation. A rule that withdrew the fraction only near the ceiling would be a
function of window state; this one is not, because it reads the role and the
configured fraction alone.

Only the roles that maintain the fleet are exempt. ``review`` and ``verify``
spend from the whole window because they are the expenditure the reserve exists
to protect, and the section names the two together, so neither is second class.
Everything else — every implementation, test, investigation or cleanup node — is
withheld the fraction.

An unreadable window is not an empty one. A utilisation nobody could read must
not be folded to zero, because zero is the one figure that admits everything, so
a reserve judged against a missing reading would open the whole window to
implementation work exactly when nothing knows how much of the window is gone.
Absence of a reading therefore refuses the roles the reserve withholds from and
admits the bookends, and the refusal says the window could not be read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The roles the reserve exists for. A verify role is admitted on the same
# footing as a review: the reserve protects both, so both read the full window.
BOOKEND_ROLES: frozenset[str] = frozenset({"review", "verify"})

# The flight key, read from the same `budget` block as the resume and
# coordinator reserves. Changing it moves the boundary a dispatch meets.
RESERVE_KEY = "bookend_reserve_pct"

# Declared fraction withheld from implementation work when the flight key is
# absent. One part in five gives the bookends a floor at the start of every
# window rather than only near its ceiling.
DEFAULT_RESERVE_PCT = 20.0

# The ceiling a dispatch is measured against when the block names none, matching
# the budget block's own unset ceiling.
DEFAULT_CEILING_PCT = 100.0


def reserve_pct(block: Mapping[str, Any] | None) -> float:
    """Read the reserved fraction of the window, in percentage points.

    Absence of the flight key is not a zero reserve: an unconfigured reckon
    still withholds the declared floor, because a reserve that defaults to
    nothing is a reserve that stops holding the moment a layer omits it.
    """
    raw = (block or {}).get(RESERVE_KEY)
    pct = DEFAULT_RESERVE_PCT if raw is None else float(raw)
    return min(max(pct, 0.0), 100.0)


def ceiling_pct(block: Mapping[str, Any] | None) -> float:
    """The window's own ceiling, before any reserve is withheld from it."""
    raw = (block or {}).get("utilisation_ceiling_pct")
    if raw is None:
        return DEFAULT_CEILING_PCT
    return min(max(float(raw), 0.0), 100.0)


def is_bookend(role: str | None) -> bool:
    """Whether a role spends from the reserved fraction."""
    return str(role) in BOOKEND_ROLES


def role_ceiling_pct(block: Mapping[str, Any] | None, role: str | None) -> float:
    """The utilisation a role of this kind may reach.

    A bookend may reach the window ceiling. Every other role stops one reserved
    fraction below it, at every utilisation — which is what makes the reserve
    hold from the start rather than appear as the window fills.
    """
    ceiling = ceiling_pct(block)
    if is_bookend(role):
        return ceiling
    return max(0.0, ceiling - reserve_pct(block))


def _unreadable_verdict(
    block: Mapping[str, Any] | None,
    *,
    role: str | None,
    claim_pct: float,
    unreadable_detail: str | None,
) -> dict[str, Any]:
    """Judge a dispatch whose window could not be read.

    The figures an admitted verdict reports as numbers are left unset here
    rather than defaulted to zero, because a zero written on this path would
    read downstream as an observed empty window, which is the reading this
    branch exists to refuse.
    """
    limit = role_ceiling_pct(block, role)
    reserve = reserve_pct(block)
    bookend = is_bookend(role)
    claim = max(0.0, float(claim_pct))
    detail = f" ({unreadable_detail})" if unreadable_detail else ""
    if bookend:
        admitted = True
        reason = (
            f"the window's utilisation could not be read{detail}, and a {role} "
            f"role spends from the whole window: the {reserve:g}% bookend "
            "reserve is not withheld from it, so the unreadable reading does "
            f"not bar this dispatch from the {limit:g}% ceiling"
        )
    else:
        admitted = False
        reason = (
            f"the window's utilisation could not be read{detail}, and an "
            "unreadable window is not an empty one: a "
            f"{role} dispatch is refused against the {reserve:g}% the window "
            "keeps for review and verify roles rather than admitted as if the "
            f"window were empty; an implementation dispatch reaches {limit:g}% "
            f"of the {ceiling_pct(block):g}% window"
        )
    return {
        "role": str(role),
        "bookend": bookend,
        "admitted": admitted,
        "reserve_pct": reserve,
        "ceiling_pct": ceiling_pct(block),
        "limit_pct": limit,
        "utilisation_pct": None,
        "claim_pct": claim,
        "projected_pct": None,
        "reason": reason,
    }


def admit(
    block: Mapping[str, Any] | None,
    *,
    role: str | None,
    utilisation_pct: float | None,
    claim_pct: float = 0.0,
    unreadable_detail: str | None = None,
) -> dict[str, Any]:
    """Judge one dispatch against the bookend reserve.

    The verdict names the role, the fraction withheld and the ceiling it may
    reach, so a refusal can be argued with rather than merely obeyed. A claim is
    admitted when the window's utilisation plus the claim does not exceed the
    role's ceiling; the reserve withholds the fraction beyond that ceiling, so a
    dispatch landing exactly on it is admitted.

    A utilisation of ``None`` is a window nobody could read, and it is judged as
    unreadable rather than as empty: an implementation dispatch is refused
    against the reserved fraction, and a bookend is admitted because the reserve
    is never withheld from it. The caller may pass the reading's own account of
    why it is missing in ``unreadable_detail``; it is appended to the refusal so
    the sentence carries the resolver's reason alongside this one.
    """
    if utilisation_pct is None:
        return _unreadable_verdict(
            block,
            role=role,
            claim_pct=claim_pct,
            unreadable_detail=unreadable_detail,
        )

    used = float(utilisation_pct)
    claim = max(0.0, float(claim_pct))
    projected = used + claim
    limit = role_ceiling_pct(block, role)
    reserve = reserve_pct(block)
    bookend = is_bookend(role)
    admitted = projected <= limit

    if admitted:
        if bookend:
            reason = (
                f"a {role} role spends from the whole window and reaches the "
                f"{limit:g}% ceiling; the {reserve:g}% bookend reserve is not "
                "withheld from it"
            )
        else:
            reason = (
                f"utilisation {used:g}% plus a {claim:g}% claim projects "
                f"{projected:g}%, at or below the {limit:g}% ceiling an "
                f"implementation dispatch may reach once the {reserve:g}% "
                "bookend reserve is withheld"
            )
    elif bookend:
        reason = (
            f"a {role} role may reach the {limit:g}% window ceiling, and "
            f"utilisation {used:g}% plus a {claim:g}% claim projects "
            f"{projected:g}% above it"
        )
    else:
        reason = (
            f"the window keeps {reserve:g}% for review and verify roles, so an "
            f"implementation dispatch reaches {limit:g}% rather than the "
            f"{ceiling_pct(block):g}% ceiling; utilisation {used:g}% plus a "
            f"{claim:g}% claim projects {projected:g}% and would spend the "
            "reserved fraction"
        )

    return {
        "role": str(role),
        "bookend": bookend,
        "admitted": admitted,
        "reserve_pct": reserve,
        "ceiling_pct": ceiling_pct(block),
        "limit_pct": limit,
        "utilisation_pct": used,
        "claim_pct": claim,
        "projected_pct": projected,
        "reason": reason,
    }
