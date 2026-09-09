from __future__ import annotations

import json
from datetime import datetime
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
    *,
    now_seconds: float | None = None,
) -> AccumulatedRunSpend:
    result = accumulate_run_spend(
        runs, run_id, streams_root=streams_root, now_seconds=now_seconds
    )
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


# ── Wall for a live run, derived from the pointer's own stamps ─────────────


def _epoch(iso: str) -> float:
    """Epoch seconds of an ISO-8601 ``Z`` stamp, parsed as the source parses it."""
    return datetime.fromisoformat(iso).timestamp()


def test_a_live_run_reports_wall_measured_from_its_stamps(tmp_path: Path) -> None:
    """A run still in flight has no throughput block, so wall is derived.

    The pointer's own creation stamp is the measured base and now the end of
    the span, so the wall cell renders a figure rather than the unmeasured
    marker. ``now_seconds`` pins the observation the fixture's elapsed derives.
    """
    run_dir = tmp_path / "r-live"
    run_dir.mkdir(parents=True)
    _codex_stream(run_dir / "stream.jsonl", 400)
    rows = [
        _record(
            "r-live",
            log_path=str(run_dir / "stream.jsonl"),
            created_at="2026-09-09T10:00:00Z",
            attempt_started_at="2026-09-09T10:00:00Z",
        )
    ]

    result = _accumulate(
        rows, "r-live", tmp_path, now_seconds=_epoch("2026-09-09T12:00:00Z")
    )

    assert result.elapsed_seconds == 7200.0
    assert result.elapsed_from_stamps is True


def test_a_resumed_live_run_sums_its_attempts_spans(tmp_path: Path) -> None:
    """A resumed run's wall counts every attempt, not only the current one.

    ``created_at`` is the original dispatch and ``attempt_started_at`` the
    current attempt, so the summed span reaches back to the creation stamp;
    measuring to the current attempt alone would report the 3,600 seconds
    since the resume instead of the 7,200 seconds since dispatch.
    """
    run_dir = tmp_path / "r-resumed-live"
    run_dir.mkdir(parents=True)
    _codex_stream(run_dir / "stream.jsonl", 500)
    rows = [
        _record(
            "r-resumed-live",
            log_path=str(run_dir / "stream.jsonl"),
            created_at="2026-09-09T10:00:00Z",
            attempt_started_at="2026-09-09T11:00:00Z",
            attempt=2,
        )
    ]

    result = _accumulate(
        rows,
        "r-resumed-live",
        tmp_path,
        now_seconds=_epoch("2026-09-09T12:00:00Z"),
    )

    current_attempt_only = _epoch("2026-09-09T12:00:00Z") - _epoch(
        "2026-09-09T11:00:00Z"
    )
    assert result.elapsed_seconds > current_attempt_only
    assert result.elapsed_seconds == _epoch("2026-09-09T12:00:00Z") - _epoch(
        "2026-09-09T10:00:00Z"
    )


def test_a_completed_row_folds_its_block_even_when_stamps_exist(
    tmp_path: Path,
) -> None:
    """The stamp path is never used where a throughput block exists.

    A record may carry both a block and creation stamps; the block is the
    recorded span and must win. This test fails if the stamp derivation is
    applied to a row that already measured its elapsed.
    """
    run_dir = tmp_path / "r-both"
    run_dir.mkdir(parents=True)
    _codex_stream(run_dir / "stream.jsonl", 300)
    rows = [
        _record(
            "r-both",
            log_path=str(run_dir / "stream.jsonl"),
            created_at="2026-09-09T10:00:00Z",
            attempt_started_at="2026-09-09T11:00:00Z",
            throughput={"elapsed_seconds": 900.0, "generation_seconds": 300.0},
        )
    ]

    result = _accumulate(rows, "r-both", tmp_path)

    assert result.elapsed_seconds == 900.0
    assert result.elapsed_from_stamps is False
    assert result.generation_seconds == 300.0


def test_live_wall_is_marked_derived_not_folded(tmp_path: Path) -> None:
    """A derived live figure carries its provenance apart from a folded span.

    A completed row folds its throughput block (a different measurement
    source), so a reader comparing rows must be able to tell the two apart
    rather than guess which figure each came from.
    """
    live_dir = tmp_path / "r-live-mark"
    live_dir.mkdir(parents=True)
    _codex_stream(live_dir / "stream.jsonl", 100)
    live = _accumulate(
        [
            _record(
                "r-live-mark",
                log_path=str(live_dir / "stream.jsonl"),
                created_at="2026-09-09T10:00:00Z",
                attempt_started_at="2026-09-09T10:00:00Z",
            )
        ],
        "r-live-mark",
        tmp_path,
        now_seconds=_epoch("2026-09-09T12:00:00Z"),
    )
    folded = _accumulate(
        [
            _record(
                "r-complete",
                throughput={"elapsed_seconds": 900.0, "generation_seconds": 300.0},
            )
        ],
        "r-complete",
        tmp_path,
    )

    assert live.elapsed_from_stamps is True
    assert folded.elapsed_from_stamps is False
    assert folded.elapsed_seconds == 900.0


def test_model_and_rate_stay_unmeasured_while_a_run_is_live(tmp_path: Path) -> None:
    """A live run has no inference span, so model and rate stay unmeasured.

    The wall is derived from the pointer's stamps, but no generation span
    exists until a terminal record or a bounded-tool-span rollout does, so
    leaving the cell unmeasured is correct rather than a gap and a zero would
    invent a measurement that was never taken.
    """
    run_dir = tmp_path / "r-live-model"
    run_dir.mkdir(parents=True)
    _codex_stream(run_dir / "stream.jsonl", 600)
    rows = [
        _record(
            "r-live-model",
            log_path=str(run_dir / "stream.jsonl"),
            created_at="2026-09-09T10:00:00Z",
            attempt_started_at="2026-09-09T10:00:00Z",
        )
    ]

    result = _accumulate(
        rows,
        "r-live-model",
        tmp_path,
        now_seconds=_epoch("2026-09-09T12:00:00Z"),
    )

    assert result.elapsed_seconds == 7200.0
    assert result.generation_seconds is None
    assert result.machine_seconds is None


def test_mixed_chain_does_not_fabricate_machine_for_the_live_segment(
    tmp_path: Path,
) -> None:
    """Machine time is not invented for a live segment whose model span is unknown.

    The chain's completed row measured its own generation, and the live row's
    derived wall joins the total, but subtracting a model figure that covers
    only part of the wall would attribute zero model time to the segment that
    is still running.
    """
    completed_dir = tmp_path / "r-c"
    completed_dir.mkdir(parents=True)
    live_dir = tmp_path / "r-l"
    live_dir.mkdir(parents=True)
    _codex_stream(completed_dir / "stream.jsonl", 100)
    _codex_stream(live_dir / "stream.jsonl", 100)
    rows = [
        _record(
            "r-c",
            log_path=str(completed_dir / "stream.jsonl"),
            throughput={"elapsed_seconds": 600.0, "generation_seconds": 200.0},
        ),
        _record(
            "r-l",
            log_path=str(live_dir / "stream.jsonl"),
            created_at="2026-09-09T10:00:00Z",
            attempt_started_at="2026-09-09T10:00:00Z",
            lineage={"kind": "redispatch", "root_run_id": "r-c"},
        ),
    ]

    result = _accumulate(
        rows, "r-c", tmp_path, now_seconds=_epoch("2026-09-09T12:00:00Z")
    )

    assert result.elapsed_seconds == 600.0 + 7200.0
    assert result.generation_seconds == 200.0
    assert result.machine_seconds is None
    assert result.elapsed_from_stamps is True
