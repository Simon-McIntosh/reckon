from __future__ import annotations

import json
from dataclasses import fields
from itertools import accumulate
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import rollout
from reckon.crew.rollout import (
    REQUEST_INPUT_CROSSING_THRESHOLD,
    WEEKLY_WINDOW_MINUTES,
    Unmeasured,
    read_rollout_receipt,
)


def _usage(input_tokens: int, *, cached: int, output: int) -> dict[str, int]:
    reasoning = output // 2
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output,
    }


def _token_record(
    *,
    total_input: int,
    request_input: int,
    sequence: int,
    context_window: int,
    used_percent: float | None,
    rate_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "token_count",
        "info": {
            "total_token_usage": _usage(
                total_input,
                cached=total_input - request_input,
                output=sum(range(sequence + 1)),
            ),
            "last_token_usage": _usage(
                request_input,
                cached=max(0, request_input - sequence),
                output=sequence,
            ),
            "model_context_window": context_window,
        },
    }
    if used_percent is not None:
        payload["rate_limits"] = rate_limits or {
            "primary": {
                "used_percent": used_percent,
                "window_minutes": WEEKLY_WINDOW_MINUTES,
                "resets_at": 1_800_000_000 + sequence,
            },
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        }
    return {"type": "event_msg", "payload": payload}


def _write_rollout(root: Path, session_id: str, records: list[dict[str, Any]]) -> Path:
    directory = root / "2026" / "09" / "08"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-08T00-00-00-{session_id}.jsonl"
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records))
    return path


def _receipt_values(receipt: object) -> tuple[object, ...]:
    return tuple(getattr(receipt, item.name) for item in fields(receipt))


def test_receipt_uses_latest_totals_and_all_single_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    threshold = REQUEST_INPUT_CROSSING_THRESHOLD
    above = [threshold + offset for offset in range(1, 4)]
    below = [threshold - offset for offset in range(1, 5)]
    request_inputs = [
        below[0],
        above[0],
        below[1],
        above[1],
        below[2],
        above[2],
        below[3],
    ]
    cumulative_inputs = list(accumulate(request_inputs))
    context_window = max(request_inputs) + threshold
    records = [
        _token_record(
            total_input=total_input,
            request_input=request_input,
            sequence=sequence,
            context_window=context_window,
            used_percent=float(sequence),
        )
        for sequence, (total_input, request_input) in enumerate(
            zip(cumulative_inputs, request_inputs, strict=True), start=1
        )
    ]
    _write_rollout(tmp_path, "constructed-session", records)

    receipt = read_rollout_receipt("constructed-session")
    expected_total = records[-1]["payload"]["info"]["total_token_usage"]
    expected_quota = records[-1]["payload"]["rate_limits"]["primary"]

    assert receipt.cumulative_input_tokens == expected_total["input_tokens"]
    assert (
        receipt.cumulative_cached_input_tokens == expected_total["cached_input_tokens"]
    )
    assert receipt.cumulative_output_tokens == expected_total["output_tokens"]
    assert receipt.maximum_request_input_tokens == max(request_inputs)
    assert receipt.requests_over_threshold == sum(
        request_input > threshold for request_input in request_inputs
    )
    assert receipt.maximum_request_input_tokens < receipt.cumulative_input_tokens
    assert receipt.model_context_window == context_window
    assert receipt.quota_used_percent == expected_quota["used_percent"]
    assert receipt.quota_window_minutes == expected_quota["window_minutes"]
    assert receipt.quota_resets_at == expected_quota["resets_at"]


