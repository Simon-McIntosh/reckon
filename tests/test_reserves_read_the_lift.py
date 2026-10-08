"""The three reserve readers read a group's effective budget block.

Three readers stand between a dispatch and a metered wallet's reserves: the
bookend refusal in :mod:`reckon.crew.dispatch_admission`, the wallet's own
reported figures in :mod:`reckon.crew.budget_group`, and the dispatch-purpose
reserve check in :mod:`reckon.crew.routing`, which reaches
:func:`reckon.budget.policy`. While a lift is in force on a declared group all
three reserves read zero for it, so the wallet's implementation ceiling opens to
100% and an implement dispatch is admitted to the whole window. Once the lift
ends — at a moved reset, at a passed stated time, or at a clear — each reader
returns to the figure the configured reserves give, and with no lift the
review-exclusion rule still zeroes the bookend reserve exactly where it did
before.

Every figure here is derived from the fixtures at assertion time rather than
written down, so a retuned reserve moves both the expectation and the report.
The negative control this file declares — replacing ``effective_budget`` with
one that returns ``config["budget"]`` unchanged — is applied on a scratch copy,
not here; these cases are the green arm.

Each case points the configuration home at a temporary directory and asserts
the workstation's own ``budget-lifts.json`` is untouched afterwards, because a
case that writes the live lift record is a monitor for the machine rather than
a test of the code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_group as bg
from reckon.crew import budget_lift as bl
from reckon.crew import reserve as reserve_module
from reckon.crew.dispatch_admission import _refuse_against_the_bookend_reserve
from reckon.crew.node import CrewError
from reckon.crew.routing import _budget_verdict
from reckon.crew.window_reading import WindowFigure, WindowReading

WEEK_HOURS = bl.CLOCK_HOURS[bl.SEVEN_DAY]
GROUP = "codex-sub"
LANE = "codex"
NOW = datetime(2026, 10, 8, 3, 12, tzinfo=UTC)
ENV = {"USER": "lead"}

# The elapsed fraction the fixtures place the wallet at. The window reading the
# dispatch is judged on sits above the implement ceiling the configured bookend
# reserve draws and below the provider's own ceiling, so the reserve is the only
# thing that could refuse the dispatch.
ELAPSED_HOURS = 22.0
WEEK_UTILISATION = 0.85
FIVE_UTILISATION = 0.05


def _week_reset(*, elapsed_hours: float = ELAPSED_HOURS) -> datetime:
    """The seven-day reset a group shows after ``elapsed_hours`` of its week."""
    return NOW + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _reading(
    *,
    week_utilisation: float = WEEK_UTILISATION,
    elapsed_hours: float = ELAPSED_HOURS,
    week_resets_at: datetime | None = None,
) -> dict:
    """One group reading, in the shape the lift resolver reads."""
    return {
        "observed_at": NOW.isoformat(),
        "five_hour": {
            "utilisation": FIVE_UTILISATION,
            "resets_at": (NOW + timedelta(hours=4)).isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": (
                week_resets_at or _week_reset(elapsed_hours=elapsed_hours)
            ).isoformat(),
        },
    }


def _clocks(reading: dict) -> dict:
    """One reading as the pace row publishes it: clocks carrying a state each."""
    return {
        "five_hour": {
            "period": "five_hour",
            "state": "observed",
            "utilisation": reading["five_hour"]["utilisation"],
            "resets_at": reading["five_hour"]["resets_at"],
        },
        "seven_day": {
            "period": "seven_day",
            "state": "observed",
            "utilisation": reading["seven_day"]["utilisation"],
            "resets_at": reading["seven_day"]["resets_at"],
        },
    }


def _config(*, bookend: float = 25.0, excluded: tuple[str, ...] = ()) -> dict:
    """A configuration declaring one wallet, its reserves, and its exclusions."""
    return {
        "backends": {LANE: {"budget_group": GROUP}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
            "coordinator_reserve_pct": 3.0,
            "bookend_reserve_pct": bookend,
        },
        "review_excluded_backends": list(excluded),
    }


def _pace_record(reading: dict) -> dict:
    """The dispatch's own pace row: its wallet, its clocks, and when it was read."""
    return {
        "group": GROUP,
        "lane": LANE,
        "recorded_at": NOW.isoformat(),
        "clocks": _clocks(reading),
    }


