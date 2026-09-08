"""Derive cumulative token measurements from a crew run stream."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from pathlib import Path
from typing import Any, Literal

# A request above this published threshold is charged at twice the input rate and
# one and a half times the output rate for the whole request, not only the excess.
SURCHARGED_REQUEST_INPUT_TOKENS = 272_000


class MeasurementState(Enum):
    """An explicit state for a quantity the stream cannot measure."""

    UNMEASURED = "unmeasured"


UNMEASURED = MeasurementState.UNMEASURED
MeasuredTokenCount = int | MeasurementState
StreamDialect = Literal["claude", "codex"]


@dataclass(frozen=True)
class StreamTokenUsage:
    """Token totals and request-level measurements available from one stream."""

    cumulative_input_tokens: int
    cumulative_cached_input_tokens: int
    cumulative_output_tokens: int
    maximum_request_input_tokens: MeasuredTokenCount
    surcharged_request_count: MeasuredTokenCount
    turn_count: int
    dialect: StreamDialect


_CLAUDE_INPUT_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_CLAUDE_CACHED_FIELDS = ("cache_read_input_tokens",)
_CLAUDE_OUTPUT_FIELDS = ("output_tokens",)
_CODEX_INPUT_FIELDS = ("input_tokens", "cached_input_tokens")
_CODEX_CACHED_FIELDS = ("cached_input_tokens",)
_CODEX_OUTPUT_FIELDS = ("output_tokens", "reasoning_output_tokens")


def measure_stream_tokens(
    stream_path: str | Path,
) -> StreamTokenUsage | MeasurementState:
    """Measure token usage from a Claude or Codex JSON-lines run stream.

    Missing and unreadable paths, malformed streams, and streams without any
    usage counters return ``UNMEASURED``. A recognized usage record whose
    counters are all zero remains a measured zero.
    """

    claude_messages: dict[str, dict[str, int]] = {}
    claude_aggregate: tuple[dict[str, int], int | None] | None = None
    codex_turns: list[dict[str, int]] = []
    anonymous_message = 0
    try:
        with Path(stream_path).open(encoding="utf-8") as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, Mapping):
                    continue

                if record.get("type") == "assistant":
                    message = record.get("message")
                    if not isinstance(message, Mapping):
                        continue
                    usage = _measured_fields(
                        message.get("usage"),
                        _CLAUDE_INPUT_FIELDS + _CLAUDE_OUTPUT_FIELDS,
                    )
                    if usage is None:
                        continue
                    message_id = message.get("id")
                    if not isinstance(message_id, str) or not message_id:
                        anonymous_message += 1
                        message_id = f"anonymous-{anonymous_message}"
                    maxima = claude_messages.setdefault(message_id, {})
                    # Claude emits an assistant record for each content block.
                    # A record carries the message's usage at that point, so the
                    # maximum for each field preserves the most complete value.
                    for field, value in usage.items():
                        maxima[field] = max(maxima.get(field, 0), value)
                    continue

                if record.get("type") == "result":
                    usage = _measured_fields(
                        record.get("usage"),
                        _CLAUDE_INPUT_FIELDS + _CLAUDE_OUTPUT_FIELDS,
                    )
                    if usage is not None:
                        claude_aggregate = (
                            usage,
                            _nonnegative_int(record.get("num_turns")),
                        )
                    continue

                if record.get("type") == "turn.completed":
                    usage = _measured_fields(
                        record.get("usage"),
                        _CODEX_INPUT_FIELDS + _CODEX_OUTPUT_FIELDS,
                    )
                    if usage is not None:
                        codex_turns.append(usage)
    except OSError:
        return UNMEASURED

    has_claude = bool(claude_messages) or claude_aggregate is not None
    has_codex = bool(codex_turns)
    if has_claude == has_codex:
        return UNMEASURED
    if has_codex:
        return _codex_usage(codex_turns)
    return _claude_usage(claude_messages, claude_aggregate)


def _claude_usage(
    messages: Mapping[str, Mapping[str, int]],
    aggregate: tuple[Mapping[str, int], int | None] | None,
) -> StreamTokenUsage:
    request_inputs = [
        _sum_fields(usage, _CLAUDE_INPUT_FIELDS) for usage in messages.values()
    ]
    if aggregate is None:
        cumulative = _sum_usage(messages.values())
        turn_count = len(messages)
    else:
        # Claude's result.usage is the final run aggregate. Assistant
        # message.usage records supply request-level input, while their opening
        # output counts can be incomplete even after content-block deduplication.
        cumulative, reported_turn_count = aggregate
        turn_count = (
            reported_turn_count if reported_turn_count is not None else len(messages)
        )

    if request_inputs:
        maximum_request_input: MeasuredTokenCount = max(request_inputs)
        surcharge_count: MeasuredTokenCount = sum(
            value > SURCHARGED_REQUEST_INPUT_TOKENS for value in request_inputs
        )
    else:
        maximum_request_input = UNMEASURED
        surcharge_count = UNMEASURED

    return StreamTokenUsage(
        # Cache reads are returned separately but remain included in cumulative
        # input because a metered lane charges total input even though a cache
        # hit can feel free to a caller. Cache creation is fresh work and is
        # therefore included only in the total-input side of this split.
        cumulative_input_tokens=_sum_fields(cumulative, _CLAUDE_INPUT_FIELDS),
        cumulative_cached_input_tokens=_sum_fields(cumulative, _CLAUDE_CACHED_FIELDS),
        cumulative_output_tokens=_sum_fields(cumulative, _CLAUDE_OUTPUT_FIELDS),
        maximum_request_input_tokens=maximum_request_input,
        surcharged_request_count=surcharge_count,
        turn_count=turn_count,
        dialect="claude",
    )


def _codex_usage(turns: list[Mapping[str, int]]) -> StreamTokenUsage:
    cumulative = _sum_usage(turns)
    # A Codex exec stream reports usage once per turn in turn.completed.usage,
    # not once per model request. Surcharge exposure is therefore unmeasurable
    # from the record rather than absent.
    return StreamTokenUsage(
        cumulative_input_tokens=_sum_fields(cumulative, _CODEX_INPUT_FIELDS),
        cumulative_cached_input_tokens=_sum_fields(cumulative, _CODEX_CACHED_FIELDS),
        cumulative_output_tokens=_sum_fields(cumulative, _CODEX_OUTPUT_FIELDS),
        maximum_request_input_tokens=UNMEASURED,
        surcharged_request_count=UNMEASURED,
        turn_count=len(turns),
        dialect="codex",
    )


def _sum_usage(usages: Any) -> dict[str, int]:
    total: dict[str, int] = {}
    for usage in usages:
        for field, value in usage.items():
            total[field] = total.get(field, 0) + value
    return total


def _sum_fields(usage: Mapping[str, int], fields: tuple[str, ...]) -> int:
    return sum(usage.get(field, 0) for field in fields)


def _measured_fields(value: Any, fields: tuple[str, ...]) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    measured = {
        field: token_count
        for field in fields
        if (token_count := _nonnegative_int(value.get(field))) is not None
    }
    return measured or None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Real) or value < 0:
        return None
    return int(value)
