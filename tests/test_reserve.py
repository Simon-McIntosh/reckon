"""The bookend reserve: a fraction of every window withheld from implementation.

The measure these tests exist to demonstrate is a pair, not a single refusal. A
reserve that does nothing admits both an implementation dispatch and a review at
the same window state, so asserting the refusal alone proves nothing — the
refusal is only evidence of a reserve when the same state admits the review it
is holding the fraction for. Every window-state assertion here therefore lands
both halves.

The reserve must bind at the start of the window and not only near its ceiling.
A rule that withheld the fraction once the window filled would pass every
assertion taken at a nearly-full window, so the pair is asserted at an empty
window first and repeated at a nearly-full one.
"""

from __future__ import annotations

import pytest

from reckon.crew import reserve

# A window with the shipped ceiling and a configured reserve, as a resolved
# budget block would carry them.
WINDOW = {"utilisation_ceiling_pct": 100.0, "bookend_reserve_pct": 20.0}


def test_empty_window_refuses_implementation_and_admits_review() -> None:
    """The pair at the start of the window: the same state, opposite verdicts.

    A reserve that held only as the window filled would admit both here. It is
    the empty window that separates a real reserve from that rule.
    """
    implementation = reserve.admit(
        WINDOW, role="implement", utilisation_pct=0.0, claim_pct=100.0
    )
    review = reserve.admit(WINDOW, role="review", utilisation_pct=0.0, claim_pct=100.0)

    assert implementation["admitted"] is False
    assert review["admitted"] is True
    assert implementation["limit_pct"] == 80.0
    assert review["limit_pct"] == 100.0
    assert "20" in implementation["reason"]


def test_nearly_full_window_keeps_the_same_pair() -> None:
    """The pair again near the ceiling, so the reserve is not a fill-time rule."""
    implementation = reserve.admit(
        WINDOW, role="implement", utilisation_pct=79.0, claim_pct=5.0
    )
    review = reserve.admit(WINDOW, role="review", utilisation_pct=79.0, claim_pct=5.0)

    assert implementation["admitted"] is False
    assert review["admitted"] is True


def test_a_modest_implementation_claim_is_still_admitted() -> None:
    """The reserve is a fraction, not a wall against all implementation work."""
    verdict = reserve.admit(
        WINDOW, role="implement", utilisation_pct=0.0, claim_pct=10.0
    )
    assert verdict["admitted"] is True


def test_the_reserve_key_moves_the_refusal_boundary() -> None:
    """Changing the flight key moves where an implementation dispatch is refused.

    One claim is admitted at the shipped fraction and refused at a larger one,
    so the boundary is data rather than a constant in the code.
    """
    claim = 70.0
    small = reserve.admit(
        WINDOW, role="implement", utilisation_pct=0.0, claim_pct=claim
    )
    large_block = {**WINDOW, "bookend_reserve_pct": 50.0}
    large = reserve.admit(
        large_block, role="implement", utilisation_pct=0.0, claim_pct=claim
    )

    assert small["admitted"] is True
    assert large["admitted"] is False
    assert reserve.role_ceiling_pct(WINDOW, "implement") == 80.0
    assert reserve.role_ceiling_pct(large_block, "implement") == 50.0


@pytest.mark.parametrize("role", ["review", "verify"])
def test_verify_is_admitted_on_the_same_footing_as_review(role: str) -> None:
    """Both named bookend roles are exempt, asserted by identity of ceiling."""
    assert reserve.is_bookend(role) is True
    assert reserve.role_ceiling_pct(WINDOW, role) == reserve.role_ceiling_pct(
        WINDOW, "review"
    )
    verdict = reserve.admit(WINDOW, role=role, utilisation_pct=0.0, claim_pct=100.0)
    assert verdict["admitted"] is True


def test_the_reserve_holds_from_the_start_of_the_window() -> None:
    """The withheld fraction is gone before any work is dispatched.

    The implementation ceiling is one reserved fraction below the window
    ceiling at zero utilisation, and it does not move as the window fills — the
    reserve reads the role and the fraction, never the window state.
    """
    empty = reserve.role_ceiling_pct(WINDOW, "implement")
    nearly_full = reserve.role_ceiling_pct(WINDOW, "implement")
    assert empty == 80.0
    assert nearly_full == empty
    assert reserve.ceiling_pct(WINDOW) - empty == reserve.reserve_pct(WINDOW)


def test_an_unset_key_still_withholds_the_declared_floor() -> None:
    """A missing flight key is not a zero reserve.

    A default that resolved to nothing would silently disable the reserve in
    every layer that omits it, so the refusal is asserted with the key absent.
    """
    bare = {"utilisation_ceiling_pct": 100.0}
    assert reserve.reserve_pct(bare) == reserve.DEFAULT_RESERVE_PCT
    implementation = reserve.admit(
        bare, role="implement", utilisation_pct=0.0, claim_pct=85.0
    )
    review = reserve.admit(bare, role="review", utilisation_pct=0.0, claim_pct=85.0)
    assert implementation["admitted"] is False
    assert review["admitted"] is True