def _grant(config: dict, *, readings: list[dict], **kwargs) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend the window into its reset",
        multiple=1.8,
        readings=readings,
        now=NOW,
        environ=ENV,
        **kwargs,
    )


@pytest.fixture()
def guarded_home(tmp_path, monkeypatch):
    """Point the config home at a throwaway tree, watching the real one."""
    real = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF
    before = real.read_bytes() if real.exists() else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = real.read_bytes() if real.exists() else None
    assert after == before, "a case wrote the workstation's budget-lifts.json"


# ── The bookend refusal ─────────────────────────────────────────────────────


def test_the_bookend_refusal_admits_under_a_lift_and_holds_without_one(
    guarded_home,
) -> None:
    """The pair: the same reading, opposite verdicts, either side of a clear.

    The reading sits above the implement ceiling the configured bookend reserve
    draws and below the provider's own ceiling, so the reserve is the only thing
    that could refuse it. Under the lift it is admitted; once cleared, the same
    reading is refused against the reserved fraction. The refused half is what
    shows the admission is the lift rather than a window with room.
    """
    config = _config()
    reading = _reading()
    _grant(config, readings=[reading])

    _refuse_against_the_bookend_reserve(
        config=config, role="implement", pace_record=_pace_record(reading)
    )

    bl.clear(config, group=GROUP, now=NOW + timedelta(minutes=1), environ=ENV)

    with pytest.raises(CrewError) as refusal:
        _refuse_against_the_bookend_reserve(
            config=config, role="implement", pace_record=_pace_record(reading)
        )

    bookend = float(config["budget"][reserve_module.RESERVE_KEY])
    assert (
        f"the window keeps {bookend:g}% for review and verify roles"
        in str(refusal.value)
    ), str(refusal.value)


def test_the_bookend_refusal_returns_on_a_moved_reset(guarded_home) -> None:
    """A reading whose seven-day reset has moved ends the lift at that reading.

    The row's clocks are the newest reading a reset-anchored lift checks its
    recorded reset against, so a dispatch judged after the roll is refused
    against the configured reserve without anything running to lower the lift.
    """
    config = _config()
    reading = _reading()
    _grant(config, readings=[reading])

    moved = _reading(week_resets_at=_week_reset() + timedelta(hours=1))
    with pytest.raises(CrewError):
        _refuse_against_the_bookend_reserve(
            config=config, role="implement", pace_record=_pace_record(moved)
        )


# ── The wallet's own figures ────────────────────────────────────────────────


def test_group_figures_reports_no_reserve_under_a_lift(guarded_home) -> None:
    """The wallet a reader consults reports the reserve gone while a lift holds.

    The lift ends on a clear and the figure returns to the configured reserve,
    read from the configuration at assertion time, so the expectation and the
    report move together if the declaration is retuned.
    """
    config = _config()
    reading = _reading()
    bookend = float(config["budget"][reserve_module.RESERVE_KEY])

    lifted = _grant(config, readings=[reading])
    assert lifted["id"]

    assert bg.group_figures(GROUP, config, {}, now=NOW).reserve_pct == 0.0

    bl.clear(config, group=GROUP, now=NOW + timedelta(minutes=1), environ=ENV)
    assert bg.group_figures(GROUP, config, {}, now=NOW).reserve_pct == pytest.approx(
        bookend
    )


def test_group_figures_returns_the_reserve_once_the_stated_time_passes(
    guarded_home,
) -> None:
    """A lift with a stated end is inert past it, and the reserve returns."""
    config = _config()
    reading = _reading()
    bookend = float(config["budget"][reserve_module.RESERVE_KEY])
    end = NOW + timedelta(hours=1)
    _grant(config, readings=[reading], ends={"kind": "at", "at": end.isoformat()})

    assert bg.group_figures(GROUP, config, {}, now=NOW).reserve_pct == 0.0
    assert bg.group_figures(
        GROUP, config, {}, now=end + timedelta(hours=1)
    ).reserve_pct == pytest.approx(bookend)


