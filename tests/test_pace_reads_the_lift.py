"""The pace reads the lift: a group's budget block resolves through a lift.

A lift is resolved in one place and every reader of a group's budget block reads
that result. ``group_pace`` is where the pace path meets the lift: it resolves
the group's block through ``budget_lift.effective_budget`` before deriving the
allowance, so the multiple the allowance is derived against is the lifted one, a
drain-by lift withholds by its own line and an uncapped lift withholds nothing.

Every figure below is derived from one fixture at assertion time. The fixture is
the measurement the plan cites: a 168 h window read 22.0 h in at 21% used against
a 1.1x configured multiple. Every test points ``RECKON_HOME`` at a temporary
directory and asserts the workstation's real lift document is untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget
from reckon.crew import budget_lift as bl
from reckon.crew import window_reading

GROUP = "codex-sub"
BACKEND = "codex-sub"
PROJECT = "lift-test"

# The plan's own measurement: 21% of a 168 h window spent after 22.0 h.
NOW = datetime(2026, 10, 8, 3, 12, tzinfo=UTC)
WEEK_HOURS = 168.0
WEEK_MINUTES = 10_080
MULTIPLE = 1.1
UTILISATION = 0.21
ELAPSED_HOURS = 22.0

LIFT_MULTIPLE = 1.8


def _week_reset(*, elapsed_hours: float = ELAPSED_HOURS, observed_at: datetime = NOW):
    """The seven-day reset the fixture's window shows at ``observed_at``."""
    return observed_at + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _config(**budget_block: object) -> dict:
    block = {
        "pace_multiple": MULTIPLE,
        "utilisation_ceiling_pct": 100.0,
        "resume_reserve_pct": 5.0,
        "coordinator_reserve_pct": 3.0,
    }
    block.update(budget_block)
    return {
        "backends": {BACKEND: {"launch": "cli", "command": "codex", "budget_group": GROUP}},
        "budget": block,
    }


def _window(
    *,
    week_utilisation: float = UTILISATION,
    five_utilisation: float = 0.05,
    elapsed_hours: float = ELAPSED_HOURS,
    observed_at: datetime = NOW,
    week_resets_at: datetime | None = None,
) -> window_reading.WindowReading:
    """The group's one window reading, aged seconds against its observation."""
    reset = week_resets_at or _week_reset(
        elapsed_hours=elapsed_hours, observed_at=observed_at
    )
    figures = (
        window_reading.WindowFigure(
            period="five_hour",
            utilisation=five_utilisation,
            observed_at=observed_at,
            age_seconds=5.0,
            resets_at=budget._iso(observed_at + timedelta(hours=1)),
            window_minutes=300,
        ),
        window_reading.WindowFigure(
            period="seven_day",
            utilisation=week_utilisation,
            observed_at=observed_at,
            age_seconds=5.0,
            resets_at=budget._iso(reset),
            window_minutes=WEEK_MINUTES,
        ),
    )
    return window_reading.WindowReading(
        figures=figures, observed_at=observed_at, age_seconds=5.0
    )


def _lift_reading(
    *,
    week_utilisation: float = UTILISATION,
    elapsed_hours: float = ELAPSED_HOURS,
    observed_at: datetime = NOW,
    week_resets_at: datetime | None = None,
) -> dict:
    """A reading in the shape ``effective_budget`` takes, for a grant."""
    reset = week_resets_at or _week_reset(
        elapsed_hours=elapsed_hours, observed_at=observed_at
    )
    return {
        "observed_at": observed_at.isoformat(),
        "five_hour": {
            "utilisation": 0.05,
            "resets_at": (observed_at + timedelta(hours=1)).isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": reset.isoformat(),
        },
    }


def _entry(
    config: dict,
    *,
    window: window_reading.WindowReading | None = None,
    records=(),
    session: str | None = None,
) -> dict:
    """The single group entry ``group_pace`` emits for the fixture group."""
    report = budget.group_pace(
        config,
        windows={BACKEND: window if window is not None else _window()},
        ready=[{"name": "n", "group": GROUP, "score": 0.0}],
        records=list(records),
        now=NOW,
        session=session,
    )
    found = [entry for entry in report if entry["group"] == GROUP]
    assert len(found) == 1, f"expected one entry for {GROUP!r}, got {found}"
    return found[0]


def _grant(config: dict, **kwargs: object) -> dict:
    kwargs.setdefault("readings", [_lift_reading()])
    kwargs.setdefault("now", NOW)
    # A grant is refused while the environment names a run, which the test
    # process's own does; the grant here is the test's, not a worker's.
    kwargs.setdefault("environ", {})
    kwargs.setdefault("granted_by", "the-pace-reads-the-lift")
    return bl.grant(config, group=GROUP, reason="spend ahead into the window", **kwargs)


_REAL_LIFTS = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF


