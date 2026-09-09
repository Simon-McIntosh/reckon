"""Input quantities keep request maxima separate from run totals."""

from __future__ import annotations

import json
from collections.abc import Mapping

from reckon import _backends
from reckon.crew.rollout import RolloutReceipt

CODEX = {"launch": "cli", "command": "codex"}
CLAUDE = {"launch": "cli", "command": "claude"}


def _observe(
    backend: Mapping[str, str],
    events: list[dict[str, object]],
    receipt: object | None = None,
):
    return _backends.observe_stream(
        backend_name="probe",
        backend=backend,
        lines=[json.dumps(event) for event in events],
        receipt=receipt,
    )


def _assistant_event(
    *, input_tokens: int, cached_input_tokens: int, created_input_tokens: int
) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "measured"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cached_input_tokens,
                "cache_creation_input_tokens": created_input_tokens,
                "output_tokens": 1,
            },
        },
    }


def test_codex_run_totals_are_never_reported_as_a_request_peak() -> None:
    usage = {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 7,
        "reasoning_output_tokens": 3,
    }
    throughput = _observe(
        CODEX,
        [
            {"type": "thread.started", "thread_id": "thread-measured"},
            {"type": "turn.completed", "usage": usage},
        ],
    ).throughput

    expected_input = usage["input_tokens"] + usage["cached_input_tokens"]
    assert throughput["cumulative_input_tokens"] == expected_input
    assert throughput["cumulative_cached_input_tokens"] == usage["cached_input_tokens"]
    assert throughput["peak_input_tokens"] is None


def test_codex_run_totals_sum_every_completed_turn() -> None:
    turns = [
        {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
        },
        {
            "input_tokens": 200,
            "cached_input_tokens": 30,
            "output_tokens": 7,
            "reasoning_output_tokens": 3,
        },
        {
            "input_tokens": 300,
            "cached_input_tokens": 40,
            "output_tokens": 11,
            "reasoning_output_tokens": 4,
        },
    ]
    observation = _observe(
        CODEX,
        [{"type": "turn.completed", "usage": usage} for usage in turns],
    )
    throughput = observation.throughput

    expected_input = sum(
        usage["input_tokens"] + usage["cached_input_tokens"] for usage in turns
    )
    expected_cached = sum(usage["cached_input_tokens"] for usage in turns)
    expected_output = sum(
        usage["output_tokens"] + usage["reasoning_output_tokens"] for usage in turns
    )
    largest_turn_input = max(
        usage["input_tokens"] + usage["cached_input_tokens"] for usage in turns
    )
    largest_turn_output = max(
        usage["output_tokens"] + usage["reasoning_output_tokens"] for usage in turns
    )

    assert throughput["cumulative_input_tokens"] == expected_input
    assert throughput["cumulative_input_tokens"] != largest_turn_input
    assert throughput["cumulative_cached_input_tokens"] == expected_cached
    assert throughput["cumulative_cached_input_tokens"] != max(
        usage["cached_input_tokens"] for usage in turns
    )
    assert throughput["generated_tokens"] == expected_output
    assert throughput["generated_tokens"] != largest_turn_output
    budget_tokens = observation.budget["tokens"]
    assert (
        budget_tokens["input_tokens"] + budget_tokens["cached_input_tokens"]
        == throughput["cumulative_input_tokens"]
    )


def test_codex_usage_less_final_turn_does_not_erase_measured_totals() -> None:
    measured_turns = [
        {
            "input_tokens": 90,
            "cached_input_tokens": 10,
            "output_tokens": 4,
        },
        {
            "input_tokens": 180,
            "cached_input_tokens": 20,
            "output_tokens": 8,
        },
    ]
    events = [{"type": "turn.completed", "usage": usage} for usage in measured_turns]
    events.append({"type": "turn.completed"})

    observation = _observe(CODEX, events)

    expected_input = sum(
        usage["input_tokens"] + usage["cached_input_tokens"] for usage in measured_turns
    )
    assert observation.throughput["cumulative_input_tokens"] == expected_input
    assert observation.throughput["cumulative_input_tokens"] is not None
    assert (
        observation.budget["tokens"]["input_tokens"]
        + observation.budget["tokens"]["cached_input_tokens"]
        == expected_input
    )


