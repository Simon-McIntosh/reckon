"""Read per-session quota and token receipts from Codex client rollouts.

This surface is deliberately separate from :mod:`reckon.crew.metering`, which
derives cumulative token usage from the crew event stream.  The client rollout
contains the per-request usage needed to measure the largest request; the crew
stream does not.  Both surfaces are correct for the evidence they carry.

The client's quota ``used_percent`` is quantised to whole percentage points.
One run can therefore appear to consume zero, one, two, or three points, and a
run that consumed four tenths of a point is indistinguishable from one that
consumed nothing.  Per-run attribution from this field is not sound; aggregates
over many runs are.  This module returns the raw quota reading and deliberately
does not compute a per-run delta.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path

CLIENT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# The published rule counts only requests strictly above 272,000 input tokens.
REQUEST_INPUT_CROSSING_THRESHOLD = 272_000

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z")


class Unmeasured(StrEnum):
    """Reasons a rollout receipt cannot support a returned figure."""

    MISSING_ROLLOUT = auto()
    UNREADABLE_ROLLOUT = auto()
    NO_TOKEN_COUNT = auto()
    NO_TOTAL_TOKEN_USAGE = auto()
    NO_REQUEST_TOKEN_USAGE = auto()
    NO_CONTEXT_WINDOW = auto()
    NO_RATE_LIMITS = auto()
    NO_RATE_LIMIT_VALUE = auto()


WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


@dataclass(frozen=True, slots=True)
class QuotaReading:
    """One quota window, retained with the dimensions that identify it."""

    window_minutes: int
    used_percent: int | float | Unmeasured
    resets_at: int | float | Unmeasured


@dataclass(frozen=True, slots=True)
class RolloutReceipt:
    """Measured client receipt values, with an explicit marker for every gap."""

    cumulative_input_tokens: int | Unmeasured
    cumulative_cached_input_tokens: int | Unmeasured
    cumulative_output_tokens: int | Unmeasured
    maximum_request_input_tokens: int | Unmeasured
    requests_over_threshold: int | Unmeasured
    model_context_window: int | Unmeasured
    quota_readings: Mapping[int, QuotaReading] | Unmeasured
    plan_type: str | Unmeasured

    def quota_for_window(self, window_minutes: int) -> QuotaReading | Unmeasured:
        """Return the reading for one window length without positional lookup."""
        if isinstance(self.quota_readings, Unmeasured):
            return self.quota_readings
        return self.quota_readings.get(window_minutes, Unmeasured.NO_RATE_LIMIT_VALUE)

    @property
    def weekly_quota(self) -> QuotaReading | Unmeasured:
        """Return the seven-day reading, wherever it appeared in the receipt."""
        return self.quota_for_window(WEEKLY_WINDOW_MINUTES)

    @property
    def short_horizon_quota(self) -> QuotaReading | Unmeasured:
        """Return the shortest positive window shorter than one week."""
        if isinstance(self.quota_readings, Unmeasured):
            return self.quota_readings
        short_windows = (
            window
            for window in self.quota_readings
            if 0 < window < WEEKLY_WINDOW_MINUTES
        )
        window = min(short_windows, default=None)
        if window is None:
            return Unmeasured.NO_RATE_LIMIT_VALUE
        return self.quota_readings[window]

    @property
    def five_hour_quota(self) -> QuotaReading | Unmeasured:
        """Return the account's short-horizon reading."""
        return self.short_horizon_quota

    @property
    def quota_used_percent(self) -> int | float | Unmeasured:
        """Compatibility view of the weekly utilisation."""
        reading = self.weekly_quota
        return reading if isinstance(reading, Unmeasured) else reading.used_percent

    @property
    def quota_window_minutes(self) -> int | Unmeasured:
        """Compatibility view of the weekly window length."""
        reading = self.weekly_quota
        return reading if isinstance(reading, Unmeasured) else reading.window_minutes

    @property
    def quota_resets_at(self) -> int | float | Unmeasured:
        """Compatibility view of the weekly reset time."""
        reading = self.weekly_quota
        return reading if isinstance(reading, Unmeasured) else reading.resets_at


def _unmeasured(reason: Unmeasured) -> RolloutReceipt:
    return RolloutReceipt(*(reason for _ in RolloutReceipt.__dataclass_fields__))


def _locate_rollout(session_id: str) -> Path | None:
    if _SESSION_ID.fullmatch(session_id) is None:
        return None
    candidates = CLIENT_SESSIONS_DIR.glob(f"*/*/*/rollout-*-{session_id}.jsonl")
    return max(candidates, key=str, default=None)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _usage_value(usage: Mapping[str, object] | None, key: str) -> int | Unmeasured:
    if usage is None:
        return Unmeasured.NO_TOTAL_TOKEN_USAGE
    value = _integer(usage.get(key))
    return value if value is not None else Unmeasured.NO_TOTAL_TOKEN_USAGE


