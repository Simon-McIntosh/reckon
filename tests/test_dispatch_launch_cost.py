"""The claim grace follows the observed launch-to-claim time, with a floor.

A dispatch that loses a registration race waits, bounded, for the winning claim
to launch or withdraw. The bound follows the observed launch-to-claim
distribution — the interval from a claim's registration to the instant its
worker record carries ``launched_at`` — scaled by a margin and floored, so a
slow fleet waits longer and a fast one does not hold a losing dispatch's turn.

These tests pin the derivation, not a particular number: they assert that the
grace scales with its observations, never falls below the floor, and reads
recorded launch intervals rather than a fixed distribution.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.dispatch_claims import (
    CLAIM_GRACE_FLOOR_SECONDS,
    CLAIM_GRACE_MARGIN,
    claim_grace_seconds,
    recent_claim_grace_seconds,
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


def test_the_module_grace_is_the_derivation_not_a_constant(tmp_path: Path) -> None:
    """Recorded launch intervals, rather than a module constant, set the grace."""
    for index in range(20):
        directory = tmp_path / f"r-{index:02d}-sample"
        directory.mkdir()
        (directory / "worker.json").write_text(
            '{"claim_registered_at":"2026-10-08T12:00:00Z",'
            '"launched_at":"2026-10-08T12:00:20Z"}',
            encoding="utf-8",
        )

    assert recent_claim_grace_seconds(tmp_path) == claim_grace_seconds([20.0] * 20)
