"""The claim grace follows the observed launch-to-claim time, with a floor.

A dispatch that loses a registration race waits, bounded, for the winning claim
to launch or withdraw. That bound used to be a fixed fifteen seconds whatever
the fleet's launches actually cost. It is now derived from the observed
launch-to-claim distribution — the interval from a claim's registration to the
instant its worker record carries ``launched_at`` — scaled by a margin and
floored, so a slow fleet waits longer and a fast one does not hold a losing
dispatch's turn.

These tests pin the derivation, not a particular number: they assert that the
grace scale with the observation it is handed, that it never falls below the
floor, and that the module value is the derivation applied to the recorded
distribution rather than a fixed constant.
"""

from __future__ import annotations

from reckon.crew.dispatch_claims import (
    _CLAIM_LAUNCH_OBSERVED_SECONDS,
    CLAIM_GRACE_FLOOR_SECONDS,
    CLAIM_GRACE_MARGIN,
    RACING_WINNER_WAIT_SECONDS,
    claim_grace_seconds,
)


def test_the_grace_follows_a_slower_observation() -> None:
    """A larger observed launch time yields a larger grace: it is not fixed."""
    quick = claim_grace_seconds([8.0])
    slow = claim_grace_seconds([80.0])
    assert slow > quick


def test_the_grace_scales_with_the_margin() -> None:
    """The grace is the observed tail times the margin, not the raw figure."""
    assert claim_grace_seconds([20.0]) == CLAIM_GRACE_MARGIN * 20.0


def test_the_grace_never_falls_below_the_floor() -> None:
    """A tiny observation is lifted to the floor rather than shrinking the wait."""
    assert claim_grace_seconds([0.5]) == CLAIM_GRACE_FLOOR_SECONDS
    assert claim_grace_seconds([0.0]) == CLAIM_GRACE_FLOOR_SECONDS


def test_no_observation_falls_back_to_the_floor() -> None:
    """A quiet fleet — nothing to learn from — waits the floor, not nothing."""
    assert claim_grace_seconds([]) == CLAIM_GRACE_FLOOR_SECONDS


def test_the_grace_reads_the_tail_of_the_distribution() -> None:
    """A single slow outlier high in the tail does not stretch the whole bound."""
    # With percentile 0.9 over ten points, the rank sits at the ninth value, so
    # a lone maximum beyond the tail is not what the grace is taken from.
    samples = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 10.0, 1000.0]
    assert claim_grace_seconds(samples) == CLAIM_GRACE_MARGIN * 10.0


def test_the_module_grace_is_the_derivation_not_a_constant() -> None:
    """The shipped grace is the recorded distribution put through the function.

    A fixed fifteen-second grace is what this replaces: if the module value is
    a constant again, this fails rather than passing silently.
    """
    assert (
        claim_grace_seconds(_CLAIM_LAUNCH_OBSERVED_SECONDS)
        == RACING_WINNER_WAIT_SECONDS
    )
    assert RACING_WINNER_WAIT_SECONDS != 15.0
    assert RACING_WINNER_WAIT_SECONDS >= CLAIM_GRACE_FLOOR_SECONDS
