from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.metering import (
    SURCHARGED_REQUEST_INPUT_TOKENS,
    UNMEASURED,
    StreamTokenUsage,
    measure_stream_tokens,
)

CODEX_RUN = (
    "r-20260908T043606789253-the-matrix-drops-glm-and-carries-the-effort-finding"
)
CLAUDE_RUN = "r-20260904T035133094427-a-session-id-is-always-answerable"


def _write_stream(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records))
    return path


def _measured(path: Path) -> StreamTokenUsage:
    result = measure_stream_tokens(path)
    assert isinstance(result, StreamTokenUsage)
    return result


def _real_stream(run_id: str, name: str = "stream.jsonl") -> Path:
    path = Path.home() / ".config" / "reckon" / "crew" / "runs" / run_id / name
    if not path.is_file():
        pytest.skip(f"real crew stream is not mounted: {path}")
    return path


def _ledger_run(run_id: str) -> dict[str, object]:
    ledger = json.loads(Path("docs/state/reckon/crew.json").read_text())
    return next(row for row in ledger["data"]["runs"] if row["run_id"] == run_id)


def test_constructed_codex_stream_returns_exact_sums_and_turn_count(
    tmp_path: Path,
) -> None:
    records = [
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 31,
                "cached_input_tokens": 13,
                "output_tokens": 7,
                "reasoning_output_tokens": 3,
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 17,
                "cached_input_tokens": 5,
                "output_tokens": 11,
                "reasoning_output_tokens": 2,
            },
        },
    ]
    result = _measured(_write_stream(tmp_path / "codex.jsonl", records))
    usages = [record["usage"] for record in records]
    fresh_input = sum(usage["input_tokens"] for usage in usages)
    cached_input = sum(usage["cached_input_tokens"] for usage in usages)
    output = sum(
        usage["output_tokens"] + usage["reasoning_output_tokens"] for usage in usages
    )

    assert result.dialect == "codex"
    assert result.cumulative_input_tokens == fresh_input + cached_input
    assert result.cumulative_cached_input_tokens == cached_input
    assert result.cumulative_output_tokens == output
    assert result.turn_count == len(records)
    assert result.maximum_request_input_tokens is UNMEASURED
    assert result.surcharged_request_count is UNMEASURED


def test_claude_uses_each_messages_largest_counter_values(tmp_path: Path) -> None:
    smaller = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 17,
        "cache_read_input_tokens": 101,
        "output_tokens": 3,
    }
    larger = {
        "input_tokens": 5,
        "cache_creation_input_tokens": 23,
        "cache_read_input_tokens": 149,
        "output_tokens": 29,
    }
    records = [
        {"type": "assistant", "message": {"id": "message-a", "usage": smaller}},
        {"type": "assistant", "message": {"id": "message-a", "usage": larger}},
    ]
    result = _measured(_write_stream(tmp_path / "claude-duplicate.jsonl", records))
    cached = larger["cache_read_input_tokens"]
    total_input = (
        larger["input_tokens"] + larger["cache_creation_input_tokens"] + cached
    )

    assert result.dialect == "claude"
    assert result.cumulative_input_tokens == total_input
    assert result.cumulative_cached_input_tokens == cached
    assert result.cumulative_output_tokens == larger["output_tokens"]
    assert result.maximum_request_input_tokens == total_input
    assert result.turn_count == 1


