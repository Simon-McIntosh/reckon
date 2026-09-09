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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    NO_BOUNDED_TOOL_SPANS = auto()


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
    generation_seconds: float | Unmeasured
    machine_seconds: float | Unmeasured

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


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a rollout record's ISO timestamp, tolerating the trailing Z."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _segment_cumulative(readings: Sequence[int]) -> int:
    """Sum a cumulative client reading across its resets.

    The client's running token total resets when a session compacts its
    context; every reset shows up as a decrease in the reading.  Between
    resets the reading climbs from the segment's base to the segment's
    consumed amount.  The first segment starts from zero — its first reading
    is itself already-consumed input — so it is counted in full; each later
    segment contributes its climb above the reset base.  A single monotone
    run therefore reproduces the last reading exactly, while a session that
    compacted once or more reconstructs a total that no single reading
    carries.
    """
    total = 0
    segment_base = readings[0]
    previous = readings[0]
    first_segment = True
    for value in readings[1:]:
        if value < previous:
            if first_segment:
                total += previous
                first_segment = False
            else:
                total += previous - segment_base
            segment_base = value
        previous = value
    if first_segment:
        total += previous
    else:
        total += previous - segment_base
    return total


def _bounded_tool_span(
    spans: Sequence[tuple[datetime, datetime]],
    earliest: datetime,
    latest: datetime,
) -> tuple[float, float, float] | None:
    """Fold bounded tool intervals into wall, machine and model seconds.

    A bounded tool span joins a tool request to its result — the window the
    model sat waiting on the machine.  Overlapping spans (parallel tool calls)
    are joined so a wait is charged once, and the model's own time is wall
    minus that joined wait.  Returns None only when no span is bounded, which
    is the explicit-unmeasured state rather than a claim of zero model time.
    """
    if not spans:
        return None
    intervals = sorted(spans)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    machine = sum((end - start).total_seconds() for start, end in merged)
    wall = (latest - earliest).total_seconds()
    generation = max(0.0, wall - machine)
    return wall, machine, generation


def read_rollout_receipt(session_id: str) -> RolloutReceipt:
    """Return the measured client receipt values for one crew session.

    The cumulative fields are rebuilt per segment across every reset of the
    client's ``total_token_usage`` reading, because a session that compacted
    its context resets that counter and the last reading then under-reports
    while the maximum misrepresents a count that no longer accumulates.
    ``generation_seconds`` and ``machine_seconds`` bound the tool spans in
    the rollout and subtract them from wall, so a codex run's model time is
    measured rather than invented.  The maximum and crossing count range over
    every measured ``last_token_usage`` object.  Missing, unreadable,
    tokenless, span-less, and quota-less rollouts retain distinct unmeasured
    reasons; a measured zero is never used as a missing-value substitute.
    """
    path = _locate_rollout(str(session_id))
    if path is None:
        return _unmeasured(Unmeasured.MISSING_ROLLOUT)

    token_count_seen = False
    total_inputs: list[int] = []
    total_cached: list[int] = []
    total_outputs: list[int] = []
    request_inputs: list[int] = []
    latest_context_window: int | None = None
    latest_rate_limits: Mapping[str, object] | None = None
    earliest: datetime | None = None
    latest: datetime | None = None
    open_calls: dict[object, datetime] = {}
    bounded_spans: list[tuple[datetime, datetime]] = []

    def _observe_timestamp(value: object) -> None:
        nonlocal earliest, latest
        moment = _parse_timestamp(value)
        if moment is None:
            return
        if earliest is None or moment < earliest:
            earliest = moment
        if latest is None or moment > latest:
            latest = moment

    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                _observe_timestamp(record.get("timestamp"))
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                payload_type = payload.get("type")
                if payload_type == "token_count":
                    token_count_seen = True
                    info = payload.get("info")
                    if isinstance(info, Mapping):
                        total = info.get("total_token_usage")
                        if isinstance(total, Mapping):
                            for bucket, key in (
                                (total_inputs, "input_tokens"),
                                (total_cached, "cached_input_tokens"),
                                (total_outputs, "output_tokens"),
                            ):
                                value = _integer(total.get(key))
                                if value is not None:
                                    bucket.append(value)
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
                elif payload_type in ("custom_tool_call", "function_call"):
                    call_id = payload.get("call_id")
                    moment = _parse_timestamp(record.get("timestamp"))
                    if call_id is not None and moment is not None:
                        open_calls[call_id] = moment
                elif payload_type in (
                    "custom_tool_call_output",
                    "function_call_output",
                ):
                    call_id = payload.get("call_id")
                    moment = _parse_timestamp(record.get("timestamp"))
                    started = open_calls.pop(call_id, None)
                    if started is not None and moment is not None and moment >= started:
                        bounded_spans.append((started, moment))
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

    span = (
        _bounded_tool_span(bounded_spans, earliest, latest)
        if earliest is not None and latest is not None
        else None
    )
    if span is None:
        generation_seconds: float | Unmeasured = Unmeasured.NO_BOUNDED_TOOL_SPANS
        machine_seconds: float | Unmeasured = Unmeasured.NO_BOUNDED_TOOL_SPANS
    else:
        _wall, machine, generation = span
        generation_seconds = round(generation, 3)
        machine_seconds = round(machine, 3)

    cumulative_input: int | Unmeasured
    cumulative_cached: int | Unmeasured
    cumulative_output: int | Unmeasured
    # Each field is guarded independently: a truncated total object may carry
    # only some buckets, and an absent bucket is unmeasured, never zero.
    cumulative_input = (
        _segment_cumulative(total_inputs)
        if total_inputs
        else Unmeasured.NO_TOTAL_TOKEN_USAGE
    )
    cumulative_cached = (
        _segment_cumulative(total_cached)
        if total_cached
        else Unmeasured.NO_TOTAL_TOKEN_USAGE
    )
    cumulative_output = (
        _segment_cumulative(total_outputs)
        if total_outputs
        else Unmeasured.NO_TOTAL_TOKEN_USAGE
    )

    return RolloutReceipt(
        cumulative_input_tokens=cumulative_input,
        cumulative_cached_input_tokens=cumulative_cached,
        cumulative_output_tokens=cumulative_output,
        maximum_request_input_tokens=maximum_request,
        requests_over_threshold=crossing_count,
        model_context_window=context_window,
        quota_readings=_quota_readings(latest_rate_limits),
        plan_type=_quota_text(latest_rate_limits, "plan_type"),
        generation_seconds=generation_seconds,
        machine_seconds=machine_seconds,
    )


__all__ = [
    "REQUEST_INPUT_CROSSING_THRESHOLD",
    "WEEKLY_WINDOW_MINUTES",
    "QuotaReading",
    "RolloutReceipt",
    "Unmeasured",
    "read_rollout_receipt",
]