def _quota_number(
    quota: Mapping[str, object] | None, key: str
) -> int | float | Unmeasured:
    if quota is None:
        return Unmeasured.NO_RATE_LIMITS
    value = _number(quota.get(key))
    return value if value is not None else Unmeasured.NO_RATE_LIMIT_VALUE


def _quota_text(quota: Mapping[str, object] | None, key: str) -> str | Unmeasured:
    if quota is None:
        return Unmeasured.NO_RATE_LIMITS
    value = quota.get(key)
    return value if isinstance(value, str) and value else Unmeasured.NO_RATE_LIMIT_VALUE


def _quota_readings(
    rate_limits: Mapping[str, object] | None,
) -> Mapping[int, QuotaReading] | Unmeasured:
    if rate_limits is None:
        return Unmeasured.NO_RATE_LIMITS

    readings: dict[int, QuotaReading] = {}
    for name in ("primary", "secondary"):
        quota = rate_limits.get(name)
        if not isinstance(quota, Mapping):
            continue
        window_minutes = _integer(quota.get("window_minutes"))
        if window_minutes is None or window_minutes <= 0:
            continue
        readings[window_minutes] = QuotaReading(
            window_minutes=window_minutes,
            used_percent=_quota_number(quota, "used_percent"),
            resets_at=_quota_number(quota, "resets_at"),
        )
    return readings


def read_rollout_receipt(session_id: str) -> RolloutReceipt:
    """Return the last cumulative and quota readings for one crew session.

    The cumulative fields come from the last ``total_token_usage`` object,
    while the maximum and crossing count range over every measured
    ``last_token_usage`` object.  Missing, unreadable, tokenless, and
    quota-less rollouts retain distinct unmeasured reasons; a measured zero is
    never used as a missing-value substitute.
    """
    path = _locate_rollout(str(session_id))
    if path is None:
        return _unmeasured(Unmeasured.MISSING_ROLLOUT)

    token_count_seen = False
    latest_total: Mapping[str, object] | None = None
    request_inputs: list[int] = []
    latest_context_window: int | None = None
    latest_rate_limits: Mapping[str, object] | None = None

    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                payload = record.get("payload")
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("type") != "token_count"
                ):
                    continue
                token_count_seen = True

                info = payload.get("info")
                if isinstance(info, Mapping):
                    total = info.get("total_token_usage")
                    if isinstance(total, Mapping):
                        latest_total = total
                    request = info.get("last_token_usage")
                    if isinstance(request, Mapping):
                        request_input = _integer(request.get("input_tokens"))
                        if request_input is not None:
                            request_inputs.append(request_input)
                    context_window = _integer(info.get("model_context_window"))
                    if context_window is not None:
                        latest_context_window = context_window

                rate_limits = payload.get("rate_limits")
                if isinstance(rate_limits, Mapping):
                    latest_rate_limits = rate_limits
    except (OSError, UnicodeError):
        return _unmeasured(Unmeasured.UNREADABLE_ROLLOUT)

    if not token_count_seen:
        return _unmeasured(Unmeasured.NO_TOKEN_COUNT)

    maximum_request = (
        max(request_inputs) if request_inputs else Unmeasured.NO_REQUEST_TOKEN_USAGE
    )
    crossing_count = (
        sum(
            request_input > REQUEST_INPUT_CROSSING_THRESHOLD
            for request_input in request_inputs
        )
        if request_inputs
        else Unmeasured.NO_REQUEST_TOKEN_USAGE
    )
    context_window: int | Unmeasured = (
        latest_context_window
        if latest_context_window is not None
        else Unmeasured.NO_CONTEXT_WINDOW
    )

    return RolloutReceipt(
        cumulative_input_tokens=_usage_value(latest_total, "input_tokens"),
        cumulative_cached_input_tokens=_usage_value(
            latest_total, "cached_input_tokens"
        ),
        cumulative_output_tokens=_usage_value(latest_total, "output_tokens"),
        maximum_request_input_tokens=maximum_request,
        requests_over_threshold=crossing_count,
        model_context_window=context_window,
        quota_readings=_quota_readings(latest_rate_limits),
        plan_type=_quota_text(latest_rate_limits, "plan_type"),
    )


__all__ = [
    "REQUEST_INPUT_CROSSING_THRESHOLD",
    "WEEKLY_WINDOW_MINUTES",
    "QuotaReading",
    "RolloutReceipt",
    "Unmeasured",
    "read_rollout_receipt",
]