def _real_lifts_bytes() -> bytes | None:
    return _REAL_LIFTS.read_bytes() if _REAL_LIFTS.exists() else None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the config home at a temp dir; leave the real lift doc untouched."""
    before = _real_lifts_bytes()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    yield
    assert _real_lifts_bytes() == before, "the real budget-lifts.json was written"


def test_the_configured_pace_holds_the_implement_dispatch() -> None:
    """With no lift, the fixture is over pace and an implement dispatch holds."""
    verdict = budget.pace_hold(_entry(_config()), "implement")

    assert verdict["held"] is True
    assert verdict["bookend"] is False


def test_a_lift_raises_the_multiple_the_allowance_is_derived_against() -> None:
    """The 1.8 lift admits the dispatch"""
    config = _config()
    lift = _grant(config, multiple=LIFT_MULTIPLE)
    entry = _entry(config)

    assert entry["allowance"]["pace_multiple"] == pytest.approx(LIFT_MULTIPLE)
    assert entry["allowance"]["lift"]["id"] == lift["id"]
    assert budget.pace_hold(entry, "implement")["held"] is False

    # And the entry the pre-flight emits carries the same lifted multiple and id.
    report = budget.preflight(
        PROJECT,
        config,
        backends=[BACKEND],
        windows={BACKEND: _window()},
        now=NOW,
        session=None,
    )
    group = next(g for g in report["groups"] if g["group"] == GROUP)
    assert group["allowance"]["pace_multiple"] == pytest.approx(LIFT_MULTIPLE)
    assert group["allowance"]["lift"]["id"] == lift["id"]


def test_a_moved_seven_day_reset_ends_the_lift() -> None:
    """A reading whose seven-day reset has moved is a fresh window."""
    config = _config()
    _grant(config, multiple=LIFT_MULTIPLE)
    moved = _week_reset() + timedelta(hours=1)

    entry = _entry(config, window=_window(week_resets_at=moved))

    assert entry["allowance"].get("lift") is None
    assert budget.pace_hold(entry, "implement")["held"] is True


def test_a_drain_by_lift_holds_above_its_line_and_admits_below() -> None:
    """The line runs from the grant figure to 100% at the target."""
    config = _config()
    target = NOW + timedelta(hours=10)
    _grant(config, form=bl.DRAIN_BY, target=budget._iso(target))

    above = _entry(config, window=_window(week_utilisation=0.5)
                   )
    below = _entry(config, window=_window(week_utilisation=0.1))

    assert budget.pace_hold(above, "implement")["held"] is True
    assert budget.pace_hold(below, "implement")["held"] is False


def test_an_uncapped_lift_admits_at_any_burn() -> None:
    """An uncapped lift removes the pace hold entirely."""
    config = _config()
    _grant(config, form=bl.UNCAPPED)
    entry = _entry(config, window=_window(week_utilisation=0.99))

    assert budget.pace_hold(entry, "implement")["held"] is False
    assert "uncapped" in budget.pace_hold(entry, "implement")["reason"]


def test_a_lift_whose_from_is_in_the_future_is_inert() -> None:
    """A lift that has not started withholds nothing and releases nothing."""
    config = _config()
    _grant(config, multiple=LIFT_MULTIPLE, starts_at=NOW + timedelta(hours=1))

    entry = _entry(config)

    assert entry["allowance"].get("lift") is None
    assert budget.pace_hold(entry, "implement")["held"] is True


def test_a_session_lift_admits_its_own_session_and_holds_another() -> None:
    """A session-scoped lift governs only the session it names."""
    config = _config()
    _grant(config, multiple=LIFT_MULTIPLE, scope=bl.SESSION_PREFIX + "s1")

    mine = _entry(config, session="s1")
    theirs = _entry(config, session="s2")

    assert budget.pace_hold(mine, "implement")["held"] is False
    assert budget.pace_hold(theirs, "implement")["held"] is True
    assert theirs["allowance"].get("lift") is None


def test_the_history_the_resolver_reads_is_the_records_in_observed_order(
    monkeypatch,
) -> None:
    """The resolver receives the group's readings in observed order.

    A reset-anchored lift ends on a fall between two consecutive readings, so the
    sequence -- not only the newest figure -- has to reach the resolver. It does:
    the readings come from the group's own records in observed order.
    """
    config = _config()
    _grant(config, multiple=LIFT_MULTIPLE)
    seen: list[list[dict]] = []
    original = bl.effective_budget

    def spy(*args, **kwargs):
        seen.append(list(kwargs.get("readings") or ()))
        return original(*args, **kwargs)

    monkeypatch.setattr(bl, "effective_budget", spy)

    early = {"backend": BACKEND, "pace": {
        "recorded_at": (NOW - timedelta(hours=2)).isoformat(),
        "clocks": {"seven_day": {"state": "observed", "utilisation": 0.18,
                                 "resets_at": budget._iso(_week_reset())}},
    }}
    late = {"backend": BACKEND, "pace": {
        "recorded_at": (NOW - timedelta(hours=1)).isoformat(),
        "clocks": {"seven_day": {"state": "observed", "utilisation": 0.20,
                                 "resets_at": budget._iso(_week_reset())}},
    }}

    _entry(config, records=[early, late])

    assert seen, "the resolver was not asked"
    readings = seen[-1]
    stamps = [r["observed_at"] for r in readings]
    assert stamps == sorted(stamps), f"readings are not in observed order: {stamps}"
    assert (NOW - timedelta(hours=2)).isoformat() in stamps
    assert (NOW - timedelta(hours=1)).isoformat() in stamps


def test_group_pace_leaves_the_records_a_replay_reads_untouched() -> None:
    """The rows a replay reads still carry the multiple each dispatch recorded.

    A lift is resolved for the decision, not written back into the records, so a
    replay that reads the multiple each row recorded is unaffected by a lift.
    """
    config = _config()
    _grant(config, multiple=LIFT_MULTIPLE)
    records = [{"backend": BACKEND, "pace": {
        "recorded_at": NOW.isoformat(),
        "policy": {"drain_lead_hours": 12.0, "pace_multiple": MULTIPLE},
        "clocks": {"seven_day": {"state": "observed", "utilisation": UTILISATION,
                                 "resets_at": budget._iso(_week_reset())}},
    }}]
    snapshot = json.dumps(records, sort_keys=True)

    _entry(config, records=records)

    assert json.dumps(records, sort_keys=True) == snapshot
