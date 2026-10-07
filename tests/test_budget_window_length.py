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


def _primary_window(
    window_minutes: int, *, elapsed: timedelta, used_percent: float = 2.0
):
    reset = NOW + timedelta(minutes=window_minutes) - elapsed
    return budget._rate_limits_reading(
        {
            "primary": {
                "window_minutes": window_minutes,
                "used_percent": used_percent,
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


def _entry(report, name):
    return next(entry for entry in report if entry["group"] == name)


def test_a_month_window_allows_the_elapsed_fraction_with_the_pacing_lean() -> None:
    reading = _primary_window(43_200, elapsed=timedelta(days=2), used_percent=6.0)

    allowance = _group(reading)["allowance"]

    assert allowance["window_minutes"] == 43_200
    assert allowance["elapsed_fraction"] == pytest.approx(2 / 30)
    assert allowance["burn_multiple"] == pytest.approx(0.9)
    assert allowance["derived"] == pytest.approx(0.0733, abs=0.001)


def test_a_week_window_keeps_the_allowance_at_the_same_elapsed_fraction() -> None:
    """A week is paced by the same rule as a month, from its own length.

    The fixture is a ten-thousand-and-eighty-minute window as a *reported*
    length rather than the code's fallback, so the assertion separates reading
    the reported length from assuming a fixed one: a derivation fixed at any
    other window length changes both the recorded length and the derived
    complement, and this case reddens on either.
    """
    elapsed_fraction = 2 / 30
    window_minutes = 10_080
    reading = _primary_window(
        window_minutes,
        elapsed=timedelta(minutes=window_minutes * elapsed_fraction),
        used_percent=6.0,
    )

    allowance = _group(reading)["allowance"]

    assert allowance["window_minutes"] == window_minutes
    assert allowance["elapsed_fraction"] == pytest.approx(elapsed_fraction)
    assert allowance["derived"] == pytest.approx(
        elapsed_fraction * PACE_MULTIPLE, abs=0.001
    )


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


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _surface_reading(window_minutes: int, used_percent: float, elapsed_fraction: float):
    """The account surface's own operative window at an exact point in its life."""
    reset = NOW + timedelta(minutes=window_minutes * (1.0 - elapsed_fraction))
    return budget._state_window_reading(
        {
            "source": "account-surface",
            "rate_limit_period_minutes": window_minutes,
            "utilisation_pct": used_percent,
            "observed_at": _iso(NOW),
            "resets_at": _iso(reset),
        },
        moment=NOW,
    )


def _stale_week_receipt(window_minutes: int = 10_080):
    """A committed weekly receipt whose window has already rolled over."""
    observed = datetime(2026, 9, 26, tzinfo=UTC)
    passed = datetime(2026, 9, 30, tzinfo=UTC)
    return budget._receipt_reading(
        {
            "quota_state": "measured",
            "observed_at": _iso(observed),
            "quota_windows": [
                {
                    "window_minutes": window_minutes,
                    "used_percent": 12.0,
                    "resets_at": int(passed.timestamp()),
                    "observed_at": _iso(observed),
                }
            ],
        },
        moment=NOW,
    )


def test_pace_reads_the_account_surface_over_a_stale_weekly_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The group paces to the window the provider reports now, not a rolled-over receipt.

    Both witnesses are supplied at once: a recorded weekly receipt observed
    before its window reset, and a fresh account surface reporting a
    10,080-minute primary window at 3% used with 0.84% of its life elapsed. The
    dispatch pace record must read the surface, or a window that reset four days
    ago would admit the whole of the current one.
    """
    stale = _stale_week_receipt()
    fresh = _surface_reading(10_080, 3.0, 0.0084)
    monkeypatch.setattr(budget, "recorded_windows", lambda *a, **k: {"codex": stale})
    monkeypatch.setattr(
        budget, "_account_surface_readings", lambda *a, **k: {"codex": fresh}
    )

    row = budget.pace_row(
        CONFIG, project="reckon", lane="codex", node="n", score=0.3, now=NOW
    )

    assert row["allowance"]["window_minutes"] == 10_080
    assert row["allowance"]["elapsed_fraction"] == pytest.approx(0.0084, abs=1e-4)
    assert row["bar"]["window_fill"] == pytest.approx(3 / 100)
    # The stale receipt, read alone, would pace the whole window: the fresh read
    # is what withholds it.
    stale_alone = _entry(
        budget.group_pace(CONFIG, windows={"codex": stale}, now=NOW), "codex-sub"
    )
    assert stale_alone["allowance"]["derived"] == pytest.approx(1.0)


def _preflight_config() -> dict:
    return {
        "default_backend": "codex",
        "backends": {
            "codex": {
                "launch": "cli",
                "command": "codex",
                "budget_group": "codex-sub",
                "budget_check": True,
            }
        },
        "roles": {"implement": {"backend": "codex"}, "review": {"backend": "codex"}},
        "budget": {
            "pace_multiple": PACE_MULTIPLE,
            "drain_lead_hours": 12.0,
            "bookend_reserve_pct": 20.0,
        },
    }


def _account_answer(window_minutes: int, used_percent: float, elapsed_fraction: float):
    reset = NOW + timedelta(minutes=window_minutes * (1.0 - elapsed_fraction))
    return {
        "id": 2,
        "result": {
            "rateLimits": {
                "limitId": "metered",
                "limitName": None,
                "primary": {
                    "usedPercent": used_percent,
                    "windowDurationMins": window_minutes,
                    "resetsAt": int(reset.timestamp()),
                },
                "secondary": None,
                "credits": None,
                "planType": "redacted",
                "rateLimitReachedType": None,
            },
            "rateLimitsByLimitId": 43_986,
        },
    }


def test_an_implement_dispatch_is_held_by_the_pace_and_a_review_is_admitted() -> None:
    """The pair: one mature reading at 6x burn holds implementation, admits review.

    A hold that fired for both, or neither, would not separate the roles. The
    reason must name the utilisation, the allowance it exceeds and the reset
    that ends the hold, so a coordinator sees what it is waiting for.
    """
    config = _preflight_config()
    answer = _account_answer(10_080, 30.0, 0.05)

    held = budget.preflight(
        "reckon",
        config,
        records=[_stale_week_receipt_row()],
        windows={},
        probe_runner=lambda probe: answer,
        now=NOW,
        roles=["implement"],
    )
    group = _group_from(held, "codex-sub")
    verdict = group["pace_hold"]

    assert held["held"] is True
    assert verdict["held"] is True
    assert verdict["utilisation"] == pytest.approx(0.30)
    assert verdict["allowance"] == pytest.approx(0.05 * PACE_MULTIPLE, abs=1e-4)
    assert verdict["resets_at"] is not None
    assert "utilisation" in verdict["reason"]
    assert "allowance" in verdict["reason"]
    assert verdict["resets_at"] in verdict["reason"]

    admitted = budget.preflight(
        "reckon",
        config,
        records=[_stale_week_receipt_row()],
        windows={},
        probe_runner=lambda probe: answer,
        now=NOW,
        roles=["review"],
    )
    assert admitted["held"] is False
    assert (
        not _group_from(admitted, "codex-sub").get("pace_hold", {}).get("held", False)
    )


def _stale_week_receipt_row() -> dict:
    observed = datetime(2026, 9, 26, tzinfo=UTC)
    passed = datetime(2026, 9, 30, tzinfo=UTC)
    return {
        "run_id": "r-stale",
        "backend": "codex",
        "gate": "passed",
        "completed_at": _iso(observed),
        "lane_receipt": {
            "quota_state": "measured",
            "observed_at": _iso(observed),
            "quota_windows": [
                {
                    "window_minutes": 10_080,
                    "used_percent": 12.0,
                    "resets_at": int(passed.timestamp()),
                    "observed_at": _iso(observed),
                }
            ],
        },
    }


def _group_from(report, name):
    return next(entry for entry in report["groups"] if entry["group"] == name)


def test_a_short_reported_window_is_retained_and_paced_by_its_own_fraction() -> None:
    """A 1,440-minute window is a real window, not a length to drop.

    A length this reader has not named before is still the account's operative
    window; dropping it falls the group back to a stale receipt. Retained, it is
    paced by its own elapsed fraction like any other window.
    """
    reading = _surface_reading(1_440, 30.0, 0.25)

    group = _entry(
        budget.group_pace(CONFIG, windows={"codex": reading}, now=NOW), "codex-sub"
    )
    allowance = group["allowance"]

    assert reading.known is True
    assert group["state"] == budget.OBSERVED
    assert allowance["window_minutes"] == 1_440
    assert allowance["elapsed_fraction"] == pytest.approx(0.25)
    assert allowance["derived"] == pytest.approx(0.25 * PACE_MULTIPLE)