def test_a_window_reading_that_moved_the_reset_returns_the_reserve(
    guarded_home,
) -> None:
    """The wallet's own figures end a lift too, from the reading they report."""
    config = _config()
    reading = _reading()
    bookend = float(config["budget"][reserve_module.RESERVE_KEY])
    _grant(config, readings=[reading])

    window = WindowReading(
        figures=(
            WindowFigure(
                period="five_hour",
                utilisation=FIVE_UTILISATION,
                observed_at=NOW,
                age_seconds=0.0,
                resets_at=reading["five_hour"]["resets_at"],
            ),
            WindowFigure(
                period="seven_day",
                utilisation=WEEK_UTILISATION,
                observed_at=NOW,
                age_seconds=0.0,
                resets_at=(_week_reset() + timedelta(hours=1)).isoformat(),
            ),
        ),
        observed_at=NOW,
    )
    figures = bg.group_figures(GROUP, config, {LANE: window}, now=NOW)
    assert figures.reserve_pct == pytest.approx(bookend)


# ── The reserve check on routing's path ─────────────────────────────────────


def _routing_ceiling(config: dict, home: Path) -> float:
    verdict = _budget_verdict(
        project="sample",
        root=home / "ledger",
        config=config,
        backend_name=LANE,
        backend={"budget_group": GROUP},
        purpose="dispatch",
        budget_state={"backend": LANE, "headroom": "unknown"},
    )
    return float(verdict["effective_ceiling_pct"])


def test_routing_sees_zero_reserves_and_the_full_ceiling_under_a_lift(
    guarded_home,
) -> None:
    """The dispatch-purpose ceiling reads 100% while a lift is in force.

    ``budget.policy`` reads ``config["budget"]`` itself, so the lifted block
    reaches it only because routing hands it the effective one. The ceiling is
    read from the configuration at assertion time, and the configured reserves
    are subtracted for the no-lift half.
    """
    config = _config()
    reading = _reading()
    ceiling = float(config["budget"]["utilisation_ceiling_pct"])
    _grant(config, readings=[reading])

    assert _routing_ceiling(config, guarded_home) == pytest.approx(ceiling)

    bl.clear(config, group=GROUP, now=NOW + timedelta(minutes=1), environ=ENV)
    configured = ceiling - float(
        config["budget"]["resume_reserve_pct"]
    ) - float(config["budget"]["coordinator_reserve_pct"])
    assert _routing_ceiling(config, guarded_home) == pytest.approx(configured)


# ── The review-exclusion rule composes through the one function ─────────────


def test_the_review_exclusion_rule_still_applies_without_a_lift(guarded_home) -> None:
    """A wallet no review can run on withholds no bookend reserve, lift or none.

    The rule is unchanged where there is no lift, and the same wallet under a
    lift resolves through the same single function, so the two rules that can
    zero a reserve do not become two parallel paths.
    """
    reading = _reading()
    config = _config(excluded=(LANE,))

    assert bg.group_figures(GROUP, config, {}, now=NOW).reserve_pct == 0.0

    # A wallet with a review-capable member keeps the configured reserve with no
    # lift, so the zero above is the exclusions' doing, not an unconditional one.
    # The check runs before the grant below, whose lift is keyed by group name
    # and would otherwise cover this wallet too.
    capable = _config()
    assert bg.effective_block(capable, GROUP, readings=[reading], now=NOW)[
        reserve_module.RESERVE_KEY
    ] == pytest.approx(float(capable["budget"][reserve_module.RESERVE_KEY]))

    _grant(config, readings=[reading])
    composed = bg.effective_block(config, GROUP, readings=[reading], now=NOW)
    assert composed[reserve_module.RESERVE_KEY] == 0.0