def test_claude_request_peak_is_the_largest_prompt_not_the_run_total() -> None:
    requests = [
        {"input_tokens": 4, "cached_input_tokens": 10, "created_input_tokens": 2},
        {"input_tokens": 5, "cached_input_tokens": 25, "created_input_tokens": 3},
        {"input_tokens": 6, "cached_input_tokens": 12, "created_input_tokens": 1},
    ]
    prompt_sizes = [sum(request.values()) for request in requests]
    totals = {key: sum(request[key] for request in requests) for key in requests[0]}
    result_usage = {
        "input_tokens": totals["input_tokens"],
        "cache_read_input_tokens": totals["cached_input_tokens"],
        "cache_creation_input_tokens": totals["created_input_tokens"],
        "output_tokens": 11,
    }
    events = [_assistant_event(**request) for request in requests]
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1_000,
            "duration_api_ms": 900,
            "usage": result_usage,
        }
    )

    throughput = _observe(CLAUDE, events).throughput

    assert throughput["peak_input_tokens"] == max(prompt_sizes)
    assert throughput["peak_input_tokens"] != sum(prompt_sizes)
    assert throughput["cumulative_input_tokens"] == sum(prompt_sizes)
    assert throughput["cumulative_cached_input_tokens"] == totals["cached_input_tokens"]


def _measured_receipt(cumulative_input: int) -> RolloutReceipt:
    return RolloutReceipt(
        cumulative_input_tokens=cumulative_input,
        cumulative_cached_input_tokens=cumulative_input // 2,
        cumulative_output_tokens=40_000,
        maximum_request_input_tokens=200_000,
        requests_over_threshold=0,
        model_context_window=258_400,
        quota_readings={},
        plan_type="metered",
        generation_seconds=120.0,
        machine_seconds=45.0,
    )


def test_codex_receipt_cumulative_authoritative_over_the_turn_stream() -> None:
    """A compressing codex run is counted by its client rollout, not its turns.

    The exec stream reports per-turn totals that reset with the session; the
    client rollout carries the per-request record from which the reset-aware
    cumulative is rebuilt.  When the rollout has been read, its cumulative is
    the figure the run reports.
    """
    turns = [
        {"input_tokens": 100, "cached_input_tokens": 20},
        {"input_tokens": 200, "cached_input_tokens": 30},
        {"input_tokens": 300, "cached_input_tokens": 40},
    ]
    events = [{"type": "turn.completed", "usage": usage} for usage in turns]
    stream_sum = sum(
        usage["input_tokens"] + usage["cached_input_tokens"] for usage in turns
    )
    rollout_sum = 1_000_000
    assert rollout_sum != stream_sum

    observation = _observe(
        CODEX,
        events,
        receipt=_measured_receipt(rollout_sum),
    )

    assert observation.throughput["cumulative_input_tokens"] == rollout_sum
    assert observation.throughput["cumulative_cached_input_tokens"] == rollout_sum // 2
    assert observation.throughput["cumulative_input_tokens"] != stream_sum


def test_codex_receipt_generation_span_is_reported_non_null() -> None:
    """A codex run with a read rollout reports its measured model span.

    The span is derived from the rollout's bounded tool time — the mechanism
    the falsifier demands — and is non-null in the throughput block, unlike
    the stream-alone notation that could not separate inference from tool wait.
    """
    observation = _observe(
        CODEX,
        [
            {"type": "thread.started", "thread_id": "thread-receipt"},
            {"type": "turn.completed", "usage": {"output_tokens": 7}},
        ],
        receipt=_measured_receipt(1_000_000),
    )

    throughput = observation.throughput
    assert throughput["generation_seconds"] is not None
    assert throughput["machine_seconds"] is not None
    # the model share of the measured wall stays inside the documented bounds
    share = (
        100
        * throughput["generation_seconds"]
        / (throughput["generation_seconds"] + throughput["machine_seconds"])
    )
    assert 1 < share < 99


def test_unknown_and_measured_throughput_blocks_have_the_same_keys() -> None:
    unknown = _backends.unknown_throughput("not measured")
    measured = _backends.throughput_block(
        generated_tokens=10,
        generation_seconds=2.0,
        elapsed_seconds=4.0,
        peak_input_tokens=50,
        cumulative_input_tokens=120,
        cumulative_cached_input_tokens=70,
        input_budget_tokens=200,
        detail="measured",
    )

    assert measured.keys() == unknown.keys()


def test_input_utilisation_is_unknown_without_a_request_peak() -> None:
    throughput = _backends.throughput_block(
        generated_tokens=10,
        generation_seconds=2.0,
        elapsed_seconds=4.0,
        peak_input_tokens=None,
        cumulative_input_tokens=900,
        cumulative_cached_input_tokens=800,
        input_budget_tokens=200,
        detail="run total only",
    )

    assert throughput["input_utilisation_pct"] is None


def test_input_utilisation_keeps_its_request_peak_meaning() -> None:
    peak = 50
    budget = 200
    throughput = _backends.throughput_block(
        generated_tokens=10,
        generation_seconds=2.0,
        elapsed_seconds=4.0,
        peak_input_tokens=peak,
        cumulative_input_tokens=900,
        cumulative_cached_input_tokens=800,
        input_budget_tokens=budget,
        detail="request peak measured",
    )

    assert throughput["input_utilisation_pct"] == round(100 * peak / budget, 1)
