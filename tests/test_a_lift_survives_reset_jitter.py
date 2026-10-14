"""A reset-anchored lift is not ended by a second of provider stamp jitter.

A provider publishes a stale reset stamp a second off the one it published a
moment earlier, and the exact-equality test an earlier resolver used read that
wobble as a rollover, so a lift ended the instant it was granted. These tests
pin the behaviour the jitter tolerance must show against every case:

* a stamp that differs from the recorded one by one second leaves the lift in
  force;
* a stamp a full window later ends it, cause ``reset``;
* the two utilisation signals end it exactly as they always did;
* the tolerance itself is measured from the stored readings, so a history that
  shows a three-second wobble tolerates a two-second move the second-resolution
  floor alone would not.

Each test points the config home at a temporary directory and asserts the live
workstation's ``budget-lifts.json`` is untouched afterwards, because a test that
writes the real lift record is a monitor for the machine rather than the code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_lift as bl
from tests.real_home_traces import traces_naming

WEEK_HOURS = bl.CLOCK_HOURS[bl.SEVEN_DAY]
GROUP = "lift-guard-fixture"
NOW = datetime(2026, 10, 8, 3, 12, tzinfo=UTC)
ENV = {"USER": "lead"}


def _week_reset(*, elapsed_hours: float = 22.0) -> datetime:
    """The seven-day reset the group shows after ``elapsed_hours`` of its week."""
    return NOW + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _reading(
    *,
    week_utilisation: float = 0.21,
    week_resets_at: datetime | None = None,
) -> dict:
    """A window reading in the shape the lift resolver reads."""
    return {
        "observed_at": NOW.isoformat(),
        "five_hour": {
            "utilisation": 0.05,
            "resets_at": (NOW + timedelta(hours=4)).isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": (week_resets_at or _week_reset()).isoformat(),
        },
    }


def _config() -> dict:
    return {
        "backends": {GROUP: {"budget_group": GROUP}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
            "coordinator_reserve_pct": 3.0,
        },
    }


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the config home at a temp dir; prove the real record is untouched.

    Only the group this suite keys its grants by is searched for, so a peer's
    own lift -- keyed by a group this file never uses -- is never a hit.
    """
    home = Path.home() / ".config" / "reckon"
    before = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    assert after == before, f"a test wrote the live budget-lifts.json: {after}"


def _grant(config: dict, *, reading: dict) -> dict:
    """Grant a reset-anchored lift anchored to ``reading``'s seven-day stamp."""
    return bl.grant(
        config,
        group=GROUP,
        reason="run a build hard",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        environ=ENV,
    )


def test_a_one_second_stamp_wobble_leaves_the_lift_in_force():
    config = _config()
    recorded = _week_reset()
    lift = _grant(config, reading=_reading(week_resets_at=recorded))
    assert lift["ends"]["kind"] == "reset"
    assert lift["ends"]["resets_at"] == recorded.isoformat()

    wobbled = _reading(week_resets_at=recorded + timedelta(seconds=1))
    block = bl.effective_budget(config, group=GROUP, readings=[wobbled], now=NOW)
    assert block is not config["budget"]
    assert block["lift_id"] == lift["id"]
    assert (
        bl.end_cause(lift, readings=[wobbled], now=NOW, bound=bl.ceilings(config))
        is None
    )


def test_a_stamp_a_full_window_later_ends_the_lift():
    config = _config()
    recorded = _week_reset()
    lift = _grant(config, reading=_reading(week_resets_at=recorded))

    rolled = _reading(week_resets_at=recorded + timedelta(hours=WEEK_HOURS))
    cause = bl.end_cause(lift, readings=[rolled], now=NOW, bound=bl.ceilings(config))
    assert cause == bl.END_RESET
    assert (
        bl.effective_budget(config, group=GROUP, readings=[rolled], now=NOW)
        is config["budget"]
    )


def test_the_utilisation_below_the_grant_figure_still_ends_the_lift():
    config = _config()
    recorded = _week_reset()
    lift = _grant(config, reading=_reading(week_resets_at=recorded))

    fell = _reading(week_utilisation=0.10, week_resets_at=recorded)
    assert (
        bl.end_cause(lift, readings=[fell], now=NOW, bound=bl.ceilings(config))
        == bl.END_RESET
    )


def test_a_fall_between_two_readings_still_ends_the_lift():
    config = _config()
    recorded = _week_reset()
    lift = _grant(config, reading=_reading(week_resets_at=recorded))

    history = [
        _reading(week_utilisation=0.30, week_resets_at=recorded),
        _reading(week_utilisation=0.25, week_resets_at=recorded),
    ]
    assert (
        bl.end_cause(lift, readings=history, now=NOW, bound=bl.ceilings(config))
        == bl.END_RESET
    )


def test_the_tolerance_is_measured_from_the_stored_readings():
    """A two-second move survives once the history shows a three-second wobble.

    With no history the move is judged against the second the stamps can differ
    by, so it ends the lift; with a history whose stamps span three seconds the
    measured jitter is three seconds and the same move leaves it in force. The
    two arms are the same lift and the same reading, so the reading alone cannot
    account for the difference.
    """
    config = _config()
    recorded = _week_reset()
    lift = _grant(config, reading=_reading(week_resets_at=recorded))
    two_seconds_on = _reading(week_resets_at=recorded + timedelta(seconds=2))

    alone = bl.end_cause(
        lift, readings=[two_seconds_on], now=NOW, bound=bl.ceilings(config)
    )
    assert alone == bl.END_RESET

    history = [
        _reading(week_resets_at=recorded - timedelta(seconds=3)),
        two_seconds_on,
    ]
    assert (
        bl.end_cause(lift, readings=history, now=NOW, bound=bl.ceilings(config)) is None
    )


def test_the_provider_jitter_is_the_spread_of_the_stamp_history():
    """The measured figure is the spread, floored at the stamp resolution."""
    recorded = _week_reset()
    assert bl._provider_jitter(recorded, [], clock=bl.SEVEN_DAY) == pytest.approx(
        bl.STAMP_RESOLUTION_SECONDS
    )
    history = [
        (0.21, recorded - timedelta(seconds=3)),
        (0.21, recorded + timedelta(seconds=1)),
    ]
    assert bl._provider_jitter(recorded, history, clock=bl.SEVEN_DAY) == pytest.approx(
        4.0
    )
