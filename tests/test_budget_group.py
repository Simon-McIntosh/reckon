"""Budget groups resolve from declaration, and a position is the group's own.

The three properties under test are that grouping is read from resolved flight
config rather than from lane names, that a group's position is its freshest
member reading carrying that member's age, and that no function exists by which
one lane's share of a shared wallet can be computed as a position.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import budget_group as bg

# Four lanes on one subscription plus a fifth on a separate one, mirroring the
# declared configuration this position replaces per-lane readings for.
SOL_FAMILY_CONFIG = {
    "backends": {
        "codex": {"budget_group": "codex-main"},
        "codex-astra": {"budget_group": "codex-main"},
        "codex-terra": {"budget_group_typo": "codex-main"},
        "codex-luna": {"budget_group": "codex-main"},
        "codex-spark": {"budget_group": "spark"},
    }
}

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _stamp(age: timedelta) -> str:
    return (NOW - age).isoformat()


def test_grouping_reads_the_declared_slot_from_resolved_config():
    """Two declared groups, not five lanes. A typo in the slot name never groups."""
    groups = bg.declared_groups(SOL_FAMILY_CONFIG)

    assert list(groups) == ["codex-main", "spark"]
    assert len(groups) == 2
    assert groups["codex-main"] == ["codex", "codex-astra", "codex-luna"]
    assert groups["spark"] == ["codex-spark"]


def test_a_backend_declaring_no_group_is_left_ungrouped():
    """An undeclared backend joins no group, however it is named."""
    config = {
        "backends": {
            "sol": {"budget_group": "codex-main"},
            "orphan": {"command": "codex"},
            "blank": {"budget_group": "   "},
        }
    }

    groups = bg.declared_groups(config)

    assert groups == {"codex-main": ["sol"]}
    assert bg.ungrouped(config) == ("orphan", "blank")


def test_every_member_resolves_to_exactly_one_group():
    """Every declared backend appears in one group, and no backend in two."""
    groups = bg.declared_groups(SOL_FAMILY_CONFIG)
    placement = {
        member: group for group, members in groups.items() for member in members
    }

    assert set(placement) == {
        "codex",
        "codex-astra",
        "codex-luna",
        "codex-spark",
    }
    assert all(member not in bg.ungrouped(SOL_FAMILY_CONFIG) for member in placement)


def test_position_is_the_freshest_member_not_the_first_declared():
    """The freshest member is last in declaration order, and it is the one used."""
    config = {
        "backends": {
            "codex": {"budget_group": "codex-main"},
            "codex-astra": {"budget_group": "codex-main"},
            "codex-luna": {"budget_group": "codex-main"},
        }
    }
    readings = {
        "codex": {
            "observed_at": _stamp(timedelta(hours=2, minutes=24)),
            "used_percent": 13,
        },
        "codex-astra": {
            "observed_at": _stamp(timedelta(seconds=0)),
            "used_percent": 14,
        },
        "codex-luna": {"observed_at": _stamp(timedelta(minutes=6)), "used_percent": 7},
    }

    position = bg.group_position("codex-main", config, readings, now=NOW)

    assert position.member == "codex-astra"
    assert position.reading == readings["codex-astra"]
    assert position.age_seconds == pytest.approx(0.0)
    assert position.state == bg.OBSERVED
    assert position.members == ("codex", "codex-astra", "codex-luna")


def test_position_carries_the_freshest_members_own_age():
    """The age reported is the freshest member's, not the group's oldest reading."""
    config = {
        "backends": {
            "sol": {"budget_group": "one-wallet"},
            "luna": {"budget_group": "one-wallet"},
        }
    }
    readings = {
        "sol": {"observed_at": _stamp(timedelta(days=1)), "used_percent": 13},
        "luna": {"observed_at": _stamp(timedelta(minutes=6)), "used_percent": 7},
    }

    position = bg.group_position("one-wallet", config, readings, now=NOW)

    assert position.member == "luna"
    assert position.observed_at == _stamp(timedelta(minutes=6))
    assert position.member == "luna"
    assert position.age_seconds == pytest.approx(360.0)


def test_an_undated_member_never_outranks_a_dated_one():
    """A missing stamp is not a fresh reading."""
    config = {
        "backends": {
            "dated": {"budget_group": "one-wallet"},
            "undated": {"budget_group": "one-wallet"},
        }
    }
    readings = {
        "dated": {"observed_at": _stamp(timedelta(hours=1)), "used_percent": 13},
        "undated": {"used_percent": 99},
    }

    position = bg.group_position("one-wallet", config, readings, now=NOW)

    assert position.member == "dated"
    assert position.age_seconds == pytest.approx(3600.0)


def test_no_member_can_be_aged_reports_unobserved_and_never_zero():
    """Absence of an observation is an unobserved position, not a zero: an
    age of zero would read as a reading taken this instant."""
    config = {"backends": {"sol": {"budget_group": "one-wallet"}}}
    readings = {"sol": {"used_percent": 13}}

    position = bg.group_position("one-wallet", config, readings, now=NOW)

    assert position.state == bg.UNOBSERVED
    assert position.age_seconds is None
    assert position.member is None
    assert position.observed_at is None


def test_a_member_without_a_reading_does_not_participate():
    """A silent member cannot supply the group's position."""
    config = {
        "backends": {
            "silent": {"budget_group": "one-wallet"},
            "reading": {"budget_group": "one-wallet"},
        }
    }
    readings = {"reading": {"observed_at": _stamp(timedelta(minutes=30))}}

    position = bg.group_position("one-wallet", config, readings, now=NOW)

    assert position.member == "reading"
    assert position.member != "silent"


