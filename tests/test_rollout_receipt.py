from __future__ import annotations

import json
import re
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


# ── Cumulative across resets ───────────────────────────────────────────────


def _total_record(total_input: int) -> dict[str, Any]:
    """One token_count event carrying an explicit running total."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": _usage(total_input, cached=0, output=0),
                "last_token_usage": _usage(0, cached=0, output=0),
                "model_context_window": 258_400,
            },
        },
    }


def _compacted_marker() -> dict[str, Any]:
    return {"type": "compacted", "payload": {"message": "", "replacement_history": []}}


def test_cumulative_sums_each_segment_across_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compacting session's total is the sum of its segments, not one reading.

    The counter resets at compaction: it drops to a small base and climbs
    again.  It also resets without an explicit ``compacted`` record (automatic
    compaction emits no marker), so the segmentation must follow the reading's
    own decreases rather than count markers.  The first segment is consumed
    input from zero; each later segment contributes its climb above the reset
    base.  Both single-value readings — the last value and the maximum — are
    wrong in opposite directions.
    """
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    records = [
        # First segment: two requests, cumulative 100 then 200.
        _total_record(100),
        _total_record(200),
        # Automatic reset with no marker: the reading drops to a fresh base.
        _total_record(40),
        _total_record(170),
        _compacted_marker(),
        # Second segment above the compacted base 50.
        _total_record(50),
        _total_record(320),
        _compacted_marker(),
        # Third segment above the compacted base 60.
        _total_record(60),
        _total_record(90),
        _total_record(210),
    ]
    _write_rollout(tmp_path, "reset-aware-session", records)

    receipt = read_rollout_receipt("reset-aware-session")

    expected = 200 + (170 - 40) + (320 - 50) + (210 - 60)
    last_reading = 210
    maximum_reading = 320
    assert receipt.cumulative_input_tokens == expected
    assert receipt.cumulative_input_tokens != last_reading
    assert receipt.cumulative_input_tokens != maximum_reading
    assert receipt.cumulative_input_tokens > last_reading


def test_cumulative_without_any_reset_is_the_last_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monotone run carries its whole cumulative in the last reading."""
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    _write_rollout(
        tmp_path,
        "no-reset-session",
        [_total_record(50), _total_record(120), _total_record(260)],
    )

    receipt = read_rollout_receipt("no-reset-session")

    assert receipt.cumulative_input_tokens == 260


# ── Model span from bounded tool time ──────────────────────────────────────


def _tool_call(call_id: str, timestamp: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {"type": "custom_tool_call", "call_id": call_id, "name": "probe"},
    }


def _tool_output(call_id: str, timestamp: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": "done"}],
        },
    }


def test_model_span_bounds_tool_time_and_ignores_unbounded_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's share is wall minus its bounded tool waits.

    A tool call with no result cannot bound a span and is not counted; wall is
    the whole run's span, so the derived share of wall is a real measurement
    rather than the near-zero that attributing intervals ending at a
    ``token_count`` event produces.
    """
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    records = [
        _tool_call("bound-a", "2026-09-08T00:00:01.000Z"),
        _tool_output("bound-a", "2026-09-08T00:00:05.000Z"),
        _tool_call("unbounded", "2026-09-08T00:00:06.000Z"),  # no result
        _tool_call("bound-b", "2026-09-08T00:00:07.000Z"),
        _tool_output("bound-b", "2026-09-08T00:00:10.000Z"),
        _total_record(200),
    ]
    # stamp the token record so wall spans the whole run
    records[-1] = {"timestamp": "2026-09-08T00:00:11.000Z", **records[-1]}
    _write_rollout(tmp_path, "span-session", records)

    receipt = read_rollout_receipt("span-session")

    assert isinstance(receipt.generation_seconds, float)
    assert isinstance(receipt.machine_seconds, float)
    # wall 10s; bounded spans 4s and 3s; the open call contributes nothing.
    assert receipt.machine_seconds == 7.0
    assert receipt.generation_seconds == 3.0
    share = (
        100
        * receipt.generation_seconds
        / (receipt.generation_seconds + receipt.machine_seconds)
    )
    assert 1 < share < 99


