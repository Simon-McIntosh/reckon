"""Pace a metered account from the quota window the provider reports.

The account surface may expose one primary window and no short-horizon sibling.
That is a complete reading, not an unreadable five-hour clock.  Its allowance is
the elapsed fraction of its own window multiplied by the configured pacing lean,
so changing the provider horizon changes elapsed time, not the governing rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget
from reckon.crew import reserve

NOW = datetime(2030, 1, 3, tzinfo=UTC)
PACE_MULTIPLE = 1.1
CONFIG = {
    "backends": {
        "codex": {
            "launch": "cli",
            "command": "codex",
            "budget_group": "codex-sub",
        }
    },
    "budget": {
        "pace_multiple": PACE_MULTIPLE,
        "drain_lead_hours": 12.0,
        "bookend_reserve_pct": 20.0,
    },
}


def _primary_window(window_minutes: int, *, elapsed: timedelta):
    reset = NOW + timedelta(minutes=window_minutes) - elapsed
    return budget._rate_limits_reading(
        {
            "primary": {
                "window_minutes": window_minutes,
                "used_percent": 2.0,
                "resets_at": int(reset.timestamp()),
            },
            "secondary": None,
        },
        observed_at=NOW,
        moment=NOW,
    )


def _group(reading):
    report = budget.group_pace(CONFIG, windows={"codex": reading}, now=NOW)
    return next(entry for entry in report if entry["group"] == "codex-sub")


def test_a_month_window_allows_the_elapsed_fraction_with_the_pacing_lean() -> None:
    reading = _primary_window(43_200, elapsed=timedelta(days=2))

    allowance = _group(reading)["allowance"]

    assert allowance["window_minutes"] == 43_200
    assert allowance["elapsed_fraction"] == pytest.approx(2 / 30)
    assert allowance["burn_multiple"] == pytest.approx(0.3)
    assert allowance["derived"] == pytest.approx(0.0733, abs=0.001)


def test_a_week_window_keeps_the_allowance_at_the_same_elapsed_fraction() -> None:
    elapsed_fraction = 2 / 30
    reading = _primary_window(
        10_080,
        elapsed=timedelta(minutes=10_080 * elapsed_fraction),
    )

    allowance = _group(reading)["allowance"]

    assert allowance["window_minutes"] == 10_080
    assert allowance["elapsed_fraction"] == pytest.approx(elapsed_fraction)
    assert allowance["derived"] == pytest.approx(0.0733, abs=0.001)


def test_a_primary_only_account_is_readable_to_preflight_and_the_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reading = _primary_window(43_200, elapsed=timedelta(days=2))

    report = budget.preflight(
        "reckon",
        CONFIG,
        windows={"codex": reading},
        records=[],
        now=NOW,
    )
    group = next(entry for entry in report["groups"] if entry["group"] == "codex-sub")

    assert report["held"] is False
    assert group["state"] == budget.OBSERVED
    assert group["allowance"]["state"] == budget.OBSERVED

    monkeypatch.setattr(
        budget,
        "recorded_windows",
        lambda *_args, **_kwargs: {"codex": reading},
    )
    pace = budget.pace_row(
        CONFIG,
        project="reckon",
        lane="codex",
        node="primary-only",
        score=0.0,
        now=NOW,
    )
    operative = pace["clocks"]["five_hour"]
    verdict = reserve.admit(
        CONFIG["budget"],
        role="implement",
        utilisation_pct=operative["utilisation"] * 100.0,
    )

    assert operative["state"] == budget.OBSERVED
    assert operative["window_minutes"] == 43_200
    assert verdict["admitted"] is True
    assert "unreadable" not in verdict["reason"]
