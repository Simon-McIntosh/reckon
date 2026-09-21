"""A hold carries the evidence that produced it, or it does not hold.

Four properties are under test: a hold cannot be constructed without its
group, its figure, that figure's age and its source; a figure past its shelf
life is re-queried and the hold rests on the figure that comes back; a
re-query that cannot answer leaves the state ``unknown`` and unknown does not
hold; and a figure observed by a probe describing a pool the held group does
not declare never justifies a hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import budget_group as bg
from reckon.crew import hold as hold_mod
from reckon.crew import staleness

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SHELF_LIFE_MINUTES = 60
# A reading two and a half hours old: the five-hour window need not have
# rolled over, but the figure is far enough outside its shelf life to name a
# window that may have, which is what disqualifies it as a position.
STALE_AGE = timedelta(hours=2.4)
# The bare flight shape the reader resolves its shelf life through, so the
# boundary under test is the configured one rather than a literal in the test.
CONFIG = {"budget": {"evidence_shelf_life_minutes": SHELF_LIFE_MINUTES}}


def _stamp(age: timedelta) -> str:
    return (NOW - age).isoformat()


def _position(group: str, member: str, reading: dict) -> bg.GroupPosition:
    """A declared group's position, resolved the way a caller resolves it."""
    config = {"backends": {member: {"budget_group": group}}}
    return bg.group_position(group, config, {member: reading}, now=NOW)


def _reading(
    *,
    used_percent: float,
    age: timedelta,
    pool: str,
    serving_state: str = "will_serve",
) -> dict:
    return {
        "used_percent": used_percent,
        "observed_at": _stamp(age),
        "source": pool,
        "serving_state": serving_state,
    }


def _probe_returning(reading: staleness.Reading):
    calls: list[int] = []

    def probe() -> staleness.Reading:
        calls.append(1)
        return reading

    return probe, calls


def _probe_refusing():
    calls: list[int] = []

    def probe() -> None:
        calls.append(1)
        raise RuntimeError("probe transport refused")

    return probe, calls


def _forbidden_probe():
    def probe() -> None:
        raise AssertionError("a figure inside its own shelf life must not re-query")

    return probe


# ── The constructor refuses a hold missing any of the four facts ────────────


@pytest.mark.parametrize("omitted", ["group", "figure", "age_seconds", "source"])
def test_a_hold_missing_one_of_its_four_facts_is_refused(omitted):
    """Each omission is refused on its own, by the constructor, not by a string."""
    facts = {
        "group": "wallet",
        "figure": 95.0,
        "age_seconds": 30.0,
        "source": "wallet",
    }
    del facts[omitted]

    with pytest.raises(hold_mod.MissingHoldEvidenceError) as raised:
        hold_mod.Hold(**facts, ceiling=90.0)

    # The refusal names the missing fact on its own attribute, so nothing here
    # depends on the wording of the message.
    assert raised.value.field == omitted


def test_a_real_zero_is_a_measurement_and_not_a_missing_fact():
    """A figure of zero and an age of zero are present; ``None`` is what is absent."""
    hold = hold_mod.Hold(
        group="wallet", figure=0.0, age_seconds=0.0, source="wallet", ceiling=90.0
    )

    assert hold.figure == 0.0
    assert hold.age_seconds == 0.0


# ── A stale figure is re-queried, and the hold rests on what comes back ─────


