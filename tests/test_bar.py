"""The rising bar: four outcomes, a prescribed node never held, and no routing.

The bar admits a node to the metered lane on its open-endedness score, and the
bar rises as the provider's window fills. The cases below pin the properties
that make it a bar rather than a switch, and the two that a single-outcome
vocabulary inverted: a maximally prescribed node is local at every fill -- a
completely full window included -- and a maximally open-ended node at that same
full window is held rather than sent.
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from itertools import pairwise
from pathlib import Path

import pytest

from reckon.crew import bar
from reckon.crew.bar import (
    HOLD,
    NEAR_BAR_BAND,
    PRESCRIBED_MAX,
    RECOMMENDATIONS,
    SEND_LOCAL,
    SEND_METERED,
    SPLIT,
    Recommendation,
    recommend,
)

# A fill whose bar lands on a clean value: the smoothstep is 0.5 at the middle
# of the domain, which makes each boundary arithmetic rather than a probe.
MID_FILL = 0.5
MID_BAR = 0.5

# Scores at the two ends of the axis, named here so every case below states the
# same pair rather than re-deriving "the most prescribed" and "the most
# open-ended" node each time.
MAXIMALLY_PRESCRIBED = 0.0
MAXIMALLY_OPEN_ENDED = 1.0


def bar_at(fill: float) -> float:
    """Return the bar at ``fill`` by reading it off a recommendation.

    The bar is not a second public surface: it is a field of the recommendation
    the one function returns, so it is sampled the way a caller sees it.
    """
    return recommend(fill, MAXIMALLY_PRESCRIBED).bar


def test_the_module_exposes_one_pure_decision_function() -> None:
    """One function of window state and score, so there is no second surface."""
    public = [
        name
        for name, obj in inspect.getmembers(bar, inspect.isfunction)
        if obj.__module__ == bar.__name__ and not name.startswith("_")
    ]
    assert public == ["recommend"]


def test_the_score_is_a_parameter_and_never_read_from_a_node() -> None:
    """A literal score is honoured, and the module does not reach for one.

    Judging a synthesised node must need no repository, so the score is a
    parameter rather than something the module computes by importing the
    prescription judgement.
    """
    assert recommend(MID_FILL, 0.9).score == 0.9
    assert recommend(MID_FILL, 0.1).score == 0.1

    source_lines = Path(bar.__file__).read_text().splitlines()
    imports = [line for line in source_lines if line.startswith(("import ", "from "))]
    assert not any("prescription" in line for line in imports)


def test_the_bar_rises_monotonically_across_the_whole_window_domain() -> None:
    """Sampled at many points, not at transitions a continuous bar does not have."""
    samples = [i / 200.0 for i in range(201)]
    thresholds = [bar_at(fill) for fill in samples]
    assert all(later > earlier for earlier, later in pairwise(thresholds)), (
        "the bar must rise at every sampled point, not only at its ends"
    )


def test_the_bar_is_at_its_extremes_at_both_ends_of_the_domain() -> None:
    """An empty window admits anything; a full one admits only the most open work."""
    assert bar_at(0.0) == pytest.approx(0.0)
    assert bar_at(1.0) == pytest.approx(1.0)


def test_send_metered_is_returned_at_its_own_boundary() -> None:
    """Clearing the bar with room left in the window is the metered send."""
    on_the_bar = recommend(MID_FILL, MID_BAR)
    assert on_the_bar.verdict == SEND_METERED
    assert on_the_bar.margin == pytest.approx(0.0)


def test_send_local_is_returned_at_its_own_boundary() -> None:
    """The prescribed boundary is the free lane, asserted at the threshold itself."""
    at_the_threshold = recommend(1.0, PRESCRIBED_MAX)
    just_past_it = recommend(1.0, PRESCRIBED_MAX + 1e-9)

    assert at_the_threshold.verdict == SEND_LOCAL
    assert just_past_it.verdict != SEND_LOCAL
    assert recommend(1.0, MAXIMALLY_PRESCRIBED).verdict == SEND_LOCAL


def test_split_is_returned_at_its_own_boundary() -> None:
    """The near-bar band is a distinct outcome, not a second kind of hold."""
    edge = bar_at(MID_FILL) - NEAR_BAR_BAND
    inside_band = recommend(MID_FILL, edge + 1e-9)
    outside_band = recommend(MID_FILL, edge - 1e-9)

    assert inside_band.verdict == SPLIT
    assert outside_band.verdict == HOLD


def test_hold_is_returned_at_its_own_boundary() -> None:
    """Far enough below the bar, or at a window with no room, the node waits."""
    below_band = bar_at(MID_FILL) - NEAR_BAR_BAND - 1e-9
    assert recommend(MID_FILL, below_band).verdict == HOLD


def test_all_four_outcomes_are_reachable_and_distinct() -> None:
    """A bar that can only send or hold has become the switch this rejects."""
    edge = bar_at(MID_FILL) - NEAR_BAR_BAND
    seen = {
        recommend(MID_FILL, MID_BAR).verdict,
        recommend(1.0, MAXIMALLY_PRESCRIBED).verdict,
        recommend(MID_FILL, edge + 1e-9).verdict,
        recommend(MID_FILL, edge - 1e-9).verdict,
    }
    assert seen == set(RECOMMENDATIONS)
    assert len(RECOMMENDATIONS) == 4


def test_a_maximally_prescribed_node_is_never_held_at_any_fill() -> None:
    """The assertion the inverted policy failed, sampled across the whole domain.

    Every fill from an empty window to a completely full one, inclusive, must
    return the free lane for a node whose shape is entirely prescribed. A full
    window is called out separately because it is the point at which the bar
    sits at its ceiling and the preserved defect returned hold there.
    """
    verdicts = [recommend(i / 200.0, MAXIMALLY_PRESCRIBED).verdict for i in range(201)]
    assert set(verdicts) == {SEND_LOCAL}

    full_window = recommend(1.0, MAXIMALLY_PRESCRIBED)
    assert full_window.verdict == SEND_LOCAL
    assert full_window.verdict not in (HOLD, SPLIT)


def test_a_maximally_open_ended_node_at_a_full_window_is_held_not_sent() -> None:
    """The other half of the same inversion, asserted, with the bar at its top.

    The bar sits at its ceiling here, so the most open-ended node is the one a
    naive "clears the bar" rule would send; no room is left in the window, and
    the free lane is not for it.
    """
    assert bar_at(1.0) == pytest.approx(1.0)

    decision = recommend(1.0, MAXIMALLY_OPEN_ENDED)
    assert decision.verdict == HOLD
    assert decision.verdict != SEND_METERED
    assert decision.verdict != SEND_LOCAL


def test_hold_is_decided_by_the_score_with_the_fill_held_constant() -> None:
    """Vary only the score, and hold appears and disappears.

    The fill is fixed for every sample, so a verdict that tracked the window
    rather than the node could not produce both outcomes here.
    """
    fixed_fill = 0.85
    verdicts = {
        score / 100.0: recommend(fixed_fill, score / 100.0).verdict
        for score in range(101)
    }

    assert verdicts[MAXIMALLY_PRESCRIBED] == SEND_LOCAL
    assert HOLD in verdicts.values()
    assert verdicts[MAXIMALLY_OPEN_ENDED] == SEND_METERED
    assert len(set(verdicts.values())) > 1

    held = [score for score, verdict in verdicts.items() if verdict == HOLD]
    assert held, "the score alone must be able to produce a hold"
    assert all(score > PRESCRIBED_MAX for score in held)


def test_every_node_offered_the_free_lane_is_prescribed() -> None:
    """Routing unprescribed work to the free lane is the failure evidence records."""
    for fill_step in range(21):
        for score_step in range(21):
            decision = recommend(fill_step / 20.0, score_step / 20.0)
            if decision.verdict == SEND_LOCAL:
                assert decision.score <= PRESCRIBED_MAX, (
                    f"score {decision.score} at fill {decision.window_fill} was "
                    "offered the free lane without being prescribed"
                )


def test_a_fill_outside_the_window_domain_is_clamped_to_it() -> None:
    """The clamp branch, which no case exercised before, driven in both directions."""
    below = recommend(-0.4, MID_BAR)
    assert below.window_fill == pytest.approx(0.0)
    assert below.bar == pytest.approx(0.0)
    assert below.verdict == recommend(0.0, MID_BAR).verdict

    above = recommend(1.6, MID_BAR)
    assert above.window_fill == pytest.approx(1.0)
    assert above.bar == pytest.approx(1.0)
    assert above.verdict == recommend(1.0, MID_BAR).verdict


def test_the_result_carries_no_backend_name() -> None:
    """Checked against the type's own field names, not a list of lane names.

    A blocklist of the lanes spelled today would pass a lane spelled tomorrow,
    so the field set is closed instead: any field added to carry a routing --
    whatever it is called -- fails this, and the two string fields can only
    hold a verdict.
    """
    names = {field.name for field in fields(Recommendation)}
    assert names == {
        "verdict",
        "bar",
        "score",
        "window_fill",
        "margin",
        "override",
    }

    decision = recommend(MID_FILL, MID_BAR, override=SPLIT)
    assert decision.verdict in RECOMMENDATIONS
    assert decision.override in RECOMMENDATIONS
    assert decision.effective in RECOMMENDATIONS


def test_an_override_toward_the_free_lane_is_recorded_not_refused() -> None:
    """Deciding against a hold is recorded, and the bar's own verdict survives."""
    decision = recommend(1.0, MAXIMALLY_OPEN_ENDED, override=SEND_LOCAL)

    assert decision.verdict == HOLD
    assert decision.override == SEND_LOCAL
    assert decision.effective == SEND_LOCAL
    assert decision.overridden is True


def test_an_override_toward_hold_is_recorded_not_refused() -> None:
    """Deciding against a metered send is recorded in the other direction too."""
    decision = recommend(MID_FILL, MID_BAR, override=HOLD)

    assert decision.verdict == SEND_METERED
    assert decision.override == HOLD
    assert decision.effective == HOLD
    assert decision.overridden is True


def test_a_fixed_node_moves_from_metered_to_hold_as_the_window_fills() -> None:
    """The only assertion that proves the bar reads window state at all.

    One score, held fixed while the window fills, must travel from an admitted
    metered send at an empty window through the near-bar band to a hold at a
    full one.
    """
    score = MID_BAR
    early = recommend(0.0, score)
    middle = recommend(0.55, score)
    late = recommend(1.0, score)

    assert early.verdict == SEND_METERED
    assert middle.verdict == SPLIT
    assert late.verdict == HOLD
    assert (early.bar, middle.bar, late.bar) == pytest.approx((0.0, 0.57475, 1.0))


def test_an_unknown_override_is_a_caller_error_not_a_silent_default() -> None:
    """A value outside the four outcomes is not a decision about the bar."""
    with pytest.raises(ValueError, match="not one of"):
        recommend(MID_FILL, MID_BAR, override="route_local")
