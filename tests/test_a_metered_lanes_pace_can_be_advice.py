"""A declared wallet's pace can be advice rather than a hold.

The ``budget`` block is flat and applies to every group, so a group whose pace
should be reported rather than enforced is named in
``budget.advisory_pace_groups``. Naming it there does two things and only two:
its pace hold admits at any utilisation the window still has, and its dispatch
ceiling carries none of the resume, coordinator or bookend reserves, so the
whole window is satisfied by the work the group serves. The hard stops are
untouched — an exhausted window with no credits lift is still held, and a
credits lift's cap is still drawn through — because those bound the account
rather than the pace. A group left out of the list keeps every hold and reserve,
so an omission changes nothing.

The negative control reverses the two halves one at a time: applying the pace
hold to an advisory group must fail
``test_an_advisory_group_is_not_held_by_its_pace``, and keeping the reserves
for the group must fail
``test_an_advisory_groups_dispatch_ceiling_is_the_window_itself``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget as budget_module
from reckon import pace_report
from reckon.crew import budget_group, window_reading
from reckon.crew import budget_lift as bl
from reckon.crew import pace as pace_module
from tests.real_home_traces import traces_naming

GROUP = "codex-sub"
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
ENV = {"USER": "lead"}
CAP = 10_000.0
AT_GRANT = 60_564.37

# The week stands 8.5 hours into its 168, so a wallet at 96% is spending at
# 0.96 / (8.5 / 168): a burn multiple of about 19, which is the figure a pace
# hold stops the wallet on.
HOURS_INTO_THE_WEEK = 8.5
ADVISORY_UTILISATION = 0.96


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _config(*, advisory: bool = True) -> dict:
    block = {
        "pace_multiple": 1.1,
        "utilisation_ceiling_pct": 100.0,
        "resume_reserve_pct": 5.0,
        "coordinator_reserve_pct": 3.0,
        "bookend_reserve_pct": 20.0,
        "exhausted_statuses": [],
    }
    if advisory:
        block[pace_module.ADVISORY_KEY] = [GROUP]
    return {
        "backends": {GROUP: {"budget_group": GROUP}},
        "budget": block,
    }


def _reading(five_hour: float, week: float) -> window_reading.WindowReading:
    observed = NOW - timedelta(seconds=30)
    resets = NOW + timedelta(hours=pace_module.WEEK_HOURS - HOURS_INTO_THE_WEEK)
    return window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="five_hour",
                utilisation=five_hour,
                observed_at=observed,
                age_seconds=30.0,
                resets_at=_iso(NOW + timedelta(hours=1.0)),
            ),
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=week,
                observed_at=observed,
                age_seconds=30.0,
                resets_at=_iso(resets),
            ),
        ),
        observed_at=observed,
        age_seconds=30.0,
    )


def _entry(config: dict) -> dict:
    """The single group entry the pace report emits for this configuration."""
    report = budget_module.group_pace(
        config, windows={GROUP: _reading(0.4, ADVISORY_UTILISATION)}, now=NOW
    )
    entries = [row for row in report if row["group"] == GROUP]
    assert len(entries) == 1, f"expected one entry for {GROUP!r}, got {entries}"
    return entries[0]


def _state(utilisation_pct: float, balance: float | None) -> budget_module.BudgetState:
    """One account-surface reading of the group's own window."""
    block = {
        "headroom": "known",
        "utilisation_pct": utilisation_pct,
        "rate_limit_type": "primary",
        "rate_limit_period_minutes": 10080,
        "resets_at": "2026-10-27T06:32:42Z",
        "threshold_status": (
            "rate_limit_reached" if utilisation_pct >= 100.0 else "allowed"
        ),
    }
    if balance is not None:
        block["credits"] = {
            "has_credits": True,
            "unlimited": False,
            "balance": balance,
        }
    return budget_module._from_block(
        GROUP,
        block,
        observed_at=_iso(NOW),
        source="account-surface",
        now=NOW,
    )