def test_quota_accessors_follow_window_length_not_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    weekly_window = WEEKLY_WINDOW_MINUTES
    short_window = 5 * 60
    weekly_used = 42.0
    short_used = weekly_used * 2
    weekly_reset = 1_800_000_000
    short_reset = weekly_reset + short_window

    weekly_first = _token_record(
        total_input=1,
        request_input=1,
        sequence=1,
        context_window=258_400,
        used_percent=weekly_used,
        rate_limits={
            "primary": {
                "used_percent": weekly_used,
                "window_minutes": weekly_window,
                "resets_at": weekly_reset,
            },
            "secondary": None,
            "plan_type": "standard",
        },
    )
    short_first = _token_record(
        total_input=1,
        request_input=1,
        sequence=1,
        context_window=121_600,
        used_percent=short_used,
        rate_limits={
            "primary": {
                "used_percent": short_used,
                "window_minutes": short_window,
                "resets_at": short_reset,
            },
            "secondary": {
                "used_percent": weekly_used,
                "window_minutes": weekly_window,
                "resets_at": weekly_reset,
            },
            "plan_type": "standard",
        },
    )
    _write_rollout(tmp_path, "weekly-first", [weekly_first])
    _write_rollout(tmp_path, "short-first", [short_first])

    weekly_receipt = read_rollout_receipt("weekly-first")
    short_receipt = read_rollout_receipt("short-first")

    assert weekly_receipt.weekly_quota == short_receipt.weekly_quota
    assert weekly_receipt.weekly_quota.used_percent == weekly_used
    assert weekly_receipt.short_horizon_quota is Unmeasured.NO_RATE_LIMIT_VALUE
    assert short_receipt.short_horizon_quota.used_percent == short_used
    assert (
        short_receipt.weekly_quota.used_percent
        != short_receipt.short_horizon_quota.used_percent
    )
    assert short_receipt.weekly_quota.resets_at == weekly_reset
    assert short_receipt.model_context_window == 121_600
    assert short_receipt.plan_type == "standard"


def test_quota_keeps_unfamiliar_windows_and_measured_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    unfamiliar_window = 11 * 60
    weekly_window = WEEKLY_WINDOW_MINUTES
    unfamiliar_reset = 1_800_000_011
    records = [
        _token_record(
            total_input=1,
            request_input=1,
            sequence=1,
            context_window=121_600,
            used_percent=0,
            rate_limits={
                "primary": {
                    "used_percent": 0,
                    "window_minutes": unfamiliar_window,
                    "resets_at": unfamiliar_reset,
                },
                "secondary": {
                    "used_percent": 0,
                    "window_minutes": weekly_window,
                    "resets_at": unfamiliar_reset + weekly_window,
                },
                "plan_type": "metered",
            },
        )
    ]
    _write_rollout(tmp_path, "unfamiliar-window", records)

    receipt = read_rollout_receipt("unfamiliar-window")

    assert unfamiliar_window in receipt.quota_readings
    assert receipt.quota_readings[unfamiliar_window].used_percent == 0
    assert receipt.short_horizon_quota.window_minutes == unfamiliar_window
    assert receipt.weekly_quota.used_percent == 0


def test_two_absent_quota_entries_are_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    _write_rollout(
        tmp_path,
        "empty-quota-entries",
        [
            _token_record(
                total_input=1,
                request_input=1,
                sequence=1,
                context_window=121_600,
                used_percent=0,
                rate_limits={"primary": None, "secondary": None},
            )
        ],
    )

    receipt = read_rollout_receipt("empty-quota-entries")

    assert receipt.weekly_quota is Unmeasured.NO_RATE_LIMIT_VALUE
    assert receipt.short_horizon_quota is Unmeasured.NO_RATE_LIMIT_VALUE


def test_zero_quota_position_is_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    request_input = REQUEST_INPUT_CROSSING_THRESHOLD // 2
    records = [
        _token_record(
            total_input=request_input,
            request_input=request_input,
            sequence=1,
            context_window=REQUEST_INPUT_CROSSING_THRESHOLD,
            used_percent=float(0),
        )
    ]
    _write_rollout(tmp_path, "zero-quota-session", records)

    measured = read_rollout_receipt("zero-quota-session")
    missing = read_rollout_receipt("absent-session")
    expected_used_percent = records[-1]["payload"]["rate_limits"]["primary"][
        "used_percent"
    ]

    assert measured.quota_used_percent == expected_used_percent
    assert measured.quota_used_percent is not missing.quota_used_percent
    assert missing.quota_used_percent is Unmeasured.MISSING_ROLLOUT