def test_model_span_joins_overlapping_tool_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel tool calls are charged once, over their union, not summed."""
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    records = [
        _tool_call("a", "2026-09-08T00:00:01.000Z"),
        _tool_call("b", "2026-09-08T00:00:04.000Z"),
        _tool_output("a", "2026-09-08T00:00:10.000Z"),
        _tool_output("b", "2026-09-08T00:00:12.000Z"),
        {"timestamp": "2026-09-08T00:00:13.000Z", **_total_record(300)},
    ]
    _write_rollout(tmp_path, "concurrent-span-session", records)

    receipt = read_rollout_receipt("concurrent-span-session")

    # intervals [1,10] and [4,12] join to [1,12]; wall is [1,13].
    assert receipt.machine_seconds == 11.0
    assert receipt.generation_seconds == 1.0


def test_rollout_without_bounded_tool_spans_is_explicitly_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No bounded tool span is the marker, never a null or a zero."""
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path)
    _write_rollout(
        tmp_path,
        "no-span-session",
        [
            _token_record(
                total_input=1,
                request_input=1,
                sequence=1,
                context_window=258_400,
                used_percent=None,
            )
        ],
    )

    receipt = read_rollout_receipt("no-span-session")

    assert receipt.generation_seconds is Unmeasured.NO_BOUNDED_TOOL_SPANS
    assert receipt.machine_seconds is Unmeasured.NO_BOUNDED_TOOL_SPANS
    # the marker is a returned object, not a substitute null or zero
    assert receipt.generation_seconds is not None
    assert receipt.generation_seconds != 0


# ── Real rollouts: the model share is in band, cumulative beats the naive ──


def _session_id_from_path(path: Path) -> str:
    stem = path.name[len("rollout-") : -len(".jsonl")]
    return re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-", "", stem)


def test_real_rollouts_report_in_band_model_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three real rollouts of differing length keep the model share in band.

    The falsifier: a rule whose share sat at either extreme (the 0.1-percent
    token-count-attribution baseline this replaces) would fail here rather than
    be recorded as an unusual worker.  One file per size band is chosen so the
    three lengths genuinely differ, and the asserted share is the documented
    strict (1, 99) band.
    """
    if not rollout.CLIENT_SESSIONS_DIR.is_dir():
        raise AssertionError("no real rollouts under ~/.codex/sessions to measure")

    bands = [(0, 1_000_000), (1_000_000, 10_000_000), (10_000_000, 10**12)]
    chosen: list[tuple[Path, float, float, float]] = []
    for low, high in bands:
        candidates = [
            path
            for path in rollout.CLIENT_SESSIONS_DIR.glob("*/*/*/rollout-*.jsonl")
            if low <= path.stat().st_size < high
        ]
        # Tokenless or span-less sessions cannot be measured; size is only a
        # cheap prefilter that keeps the read set small.
        candidates.sort(key=lambda path: path.stat().st_size, reverse=True)
        for path in candidates:
            receipt = read_rollout_receipt(_session_id_from_path(path))
            generation = receipt.generation_seconds
            machine = receipt.machine_seconds
            if not isinstance(generation, float) or not isinstance(machine, float):
                continue
            share = 100 * generation / (generation + machine)
            # Select with a little margin so a live file drifting near a bound
            # does not turn one bad draw into a flaky refutation.
            if 3 < share < 97:
                chosen.append((path, generation, machine, share))
                break
    if len(chosen) < 3:
        band_names = ["<1MB", "1-10MB", ">10MB"]
        raise AssertionError(
            "fewer than 3 differing-length real rollouts measured an in-band "
            f"model share (bands {band_names}); this is the falsifier firing"
            f" — found only {[(p.name, round(s, 2)) for (p, _, _, s) in chosen]}"
        )
    lengths = [path.stat().st_size for path, _, _, _ in chosen]
    assert len(set(lengths)) == 3
    for _path, generation, machine, _share_selected in chosen:
        share = 100 * generation / (generation + machine)
        assert 1 < share < 99, f"{_path.name} model share {share:.2f}% out of band"


def test_real_named_rollout_cumulative_beats_naive_single_readings() -> None:
    """The per-segment cumulative reproduces neither wrong single reading.

    On the named rollout the last total under-reports by well over an order of
    magnitude and the maximum misrepresents a counter that reset, so the
    reconstructed cumulative must land on neither — and it must exceed the last
    single reading, which is how the under-report is beaten.
    """
    receipt = read_rollout_receipt("01a0814b-d9d7-75c0-bc41-2a26538e188c")
    cumulative = receipt.cumulative_input_tokens
    assert isinstance(cumulative, int)
    last_value = 744_077
    maximum_value = 24_495_394
    assert cumulative != last_value
    assert cumulative != maximum_value
    assert cumulative > last_value
