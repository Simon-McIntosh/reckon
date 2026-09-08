"""The lanes view distinguishes fresh room, risk, stale evidence, and absence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from reckon import mcp_views
from reckon.crew.rollout import Unmeasured


@dataclass(frozen=True)
class _Reading:
    window_minutes: int
    used_percent: int
    resets_at: int


@dataclass(frozen=True)
class _Receipt:
    model_context_window: int | Unmeasured
    quota_readings: dict[int, _Reading] | Unmeasured


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _view(
    *,
    backend: str = "metered",
    used_percent: int = 0,
    observed_at: datetime | None,
    composed_at: datetime,
    receipt: _Receipt | None = None,
) -> dict[str, Any]:
    session_id = "session"
    selected_receipt = receipt or _Receipt(
        model_context_window=1,
        quota_readings={
            5 * 60: _Reading(5 * 60, used_percent, 1),
        },
    )
    run: dict[str, Any] = {
        "backend": backend,
        "session_id": session_id,
    }
    if observed_at is not None:
        run["completed_at"] = _stamp(observed_at)
    result = mcp_views.crew_lanes_view(
        {"backends": {backend: {}}},
        [run],
        receipt_reader=lambda received: (
            selected_receipt
            if received == session_id
            else (_ for _ in ()).throw(AssertionError(received))
        ),
        composed_at=_stamp(composed_at),
    )
    return result["lanes"][0]


def _only_quota_row(lane: dict[str, Any]) -> dict[str, Any]:
    assert len(lane["quota_windows"]) == 1
    return lane["quota_windows"][0]


def test_fresh_utilisation_range_has_a_real_at_risk_band() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    observed_at = composed_at - mcp_views.QUOTA_READING_STALE_AFTER
    at_risk_start = mcp_views.AT_RISK_USED_PERCENT

    for used_percent in range(101):
        row = _only_quota_row(
            _view(
                used_percent=used_percent,
                observed_at=observed_at,
                composed_at=composed_at,
            )
        )
        expected = (
            mcp_views.EXHAUSTED_SERVING_STATE
            if used_percent >= 100
            else mcp_views.AT_RISK_SERVING_STATE
            if used_percent >= at_risk_start
            else mcp_views.AMPLE_SERVING_STATE
        )
        assert row["serving_state"] == expected

    below_risk = at_risk_start - 1
    assert (
        _only_quota_row(
            _view(
                used_percent=below_risk,
                observed_at=observed_at,
                composed_at=composed_at,
            )
        )["serving_state"]
        == mcp_views.AMPLE_SERVING_STATE
    )
    assert (
        _only_quota_row(
            _view(
                used_percent=at_risk_start,
                observed_at=observed_at,
                composed_at=composed_at,
            )
        )["serving_state"]
        == mcp_views.AT_RISK_SERVING_STATE
    )


def test_fresh_ninety_seven_percent_utilisation_is_not_reported_as_ample() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    observed_at = composed_at - mcp_views.QUOTA_READING_STALE_AFTER
    used_percent = 97

    row = _only_quota_row(
        _view(
            used_percent=used_percent,
            observed_at=observed_at,
            composed_at=composed_at,
        )
    )

    assert row["serving_state"] == mcp_views.AT_RISK_SERVING_STATE
    assert row["serving_state"] != mcp_views.AMPLE_SERVING_STATE


def test_fresh_ceiling_utilisation_remains_exhausted() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    observed_at = composed_at - mcp_views.QUOTA_READING_STALE_AFTER
    ceiling = 100

    row = _only_quota_row(
        _view(
            used_percent=ceiling,
            observed_at=observed_at,
            composed_at=composed_at,
        )
    )

    assert row["serving_state"] == mcp_views.EXHAUSTED_SERVING_STATE


def test_stale_reading_stays_stale_even_when_utilisation_is_low() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    observed_at = (
        composed_at - mcp_views.QUOTA_READING_STALE_AFTER - timedelta(seconds=1)
    )

    row = _only_quota_row(
        _view(
            used_percent=0,
            observed_at=observed_at,
            composed_at=composed_at,
        )
    )

    assert row["serving_state"] == mcp_views.STALE_SERVING_STATE


def test_reading_one_second_inside_horizon_uses_utilisation_state() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    observed_at = (
        composed_at - mcp_views.QUOTA_READING_STALE_AFTER + timedelta(seconds=1)
    )

    row = _only_quota_row(
        _view(
            used_percent=0,
            observed_at=observed_at,
            composed_at=composed_at,
        )
    )

    assert row["serving_state"] == mcp_views.AMPLE_SERVING_STATE


def test_unmetered_lane_and_missing_receipt_have_different_states() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    unmetered = _view(
        backend="clive",
        observed_at=composed_at,
        composed_at=composed_at,
    )
    missing = _view(
        observed_at=composed_at,
        composed_at=composed_at,
        receipt=_Receipt(
            model_context_window=Unmeasured.MISSING_ROLLOUT,
            quota_readings=Unmeasured.MISSING_ROLLOUT,
        ),
    )

    assert unmetered["receipt_state"] == "unmetered"
    assert missing["receipt_state"] == "unreadable"
    assert unmetered["receipt_state"] != missing["receipt_state"]


def test_missing_receipt_carries_no_observation_time() -> None:
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    lane = _view(
        observed_at=composed_at - mcp_views.QUOTA_READING_STALE_AFTER,
        composed_at=composed_at,
        receipt=_Receipt(
            model_context_window=Unmeasured.MISSING_ROLLOUT,
            quota_readings=Unmeasured.MISSING_ROLLOUT,
        ),
    )

    assert lane["observed_at"] == mcp_views.UNMEASURED
    assert lane["unmeasured"]["observed_at"] == "no_receipt_observation"