def test_constructed_claude_stream_returns_exact_request_measurements(
    tmp_path: Path,
) -> None:
    usages = [
        {
            "input_tokens": 3,
            "cache_creation_input_tokens": SURCHARGED_REQUEST_INPUT_TOKENS,
            "cache_read_input_tokens": 0,
            "output_tokens": 19,
        },
        {
            "input_tokens": 11,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 71,
            "output_tokens": 13,
        },
    ]
    records = [
        {"type": "assistant", "message": {"id": f"message-{index}", "usage": usage}}
        for index, usage in enumerate(usages)
    ]
    result = _measured(_write_stream(tmp_path / "claude.jsonl", records))
    request_inputs = [
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["cache_read_input_tokens"]
        for usage in usages
    ]
    cached_input = sum(usage["cache_read_input_tokens"] for usage in usages)
    fresh_input = sum(
        usage["input_tokens"] + usage["cache_creation_input_tokens"] for usage in usages
    )

    assert result.cumulative_input_tokens == sum(request_inputs)
    assert result.cumulative_cached_input_tokens == cached_input
    assert result.cumulative_output_tokens == sum(
        usage["output_tokens"] for usage in usages
    )
    assert (
        result.cumulative_input_tokens - result.cumulative_cached_input_tokens
        == fresh_input
    )
    assert result.maximum_request_input_tokens == max(request_inputs)
    assert result.surcharged_request_count == sum(
        value > SURCHARGED_REQUEST_INPUT_TOKENS for value in request_inputs
    )
    assert result.turn_count == len(usages)


def test_claude_result_usage_supplies_final_output_total(tmp_path: Path) -> None:
    message_usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 29,
        "cache_read_input_tokens": 103,
        "output_tokens": 1,
    }
    result_usage = {**message_usage, "output_tokens": 47}
    records = [
        {
            "type": "assistant",
            "message": {"id": "message-a", "usage": message_usage},
        },
        {"type": "result", "num_turns": 1, "usage": result_usage},
    ]
    result = _measured(_write_stream(tmp_path / "claude-result.jsonl", records))

    assert result.cumulative_output_tokens == result_usage["output_tokens"]
    assert result.turn_count == records[-1]["num_turns"]


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda tmp_path: tmp_path / "missing.jsonl",
        lambda tmp_path: tmp_path / "stream-directory",
    ],
)
def test_missing_and_unreadable_streams_are_unmeasured(
    tmp_path: Path, path_factory
) -> None:
    path = path_factory(tmp_path)
    if path.name == "stream-directory":
        path.mkdir()

    assert measure_stream_tokens(path) is UNMEASURED


def test_stream_without_usage_is_unmeasured(tmp_path: Path) -> None:
    path = _write_stream(
        tmp_path / "no-usage.jsonl",
        [{"type": "assistant", "message": {"id": "message-a", "content": []}}],
    )

    assert measure_stream_tokens(path) is UNMEASURED


def test_genuine_zero_is_distinct_from_unmeasured(tmp_path: Path) -> None:
    path = _write_stream(
        tmp_path / "zero.jsonl",
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            }
        ],
    )
    result = _measured(path)

    assert result.cumulative_input_tokens == 0
    assert result.cumulative_cached_input_tokens == 0
    assert result.cumulative_output_tokens == 0
    assert result.turn_count == 1
    assert result is not UNMEASURED


def test_real_codex_run_matches_ledger_total_and_cannot_measure_requests() -> None:
    result = _measured(_real_stream(CODEX_RUN, "lane-change-2.jsonl"))
    ledger_run = _ledger_run(CODEX_RUN)
    recorded_total = ledger_run["throughput"]["peak_input_tokens"]

    assert result.dialect == "codex"
    assert result.cumulative_input_tokens > 0
    assert result.cumulative_input_tokens == pytest.approx(recorded_total, rel=0.01)
    assert result.maximum_request_input_tokens is UNMEASURED
    assert result.maximum_request_input_tokens != result.cumulative_input_tokens
    assert result.maximum_request_input_tokens != 0
    assert result.surcharged_request_count is UNMEASURED


def test_real_claude_run_has_smaller_positive_request_maximum_than_total() -> None:
    result = _measured(_real_stream(CLAUDE_RUN))

    assert result.dialect == "claude"
    assert result.turn_count > 1
    assert isinstance(result.maximum_request_input_tokens, int)
    assert 0 < result.maximum_request_input_tokens < result.cumulative_input_tokens