def _judged(config: dict, *, credit: bool = False) -> dict:
    """The policy the ceiling is read through, resolved as dispatch resolves it."""
    if credit:
        bl.grant(
            config,
            group=GROUP,
            reason="spend the account's credits",
            form=bl.CREDITS,
            credits=CAP,
            balance_at_grant=AT_GRANT,
            now=NOW,
            environ=ENV,
        )
    block = budget_group.effective_block(
        config, GROUP, readings=None, now=NOW, session=None
    )
    return budget_module.policy({**config, "budget": block})


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the lift store at a temp home and prove the real one is untouched."""
    home = Path.home() / ".config" / "reckon"
    before = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    assert after == before, f"a test wrote the live budget-lifts.json: {after}"


# ── The pace is advice for a declared group ─────────────────────────────────


def test_an_advisory_group_is_not_held_by_its_pace() -> None:
    """96% at a 19x burn is a reading of the pace, not a boundary."""
    entry = _entry(_config())
    allowance = entry["allowance"]
    assert allowance["burn_multiple"] == pytest.approx(19.0, rel=0.05)
    assert allowance["burn_multiple"] > allowance["pace_multiple"]
    assert allowance["advisory"] is True

    verdict = pace_report.pace_hold(entry, "implement")

    assert verdict["held"] is False
    assert verdict["burn_multiple"] > allowance["pace_multiple"]


# ── The ceiling carries no reserves for a declared group ────────────────────


def test_an_advisory_groups_dispatch_ceiling_is_the_window_itself() -> None:
    """100%, not the 92% the resume and coordinator reserves withhold."""
    config = _config()
    judged = _judged(config)

    assert budget_module.effective_ceiling(judged, "dispatch") == 100.0
    at_the_ceiling = budget_module.decide(_state(100.0, None), judged, now=NOW)

    assert at_the_ceiling["held"] is True
    assert "100.0% ceiling" in at_the_ceiling["reason"]


def test_a_group_declaring_nothing_keeps_the_reserved_ceiling() -> None:
    """The same figures without the declaration keep today's derivations."""
    config = _config(advisory=False)
    judged = _judged(config)

    assert budget_module.effective_ceiling(judged, "dispatch") == 92.0
    at_ninety_six = budget_module.decide(_state(96.0, None), judged, now=NOW)

    assert at_ninety_six["held"] is True
    assert "92.0% ceiling" in at_ninety_six["reason"]


def test_a_group_declaring_nothing_is_held_by_its_pace() -> None:
    """An omission changes neither the pace hold nor the ceiling."""
    entry = _entry(_config(advisory=False))
    allowance = entry["allowance"]

    assert allowance["burn_multiple"] > allowance["pace_multiple"]
    verdict = pace_report.pace_hold(entry, "implement")
    assert verdict["held"] is True
    assert verdict["releases_at"] is not None


# ── The hard stops are untouched by the declaration ─────────────────────────


def test_a_credits_lift_still_holds_an_advisory_group_at_its_cap() -> None:
    """The ceiling on drawn credits is the account's, not the pace's."""
    config = _config()
    judged = _judged(config, credit=True)
    stop = CAP * 0.9

    admitted = budget_module.decide(_state(100.0, AT_GRANT - stop + 1), judged, now=NOW)
    spent = budget_module.decide(_state(100.0, AT_GRANT - stop), judged, now=NOW)

    assert admitted["held"] is False
    assert spent["held"] is True


def test_an_unread_balance_falls_back_to_an_advisory_ceiling() -> None:
    """A lift that cannot measure its spend admits nothing past the ceiling."""
    config = _config()
    judged = _judged(config, credit=True)

    verdict = budget_module.decide(_state(100.0, None), judged, now=NOW)

    assert verdict["held"] is True
    assert "credit_lift" not in verdict
    assert "100.0% ceiling" in verdict["reason"]


def test_an_advisory_group_above_the_window_ceiling_is_still_held() -> None:
    """The exhausted-window stop does not read the declaration at all."""
    config = _config()
    judged = _judged(config)

    verdict = budget_module.decide(_state(100.0, None), judged, now=NOW)

    assert verdict["held"] is True