def test_missing_rollout_is_explicitly_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)

    receipt = read_rollout_receipt("absent-session")

    assert set(_receipt_values(receipt)) == {Unmeasured.MISSING_ROLLOUT}


def test_unreadable_rollout_is_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    receipt_path = _write_rollout(
        tmp_path,
        "unreadable-session",
        [
            _token_record(
                total_input=1,
                request_input=1,
                sequence=1,
                context_window=REQUEST_INPUT_CROSSING_THRESHOLD,
                used_percent=float(0),
            )
        ],
    )
    original_open = Path.open

    def _deny_receipt(path: Path, *args: object, **kwargs: object) -> object:
        if path == receipt_path:
            raise PermissionError("constructed unreadable rollout")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _deny_receipt)

    receipt = read_rollout_receipt("unreadable-session")

    assert set(_receipt_values(receipt)) == {Unmeasured.UNREADABLE_ROLLOUT}


def test_rollout_without_token_count_is_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    _write_rollout(
        tmp_path,
        "no-token-count-session",
        [{"type": "event_msg", "payload": {"type": "task_started"}}],
    )

    receipt = read_rollout_receipt("no-token-count-session")

    assert set(_receipt_values(receipt)) == {Unmeasured.NO_TOKEN_COUNT}


def test_token_count_without_rate_limits_marks_only_quota_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    request_input = REQUEST_INPUT_CROSSING_THRESHOLD // 2
    _write_rollout(
        tmp_path,
        "no-rate-limits-session",
        [
            _token_record(
                total_input=request_input,
                request_input=request_input,
                sequence=1,
                context_window=REQUEST_INPUT_CROSSING_THRESHOLD,
                used_percent=None,
            )
        ],
    )

    receipt = read_rollout_receipt("no-rate-limits-session")

    assert receipt.cumulative_input_tokens == request_input
    assert receipt.maximum_request_input_tokens == request_input
    assert receipt.requests_over_threshold == sum(
        value > REQUEST_INPUT_CROSSING_THRESHOLD for value in [request_input]
    )
    assert {
        receipt.quota_used_percent,
        receipt.quota_window_minutes,
        receipt.quota_resets_at,
    } == {Unmeasured.NO_RATE_LIMITS}


def test_real_ledger_sessions_fit_the_reported_context_window() -> None:
    ledger_path = (
        Path(__file__).resolve().parents[1] / "docs" / "state" / "reckon" / "crew.json"
    )
    ledger = json.loads(ledger_path.read_text())
    session_ids: list[str] = []
    for run in reversed(ledger["data"]["runs"]):
        backend = str(run.get("agent", {}).get("backend") or "")
        session_id = str(run.get("session_id") or "")
        if backend.startswith("codex") and session_id and session_id not in session_ids:
            session_ids.append(session_id)
        if len(session_ids) == 12:
            break
    assert len(session_ids) == 12

    receipts = [read_rollout_receipt(session_id) for session_id in session_ids]
    files_found = sum(
        receipt.cumulative_input_tokens is not Unmeasured.MISSING_ROLLOUT
        for receipt in receipts
    )
    context_windows = {
        receipt.model_context_window
        for receipt in receipts
        if isinstance(receipt.model_context_window, int)
    }
    window_lengths = {
        window_minutes
        for receipt in receipts
        if isinstance(receipt.quota_readings, dict)
        for window_minutes, reading in receipt.quota_readings.items()
        if isinstance(reading.used_percent, (int, float))
    }
    for receipt in receipts:
        maximum = receipt.maximum_request_input_tokens
        context_window = receipt.model_context_window
        if isinstance(maximum, int) and isinstance(context_window, int):
            assert maximum <= context_window
        if isinstance(receipt.quota_readings, dict):
            for window_minutes, reading in receipt.quota_readings.items():
                assert window_minutes > 0
                if isinstance(reading.used_percent, (int, float)):
                    assert 0 <= reading.used_percent <= 100

    print(f"real rollout retention: {files_found}/{len(session_ids)}")
    print(f"real quota window lengths: {len(window_lengths)}")
    print(f"real context windows: {len(context_windows)}")
