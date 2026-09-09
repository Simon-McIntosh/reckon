from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.metering import (
    SURCHARGED_REQUEST_INPUT_TOKENS,
    UNMEASURED,
    AccumulatedRunSpend,
    StreamTokenUsage,
    accumulate_run_spend,
    accumulation_key,
    is_durable,
    measure_stream_tokens,
    run_streams,
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


# ── Lineage accumulation ────────────────────────────────────────────────────


def _codex_stream(path: Path, input_tokens: int, output_tokens: int = 1) -> Path:
    """A one-turn codex stream carrying a distinctive input total."""
    return _write_stream(
        path,
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                },
            }
        ],
    )


def _record(run_id: str, **extra: object) -> dict[str, object]:
    return {"run_id": run_id, **extra}


def _accumulate(
    runs: list[dict[str, object]],
    run_id: str,
    streams_root: Path,
) -> AccumulatedRunSpend:
    result = accumulate_run_spend(runs, run_id, streams_root=streams_root)
    assert isinstance(result, AccumulatedRunSpend)
    return result


def test_resumed_run_totals_all_eight_attempt_files_not_the_named_file(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r-resumed"
    run_dir.mkdir(parents=True)
    inputs = [100 + index for index in range(8)]
    for index, input_tokens in enumerate(inputs):
        name = "stream.jsonl" if index == 0 else f"resume-{index}.jsonl"
        _codex_stream(run_dir / name, input_tokens)
    streams = [run_dir / "stream.jsonl", *sorted(run_dir.glob("resume-*.jsonl"))]
    assert len(streams) == 8
    runs = [_record("r-resumed", log_path=str(run_dir / "resume-7.jsonl"))]

    result = _accumulate(runs, "r-resumed", tmp_path)

    assert result.measured_stream_count == 8
    assert result.cumulative_input_tokens == sum(inputs)
    assert result.cumulative_input_tokens > inputs[-1]
    assert result.total_charged_tokens == sum(inputs) + len(inputs)


def test_redispatch_folds_into_lineage_root_run_id(tmp_path: Path) -> None:
    root_stream = _codex_stream(tmp_path / "root.jsonl", 310)
    rd_stream = _codex_stream(tmp_path / "redispatch.jsonl", 470)
    runs = [
        _record(
            "r-root",
            log_path=str(root_stream),
            throughput={"elapsed_seconds": 100.0, "generation_seconds": 40.0},
        ),
        _record(
            "r-redispatch",
            log_path=str(rd_stream),
            lineage={
                "kind": "redispatch",
                "root_run_id": "r-root",
                "previous_run_id": "r-root",
            },
        ),
    ]

    for queried in ("r-root", "r-redispatch"):
        result = _accumulate(runs, queried, tmp_path)
        assert result.run_id == "r-root"
        assert result.durable is True
        assert result.folded_run_count == 2
        assert result.cumulative_input_tokens == 780
        assert result.measured_stream_count == 2

    alone = _accumulate(runs[:1], "r-root", tmp_path)
    assert alone.cumulative_input_tokens < 780
    assert alone.elapsed_seconds == 100.0


def test_accumulator_folds_recorded_times_across_the_chain(tmp_path: Path) -> None:
    rows = [
        _record(
            "r-root",
            log_path=str(_codex_stream(tmp_path / "a.jsonl", 10)),
            throughput={"elapsed_seconds": 600.0, "generation_seconds": 200.0},
        ),
        _record(
            "r-next",
            log_path=str(_codex_stream(tmp_path / "b.jsonl", 10)),
            lineage={"kind": "redispatch", "root_run_id": "r-root"},
            throughput={"elapsed_seconds": 900.0, "generation_seconds": 300.0},
        ),
    ]

    result = _accumulate(rows, "r-next", tmp_path)

    assert result.elapsed_seconds == 1500.0
    assert result.generation_seconds == 500.0
    assert result.machine_seconds == 1000.0


def test_lane_change_row_is_counted_durable(tmp_path: Path) -> None:
    lane = _record(
        "r-lane",
        log_path=str(_codex_stream(tmp_path / "lane.jsonl", 640)),
        lineage={"kind": "lane-change", "attempt": 1, "root_run_id": "r-lane"},
    )

    assert is_durable(lane) is True
    result = _accumulate([lane], "r-lane", tmp_path)
    assert result.durable is True
    assert result.folded_run_count == 1
    assert result.cumulative_input_tokens == 640


def test_shadow_is_keyed_to_itself_and_absent_from_its_primary(
    tmp_path: Path,
) -> None:
    primary_stream = _codex_stream(tmp_path / "primary.jsonl", 800)
    shadow_stream = _codex_stream(tmp_path / "shadow.jsonl", 250)
    primary = _record("r-primary", log_path=str(primary_stream))
    shadow = _record(
        "r-shadow",
        log_path=str(shadow_stream),
        lineage={"kind": "shadow", "primary_run_id": "r-primary"},
    )
    runs = [primary, shadow]

    assert accumulation_key(shadow) == "r-shadow"
    assert is_durable(shadow) is False
    primary_total = _accumulate(runs, "r-primary", tmp_path)
    shadow_total = _accumulate(runs, "r-shadow", tmp_path)

    assert primary_total.cumulative_input_tokens == 800
    assert primary_total.durable is True
    assert primary_total.folded_run_count == 1
    assert shadow_total.cumulative_input_tokens == 250
    assert shadow_total.durable is False
    assert shadow_total.folded_run_count == 1


def test_shadow_carrying_a_root_run_id_is_still_keyed_to_itself(
    tmp_path: Path,
) -> None:
    root_stream = _codex_stream(tmp_path / "chain-root.jsonl", 900)
    shadow_stream = _codex_stream(tmp_path / "rooted-shadow.jsonl", 120)
    root = _record("r-chain-root", log_path=str(root_stream))
    shadow = _record(
        "r-rooted-shadow",
        log_path=str(shadow_stream),
        lineage={
            "kind": "shadow",
            "root_run_id": "r-chain-root",
            "primary_run_id": "r-chain-root",
        },
    )
    runs = [root, shadow]

    assert accumulation_key(shadow) == "r-rooted-shadow"
    assert accumulation_key(root) == "r-chain-root"
    root_total = _accumulate(runs, "r-chain-root", tmp_path)
    assert root_total.cumulative_input_tokens == 900
    assert root_total.folded_run_count == 1
    rooted_shadow_total = _accumulate(runs, "r-rooted-shadow", tmp_path)
    assert rooted_shadow_total.cumulative_input_tokens == 120
    assert rooted_shadow_total.durable is False


def test_unknown_lineage_kind_is_durable_by_default() -> None:
    unknown = {
        "run_id": "r-future",
        "lineage": {"kind": "seedless-replay", "root_run_id": "r-ancient"},
    }

    assert is_durable({}) is True
    assert is_durable({"run_id": "r-plain"}) is True
    assert is_durable(unknown) is True
    assert is_durable({"lineage": {"kind": "shadow"}}) is False
    assert "exclusion" in is_durable.__doc__


def test_durability_does_not_absorb_the_contamination_question() -> None:
    contaminated_redispatch = {
        "run_id": "r-redispatch",
        "lineage": {
            "kind": "redispatch",
            "root_run_id": "r-root",
            "previous_run_id": "r-root",
        },
        "shadow_contaminated": "primary_commit_read",
    }
    contaminated_shadow = {
        "run_id": "r-shadow",
        "lineage": {
            "kind": "shadow",
            "primary_run_id": "r-primary",
        },
        "shadow_contaminated": "primary_commit_read",
    }

    assert is_durable(contaminated_redispatch) is True
    assert is_durable(contaminated_shadow) is False


def test_contamination_is_a_measurement_reason_not_a_durability_reason() -> None:
    from reckon import ledger

    contaminated_non_shadow = {
        "run_id": "r-redispatch",
        "lineage": {
            "kind": "redispatch",
            "root_run_id": "r-root",
            "previous_run_id": "r-root",
        },
        "shadow_contaminated": "primary_commit_read",
    }

    assert is_durable(contaminated_non_shadow) is True
    assert (
        ledger.measurement_exclusion_reason(contaminated_non_shadow) == "contaminated"
    )


def test_measure_stream_tokens_is_wired_through_the_accumulator(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r-wired"
    run_dir.mkdir(parents=True)
    _codex_stream(run_dir / "stream.jsonl", 555)
    runs = [_record("r-wired", log_path=str(run_dir / "stream.jsonl"))]

    result = _accumulate(runs, "r-wired", tmp_path)

    assert result.cumulative_input_tokens == 555
    assert result.measured_stream_count == 1
    assert result.total_charged_tokens == 556


def test_run_streams_returns_the_original_before_numbered_resumes(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r-ordered"
    run_dir.mkdir(parents=True)
    expected = [run_dir / "stream.jsonl"]
    _codex_stream(expected[0], 0)
    for index in range(1, 4):
        path = run_dir / f"resume-{index}.jsonl"
        _codex_stream(path, index)
        expected.append(path)
    path = run_dir / "resume-10.jsonl"
    _codex_stream(path, 10)
    expected.append(path)

    assert run_streams(run_dir / "resume-10.jsonl") == expected
    assert run_streams(Path("")) == []
