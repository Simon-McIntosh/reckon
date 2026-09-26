"""The allowance derivation: four named states, two tunables, and the ceiling.

The four states are named here before they are asserted, because the derivation
has a shape rather than a formula to spot-check: a group ahead of pace
throttles, exactly on pace returns the pace multiple times the nominal share,
behind it opens up, and exhausted it returns nothing.

The two tunables are declared flight keys, so every assertion about a *changed*
key goes through the project's own resolver and a real configuration layer
rather than a plain mapping: a mapping bypasses the layer validation that
refuses a key the schema does not declare, which is exactly how an operator
setting the key would be refused here while the suite stayed green. The
derivation's own arithmetic is still asserted against a mapping, because the
shape of the curve does not depend on which layer supplied the two figures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import flight
from reckon.crew.pace import (
    WINDOW_HOURS,
    Allowance,
    GroupReading,
    allowance_for_group,
    allowances_for_groups,
    drain_deadline,
    nominal_share,
    on_pace_utilisation,
    policy,
    remaining_windows,
)

WEEK_HOURS = 168.0
LEAD_HOURS = 12.0
DRAIN = WEEK_HOURS - LEAD_HOURS
MULTIPLE = 1.1

CONFIG = {
    "budget": {
        "drain_lead_hours": LEAD_HOURS,
        "pace_multiple": MULTIPLE,
    }
}

# The four states, named ahead of the assertions that use them.
AHEAD = "ahead"
ON_PACE = "on pace"
BEHIND = "behind"
EXHAUSTED = "exhausted"

STATES = {
    AHEAD: GroupReading("sol", utilisation=0.30, elapsed_hours=24.0),
    ON_PACE: GroupReading(
        "sol",
        utilisation=on_pace_utilisation(48.0, DRAIN),
        elapsed_hours=48.0,
    ),
    BEHIND: GroupReading("sol", utilisation=0.50, elapsed_hours=120.0),
    EXHAUSTED: GroupReading(
        "sol",
        utilisation=1.0,
        elapsed_hours=120.0,
    ),
}


def allowance_for(reading: GroupReading, config: dict | None = None) -> Allowance:
    return allowance_for_group(reading, config=CONFIG if config is None else config)


def layer_config(tmp_path: Path, name: str, **budget: float) -> dict:
    """Resolve a layer carrying the budget block, with the host layer absent.

    Written as a project layer file rather than passed as an override so the
    keys travel the same validation path an operator's own configuration does.
    """
    layer = tmp_path / name
    layer.write_text(
        "budget:\n"
        + "".join(f"  {key}: {value}\n" for key, value in budget.items())
    )
    return flight.resolve(
        project_path=layer, host_path=tmp_path / "absent-host-flight.yaml"
    ).config


def test_the_deadline_is_the_week_less_the_configured_lead():
    pace = policy(CONFIG)
    assert allowance_for(STATES[ON_PACE]).drain_hours == pytest.approx(DRAIN)
    assert drain_deadline(STATES[ON_PACE], pace) == pytest.approx(DRAIN)
    assert remaining_windows(DRAIN, 48.0) == pytest.approx(
        (DRAIN - 48.0) / WINDOW_HOURS
    )


def test_the_nominal_share_is_the_fixed_point_of_the_derivation():
    # A group that has spent exactly its nominal share per elapsed window sits
    # on the derived share with the multiple held out of the way.
    pace_multiple = 1.0
    drain = WEEK_HOURS - LEAD_HOURS
    elapsed = 48.0
    reading = GroupReading(
        "sol",
        utilisation=on_pace_utilisation(elapsed, drain),
        elapsed_hours=elapsed,
    )
    result = allowance_for(
        reading,
        {
            "budget": {
                "drain_lead_hours": LEAD_HOURS,
                "pace_multiple": pace_multiple,
            }
        },
    )
    assert result.derived == pytest.approx(nominal_share(drain))
    assert nominal_share(drain) == pytest.approx(WINDOW_HOURS / drain)


def test_a_group_exactly_on_pace_returns_the_multiple_times_the_nominal_share():
    result = allowance_for(STATES[ON_PACE])
    # Computed here rather than hardcoded: the equality is the consistency
    # proof, and a literal would stop testing the formula the moment it moved.
    expected = policy(CONFIG).pace_multiple * nominal_share(DRAIN)
    assert result.derived == pytest.approx(expected)
    assert result.remaining_budget == pytest.approx(1.0 - STATES[ON_PACE].utilisation)


def test_a_group_ahead_of_pace_is_throttled_below_the_multiple_times_nominal():
    result = allowance_for(STATES[AHEAD])
    assert result.derived < policy(CONFIG).pace_multiple * nominal_share(DRAIN)
    assert result.elapsed_hours == pytest.approx(24.0)


def test_a_group_behind_pace_opens_up_above_the_multiple_times_nominal():
    result = allowance_for(STATES[BEHIND])
    assert result.derived > policy(CONFIG).pace_multiple * nominal_share(DRAIN)
    assert result.elapsed_hours == pytest.approx(120.0)


def test_an_exhausted_group_is_asked_for_nothing():
    result = allowance_for(STATES[EXHAUSTED])
    assert result.remaining_budget == 0.0
    assert result.derived == 0.0
    assert result.effective_limit == 0.0


def test_the_four_states_are_ordered_by_how_much_room_they_leave():
    ahead = allowance_for(STATES[AHEAD]).derived
    on_pace = allowance_for(STATES[ON_PACE]).derived
    behind = allowance_for(STATES[BEHIND]).derived
    assert ahead < on_pace < behind


def test_the_effective_limit_is_the_lesser_of_the_allowance_and_the_ceiling():
    # The allowance wins: the derived share is the tighter of the two.
    roomy = GroupReading(
        "sol", utilisation=0.30, elapsed_hours=24.0, provider_ceiling=0.90
    )
    result = allowance_for(roomy)
    assert result.limited_by == "allowance"
    assert result.effective_limit == pytest.approx(result.derived)
    assert result.effective_limit < roomy.provider_ceiling

    # The ceiling wins: a burst the provider would refuse is cut to its ceiling.
    narrow = GroupReading(
        "sol", utilisation=0.30, elapsed_hours=24.0, provider_ceiling=0.001
    )
    capped = allowance_for(narrow)
    assert capped.limited_by == "ceiling"
    assert capped.effective_limit == pytest.approx(narrow.provider_ceiling)
    assert capped.derived > capped.effective_limit


def test_an_unread_ceiling_is_not_a_ceiling():
    unread = GroupReading("sol", utilisation=0.30, elapsed_hours=24.0)
    result = allowance_for(unread)
    assert result.provider_ceiling is None
    assert result.limited_by == "allowance"
    assert result.effective_limit == pytest.approx(result.derived)


def test_the_pace_multiple_is_a_configured_key_that_moves_the_allowance(tmp_path):
    base = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "base.yaml", drain_lead_hours=LEAD_HOURS, pace_multiple=MULTIPLE
        ),
    )
    doubled = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "doubled.yaml", drain_lead_hours=LEAD_HOURS, pace_multiple=2.2
        ),
    )
    assert base.derived != doubled.derived
    assert doubled.derived == pytest.approx(2.0 * base.derived)
    assert doubled.pace_multiple != base.pace_multiple


def test_the_drain_lead_is_a_configured_key_that_moves_the_allowance(tmp_path):
    base = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "base.yaml", drain_lead_hours=LEAD_HOURS, pace_multiple=MULTIPLE
        ),
    )
    early = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "early.yaml", drain_lead_hours=24.0, pace_multiple=MULTIPLE
        ),
    )
    assert early.derived != base.derived
    # A lead longer by 12 h closes the deadline by 12 h, leaving one less
    # window between now and the drain, so the share asked of each rises.
    assert early.derived > base.derived
    assert early.drain_hours == pytest.approx(base.drain_hours - 12.0)


def test_neither_tunable_stands_in_for_the_other(tmp_path):
    base = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "base.yaml", drain_lead_hours=LEAD_HOURS, pace_multiple=MULTIPLE
        ),
    )
    lead_moved = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "lead.yaml", drain_lead_hours=30.0, pace_multiple=MULTIPLE
        ),
    )
    multiple_moved = allowance_for(
        STATES[ON_PACE],
        layer_config(
            tmp_path, "multiple.yaml", drain_lead_hours=LEAD_HOURS, pace_multiple=3.3
        ),
    )
    # Each key moves its own recorded figure and leaves the other alone, so a
    # change to one cannot be read as a change to the other.
    assert lead_moved.pace_multiple == base.pace_multiple
    assert multiple_moved.drain_hours == pytest.approx(base.drain_hours)
    assert multiple_moved.pace_multiple != base.pace_multiple
    assert lead_moved.drain_hours != base.drain_hours


@pytest.mark.parametrize("state", [AHEAD, ON_PACE, BEHIND, EXHAUSTED])
def test_every_allowance_carries_the_multiple_that_produced_it(state, tmp_path):
    result = allowance_for(STATES[state])
    assert result.pace_multiple == pytest.approx(MULTIPLE)
    assert result.as_dict()["pace_multiple"] == result.pace_multiple

    retuned = allowance_for(
        STATES[state],
        layer_config(
            tmp_path, "retuned.yaml", drain_lead_hours=LEAD_HOURS, pace_multiple=7.0
        ),
    )
    assert retuned.pace_multiple == pytest.approx(7.0)
    assert retuned.as_dict()["pace_multiple"] == pytest.approx(7.0)


def test_a_layer_carrying_the_pair_resolves_and_paces_the_allowance(tmp_path):
    """Both tunables are declarable: an operator's layer sets them and they apply."""
    layer = tmp_path / "operator-flight.yaml"
    layer.write_text("budget:\n  drain_lead_hours: 24.0\n  pace_multiple: 2.2\n")
    resolved = flight.resolve(
        project_path=layer, host_path=layer.parent / "absent-host-flight.yaml"
    )

    configured = policy(resolved.config)
    assert configured.pace_multiple == pytest.approx(2.2)
    assert configured.drain_lead_hours == pytest.approx(24.0)
    assert resolved.origin("budget.pace_multiple") == "project"
    assert resolved.origin("budget.drain_lead_hours") == "project"

    result = allowance_for(STATES[ON_PACE], resolved.config)
    assert result.pace_multiple == pytest.approx(2.2)
    assert result.drain_hours == pytest.approx(WEEK_HOURS - 24.0)