def test_epoch_and_iso_stamps_are_aged_in_the_same_frame():
    """A numeric stamp and an ISO one order together by the same clock."""
    config = {
        "backends": {
            "epoch": {"budget_group": "one-wallet"},
            "iso": {"budget_group": "one-wallet"},
        }
    }
    readings = {
        "epoch": {
            "observed_at": (datetime(2026, 9, 21, 11, 0, tzinfo=UTC)).timestamp()
        },
        "iso": {"observed_at": _stamp(timedelta(minutes=1))},
    }

    position = bg.group_position("one-wallet", config, readings, now=NOW)

    assert position.member == "iso"
    assert position.age_seconds == pytest.approx(60.0)


def test_position_requires_a_declared_group_so_no_per_lane_position_exists():
    """A lane name is not a group identifier, and this function refuses one.

    This is the guard that makes a per-lane position uncomputable: there is no
    function in the module keyed on a backend, and the one position function
    rejects a backend name that declares a group of its own.
    """
    config = {
        "backends": {
            "codex": {"budget_group": "codex-main"},
            "codex-spark": {"budget_group": "spark"},
        }
    }
    readings = {"codex": {"observed_at": _stamp(timedelta(minutes=1))}}

    with pytest.raises(ValueError, match="not a declared budget group"):
        bg.group_position("codex", config, readings, now=NOW)

    assert "codex-spark" not in bg.declared_groups(config)


def test_module_exposes_no_per_backend_position_entry_point():
    """The module's only position callable takes a group identifier."""
    position_names = [
        name
        for name, value in vars(bg).items()
        if not name.startswith("_") and callable(value) and "position" in name
    ]

    assert position_names == ["group_position"]
    parameters = list(inspect.signature(bg.group_position).parameters)
    assert parameters[0] == "group"
    assert "backend" not in parameters
    assert not hasattr(bg, "backend_position")
    assert not hasattr(bg, "lane_position")


def test_group_keyed_position_is_stable_across_its_members_order():
    """The same readings give the same position whichever order config lists."""
    forward = {
        "backends": {
            "sol": {"budget_group": "w"},
            "luna": {"budget_group": "w"},
        }
    }
    reverse = {
        "backends": {
            "luna": {"budget_group": "w"},
            "sol": {"budget_group": "w"},
        }
    }
    readings = {
        "sol": {"observed_at": _stamp(timedelta(hours=1)), "used_percent": 13},
        "luna": {"observed_at": _stamp(timedelta(minutes=2)), "used_percent": 7},
    }

    first = bg.group_position("w", forward, readings, now=NOW)
    second = bg.group_position("w", reverse, readings, now=NOW)

    assert first.member == second.member == "luna"
    assert first.age_seconds == second.age_seconds == pytest.approx(120.0)