def test_a_stale_reading_is_requeried_and_the_holds_figure_is_the_probes():
    """The hold carries the probe's figure, never the stale one that asked."""
    stale = _reading(used_percent=80.0, age=STALE_AGE, pool="wallet")
    fresh = staleness.Reading(
        used_percent=97.0,
        observed_at=NOW - timedelta(seconds=30),
        source="wallet",
        serving_state="will_serve",
    )
    probe, calls = _probe_returning(fresh)

    hold = hold_mod.hold_from_position(
        _position("wallet", "lane", stale),
        probe=probe,
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert calls == [1], "the stale reading did not ask the lane's probe"
    assert isinstance(hold, hold_mod.Hold)
    assert hold.figure == 97.0
    assert hold.figure != stale["used_percent"]
    assert hold.age_seconds == pytest.approx(30.0, abs=1.0)
    assert hold.group == "wallet"
    assert hold.source == "wallet"


def test_a_figure_inside_its_shelf_life_is_held_without_a_requery():
    """The re-query is not unconditional: a live figure is never disturbed."""
    hold = hold_mod.hold_from_position(
        _position(
            "wallet",
            "lane",
            _reading(used_percent=95.0, age=timedelta(minutes=5), pool="wallet"),
        ),
        probe=_forbidden_probe(),
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert isinstance(hold, hold_mod.Hold)
    assert hold.figure == 95.0
    assert hold.age_seconds == pytest.approx(300.0, abs=1.0)


# ── A failed re-query is unknown, and unknown does not hold ─────────────────


def test_a_failed_requery_yields_unknown_and_does_not_hold():
    """The asymmetry: a missed hold costs quota, a false hold costs the lane."""
    stale = _reading(used_percent=95.0, age=STALE_AGE, pool="wallet")
    probe, calls = _probe_refusing()

    resolved = staleness.resolve_configured_reading(
        staleness.Reading(
            used_percent=95.0,
            observed_at=NOW - STALE_AGE,
            source="wallet",
            serving_state="will_serve",
        ),
        probe=probe,
        config=CONFIG,
        now=NOW,
    )
    # The state the hold refuses on is asserted directly, so the refusal below
    # cannot be passing for some unrelated reason.
    assert resolved.serving_state == staleness.SERVING_STATE_UNKNOWN

    probe, calls = _probe_refusing()
    hold = hold_mod.hold_from_position(
        _position("wallet", "lane", stale),
        probe=probe,
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert calls == [1]
    assert hold is None, "a failed re-query was read as a figure and held on"


def test_an_unknown_serving_state_does_not_hold():
    """A figure whose state is unknown is not a headroom and is not a position."""
    hold = hold_mod.hold_from_position(
        _position(
            "wallet",
            "lane",
            _reading(
                used_percent=95.0,
                age=timedelta(minutes=5),
                pool="wallet",
                serving_state=staleness.SERVING_STATE_UNKNOWN,
            ),
        ),
        probe=_forbidden_probe(),
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert hold is None


# ── A borrowed figure never justifies a hold ────────────────────────────────


def test_a_borrowed_figure_never_becomes_the_groups_position():
    """A figure from a pool this group does not declare is refused before the ceiling.

    The reading is inside its shelf life, so no re-query could replace it: the
    figure at hand is a probe describing ``codex-main`` and it is above the
    ceiling, yet it must not become the group's position, so the probe is never asked.
    """
    hold = hold_mod.hold_from_position(
        _position(
            "spark",
            "spark-lane",
            _reading(used_percent=95.0, age=timedelta(minutes=5), pool="codex-main"),
        ),
        probe=_forbidden_probe(),
        ceiling=50.0,
        config=CONFIG,
        now=NOW,
    )

    assert hold is None


def test_a_figure_whose_pool_is_not_stated_is_not_owned():
    """A pool that cannot be shown to be the group's is not adopted on trust."""
    reading = _reading(used_percent=95.0, age=timedelta(minutes=5), pool="wallet")
    del reading["source"]

    hold = hold_mod.hold_from_position(
        _position("wallet", "lane", reading),
        probe=_forbidden_probe(),
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert hold is None


# ── A figure that does not reach the ceiling is not a hold ──────────────────


def test_a_figure_below_the_ceiling_does_not_hold():
    """A position is not a hold: a figure under the ceiling is a clear lane."""
    hold = hold_mod.hold_from_position(
        _position(
            "wallet",
            "lane",
            _reading(used_percent=42.0, age=timedelta(minutes=5), pool="wallet"),
        ),
        probe=_forbidden_probe(),
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert hold is None


def test_an_unobserved_position_carries_no_figure_to_hold_on():
    """A group with no ageable member reading has no figure, so no hold."""
    config = {"backends": {"lane": {"budget_group": "wallet"}}}
    position = bg.group_position(
        "wallet", config, {"lane": {"used_percent": 95.0}}, now=NOW
    )
    assert position.state == bg.UNOBSERVED

    hold = hold_mod.hold_from_position(
        position,
        probe=_forbidden_probe(),
        ceiling=90.0,
        config=CONFIG,
        now=NOW,
    )

    assert hold is None