def test_the_shipped_defaults_declare_the_documented_pair(tmp_path):
    """A fresh install paces from the shipped layer, not from a code constant."""
    resolved = flight.resolve(host_path=tmp_path / "absent-host-flight.yaml")

    shipped = policy(resolved.config)
    assert shipped.drain_lead_hours == pytest.approx(12.0)
    assert shipped.pace_multiple == pytest.approx(1.1)
    assert resolved.origin("budget.drain_lead_hours") == "shipped"
    assert resolved.origin("budget.pace_multiple") == "shipped"


def test_a_config_carrying_neither_key_falls_back_to_the_documented_pair():
    defaults = policy({})
    assert defaults.drain_lead_hours == pytest.approx(12.0)
    assert defaults.pace_multiple == pytest.approx(1.1)
    assert policy(CONFIG).pace_multiple == pytest.approx(MULTIPLE)


def test_a_reading_at_the_deadline_does_not_divide_by_zero():
    at_deadline = GroupReading("sol", utilisation=0.4, elapsed_hours=DRAIN)
    result = allowance_for(at_deadline)
    assert result.remaining_windows == pytest.approx(1.0)
    assert result.derived == pytest.approx(MULTIPLE * 0.6)


def test_each_group_is_computed_from_its_own_reading():
    readings = [
        STATES[AHEAD],
        GroupReading("sol-spark", utilisation=0.75, elapsed_hours=48.0),
    ]
    computed = allowances_for_groups(readings, config=CONFIG)
    assert sorted(computed) == ["sol", "sol-spark"]
    assert computed["sol-spark"].utilisation == pytest.approx(0.75)
    assert computed["sol-spark"].derived == pytest.approx(
        MULTIPLE * (1.0 - 0.75) / ((DRAIN - 48.0) / WINDOW_HOURS)
    )
