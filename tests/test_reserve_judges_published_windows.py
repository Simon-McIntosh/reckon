"""Reserve each period the provider reported without inventing a sibling clock."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from reckon import budget
from reckon.crew import reserve, rollout, window_reading
from reckon.crew.dispatch import _refuse_against_the_bookend_reserve
from reckon.crew.node import CrewError

BLOCK = {"bookend_reserve_pct": 20.0, "utilisation_ceiling_pct": 100.0}
MOMENT = datetime(2030, 1, 3, tzinfo=UTC)


def _week(used: float) -> window_reading.WindowFigure:
    return window_reading.WindowFigure(
        period="seven_day",
        utilisation=used,
        observed_at=MOMENT,
        age_seconds=0.0,
        window_minutes=10_080,
    )


def _clocks(used: float, *, failed_five_hour: bool = False) -> dict:
    reported = ("five_hour", "seven_day") if failed_five_hour else ("seven_day",)
    reading = window_reading.WindowReading(
        figures=(_week(used),),
        reported_periods=reported,
        observed_at=MOMENT,
        age_seconds=0.0,
    )
    return {
        period: budget._clock(reading, period) for period in ("five_hour", "seven_day")
    }


def test_week_only_implementation_judges_the_week_and_skips_unpublished_five_hour() -> (
    None
):
    clocks = _clocks(0.01)
    verdict = reserve.admit_windows(BLOCK, role="implement", clocks=clocks)

    assert clocks["five_hour"]["state"] == "not_published"
    assert verdict["admitted"] is True
    assert verdict["windows_judged"] == ["seven_day"]
    assert verdict["windows_skipped"] == ["five_hour"]
    assert "seven-day" in verdict["reason"]
    assert "five-hour" in verdict["reason"]
    _refuse_against_the_bookend_reserve(
        config={"budget": BLOCK},
        role="implement",
        pace_record={"lane": "codex", "group": "codex-sub", "clocks": clocks},
    )


def test_week_only_implementation_is_refused_above_its_ceiling() -> None:
    clocks = _clocks(0.81)
    verdict = reserve.admit_windows(BLOCK, role="implement", clocks=clocks)

    assert verdict["admitted"] is False
    assert verdict["windows_judged"] == ["seven_day"]
    assert verdict["windows_skipped"] == ["five_hour"]
    with pytest.raises(CrewError, match="seven-day"):
        _refuse_against_the_bookend_reserve(
            config={"budget": BLOCK},
            role="implement",
            pace_record={"lane": "codex", "group": "codex-sub", "clocks": clocks},
        )


def test_a_published_five_hour_window_with_a_failed_read_is_unreadable() -> None:
    clocks = _clocks(0.01, failed_five_hour=True)
    verdict = reserve.admit_windows(BLOCK, role="implement", clocks=clocks)

    assert clocks["five_hour"]["state"] == "unknown"
    assert verdict["admitted"] is False
    assert verdict["windows_skipped"] == []
    assert "five-hour" in verdict["reason"]
    assert "could not be read" in verdict["reason"]
    with pytest.raises(CrewError, match="could not be read"):
        _refuse_against_the_bookend_reserve(
            config={"budget": BLOCK},
            role="implement",
            pace_record={"lane": "codex", "group": "codex-sub", "clocks": clocks},
        )


@pytest.mark.parametrize("role", ["review", "verify"])
def test_bookends_keep_the_whole_ceiling_even_with_an_unreadable_clock(
    role: str,
) -> None:
    verdict = reserve.admit_windows(
        BLOCK, role=role, clocks=_clocks(0.85, failed_five_hour=True)
    )

    assert verdict["admitted"] is True
    assert verdict["limit_pct"] == 100.0
    assert "unreadable" in verdict["reason"]


def test_rollout_reports_a_failed_period_as_published() -> None:
    receipt = SimpleNamespace(
        quota_readings={
            300: rollout.QuotaReading(300, rollout.Unmeasured.NO_RATE_LIMIT_VALUE, 1),
            10_080: rollout.QuotaReading(10_080, 1.0, 1),
        }
    )
    reading = budget._rollout_reading(receipt, observed_at=MOMENT, moment=MOMENT)

    assert reading.reported_periods == ("five_hour", "seven_day")
    assert budget._clock(reading, "five_hour")["state"] == "unknown"
    assert budget._clock(reading, "seven_day")["state"] == "observed"


def test_stream_reports_a_malformed_period_as_published() -> None:
    reading = window_reading.read_windows(
        [
            {
                "type": "rate_limit_event",
                "timestamp": MOMENT.isoformat(),
                "rate_limit_info": {
                    "unifiedWindows": {
                        "five_hour": {"utilization": "bad"},
                        "seven_day": {"utilization": 0.01},
                    }
                },
            }
        ],
        now=MOMENT,
    )
    clocks = {
        period: budget._clock(reading, period) for period in ("five_hour", "seven_day")
    }

    assert reading.reported_periods == ("five_hour", "seven_day")
    assert clocks["five_hour"]["state"] == "unknown"
    assert (
        reserve.admit_windows(BLOCK, role="implement", clocks=clocks)["admitted"]
        is False
    )


def test_a_negative_reported_utilisation_is_unreadable() -> None:
    clocks = {
        "seven_day": {
            "period": "seven_day",
            "state": "observed",
            "utilisation": -0.01,
        }
    }
    verdict = reserve.admit_windows(BLOCK, role="implement", clocks=clocks)

    assert verdict["admitted"] is False
    assert "could not be read" in verdict["reason"]
