"""Derive cumulative token and time measurements along a run's lineage.

A run's spend is read from its streams and folded across every attempt that
reached the same chain: resumes keep the run id and add a stream file, while
redispatches and lane changes mint a new run id and chain to a recorded root.
A shadow mints a new run id too but is never a further attempt, so its
evidence stays on its own row and is excluded from its primary's total under
one exported predicate (:func:`is_durable`).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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


# ── Lineage accumulation: spend that adds up along the chain ─────────────────


def is_durable(record: Mapping[str, Any]) -> bool:
    """Whether a run's spend belongs in its lineage chain's cumulative total.

    The rule names the exclusion rather than enumerating the durable kinds: a
    run is non-durable only when it is a shadow, so a lineage kind recorded
    after this module was written is durable by default and only an explicit
    shadow is carved out. Durability and measurement quality are separate
    questions — a contaminated row is still durable because its spend merged,
    and a shadow is not durable whether or not it is contaminated.
    """

    lineage = record.get("lineage")
    if not isinstance(lineage, Mapping):
        return True
    return str(lineage.get("kind") or "") != "shadow"


def accumulation_key(record: Mapping[str, Any]) -> str:
    """Run id whose cumulative total carries this run's spend.

    A shadow accumulates onto its own run id even when it carries a chain
    root, so its never-merged evidence stays off its primary's total; the
    shadow branch therefore precedes the chain-root branch, which is the
    ordering the accidental ``root_run_id or run_id`` form gets wrong the day
    a shadow records a root. Every other kind folds into the chain root it
    recorded, falling back to its own run id, which lets a resumed run (no
    new run id) and a self-rooted lane change both accumulate under the id
    that carries them.
    """

    lineage = record.get("lineage")
    run_id = str(record.get("run_id") or "")
    if isinstance(lineage, Mapping):
        if str(lineage.get("kind") or "") == "shadow":
            return run_id
        root = str(lineage.get("root_run_id") or "")
        if root:
            return root
    return run_id


def _resume_stream_order(path: Path) -> tuple[int, str]:
    """Order numbered resume streams by attempt rather than by filename text."""
    match = re.fullmatch(r"resume-(\d+)\.jsonl", path.name)
    return (int(match.group(1)), path.name) if match else (sys.maxsize, path.name)


def run_streams(stream_path: str | Path) -> list[Path]:
    """Every surviving stream one run wrote, in attempt order.

    ``log_path`` is repointed on every resume, so a figure read from the named
    file alone resets each time the run restarts. The original stream and
    every numbered resume share one directory, and the cumulative figure is
    the sum over all of them.
    """

    value = str(stream_path or "")
    if not value.strip():
        return []
    stream = Path(value).expanduser()
    original = (
        stream.parent / "stream.jsonl" if stream.name.startswith("resume-") else stream
    )
    resumes = sorted(stream.parent.glob("resume-*.jsonl"), key=_resume_stream_order)
    return [candidate for candidate in (original, *resumes) if candidate.is_file()]


@dataclass(frozen=True)
class AccumulatedRunSpend:
    """Cumulative token and time figures for one run's whole lineage chain.

    Tokens are the charged totals a meter actually charges: all input
    including cache reads, plus all generated output. Times are folded from
    the chain's rows' recorded throughput blocks. The chain reports how many
    run rows and measured streams it folded, so an unmeasured chain is an
    explicit count of zeros rather than an ambiguity.

    ``durable`` is the exported exclusion predicate applied to the queried
    record: a shadow's own row carries its spend, but that spend is never a
    durable contribution to its primary's total.
    """

    run_id: str
    durable: bool
    folded_run_count: int
    measured_stream_count: int
    unmeasured_stream_count: int
    cumulative_input_tokens: int
    cumulative_cached_input_tokens: int
    cumulative_output_tokens: int
    elapsed_seconds: float | None
    generation_seconds: float | None
    machine_seconds: float | None
    elapsed_from_stamps: bool = False

    @property
    def total_charged_tokens(self) -> int:
        """Input (including cache reads) plus output — what a meter charges."""
        return self.cumulative_input_tokens + self.cumulative_output_tokens


def _find_run(
    runs: Sequence[Mapping[str, Any]], run_id: str
) -> Mapping[str, Any] | None:
    for record in runs:
        if str(record.get("run_id") or "") == run_id:
            return record
    return None


def _resolve_streams_root(streams_root: str | Path | None) -> Path:
    """The directory holding one subdirectory per run id."""
    if streams_root is not None:
        return Path(str(streams_root)).expanduser()
    from reckon.crew.runs import runs_dir

    return runs_dir()


def _row_streams(record: Mapping[str, Any], streams_root: Path) -> list[Path]:
    """Every surviving stream path a row's attempts wrote, without duplicates.

    ``log_path`` is the stream file the launcher wrote and resumes live beside
    it; the run directory is the fallback when a pointer never carried one, so
    a record whose launcher recorded nothing still reaches whatever stream
    survived.
    """

    gathered: list[Path] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    log_value = str(record.get("log_path") or "")
    if log_value.strip():
        candidates.extend(run_streams(log_value))
    run_id = str(record.get("run_id") or "")
    if run_id:
        candidates.extend(run_streams(streams_root / run_id / "stream.jsonl"))
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            gathered.append(candidate)
    return gathered


def _measure_streams(
    paths: Sequence[Path],
) -> tuple[int, int, int, int, int]:
    """Sum one run's streams into token totals and stream counts."""
    total_input = 0
    total_cached = 0
    total_output = 0
    measured = 0
    unmeasured = 0
    for path in paths:
        usage = measure_stream_tokens(path)
        if usage is UNMEASURED or not isinstance(usage, StreamTokenUsage):
            unmeasured += 1
            continue
        measured += 1
        total_input += usage.cumulative_input_tokens
        total_cached += usage.cumulative_cached_input_tokens
        total_output += usage.cumulative_output_tokens
    return total_input, total_cached, total_output, measured, unmeasured


def _stamped_elapsed(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> float | None:
    """Wall seconds elapsed for an in-flight run, from the pointer's own stamps.

    A run that is still working has no terminal record, so its wall measures
    to now. The run's creation stamp is its original dispatch, which is the
    same base the completed row's folded span uses, so a run resumed several
    times counts every attempt's interval rather than only the current one.
    ``attempt_started_at`` is the fallback when no creation stamp was
    recorded, matching the budget watchdog's convention. Missing or malformed
    stamps return ``None``, which keeps an unrunnable record an unmeasured
    one rather than an invented time.
    """

    started = record.get("created_at") or record.get("attempt_started_at")
    if not isinstance(started, str) or not started:
        return None
    try:
        moment = datetime.fromisoformat(str(started))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if now_seconds is None:
        now_seconds = datetime.now(UTC).timestamp()
    return max(0.0, float(now_seconds) - moment.timestamp())


def _fold_time(
    elapsed: float | None,
    generation: float | None,
    record: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
) -> tuple[float | None, float | None, bool]:
    """Fold one row's times into the chain totals, marking their provenance.

    A completed row contributes the span its throughput block recorded. A row
    still in flight has no block, so wall is derived from the pointer's own
    stamps instead, and generation stays unmeasured because no inference span
    exists until a terminal record or a rollout with bounded tool spans does.
    The third return reports whether the contributed wall was derived from
    stamps, which the caller records so a reader can distinguish a derived
    live figure from a span folded from a completed row's block.
    """

    throughput = record.get("throughput")
    if isinstance(throughput, Mapping):
        span = throughput.get("elapsed_seconds")
        if isinstance(span, Real):
            elapsed = (elapsed or 0.0) + float(span)
        model = throughput.get("generation_seconds")
        if isinstance(model, Real):
            generation = (generation or 0.0) + float(model)
        return elapsed, generation, False
    stamped = _stamped_elapsed(record, now_seconds=now_seconds)
    if stamped is None:
        return elapsed, generation, False
    return (elapsed or 0.0) + stamped, generation, True


def accumulate_run_spend(
    runs: Sequence[Mapping[str, Any]],
    run_id: str,
    *,
    streams_root: str | Path | None = None,
    now_seconds: float | None = None,
) -> AccumulatedRunSpend | MeasurementState:
    """Return the whole lineage chain's cumulative spend for one run.

    Rows are folded by accumulation key. A redispatch or lane change folds
    into the chain root it recorded, a resumed run keeps its run id and its
    directory's streams accumulate under it, and a shadow accumulates onto its
    own id so its never-merged evidence stays off its primary's total. Each
    folded row contributes every surviving stream in its run directory,
    because ``log_path`` is repointed on every resume and a figure read from
    it alone would reset at each restart.

    ``UNMEASURED`` names a run id absent from ``runs``; a present run whose
    streams carry no usage counters returns a zero total with its stream
    counts rather than the absence marker. ``streams_root`` is the directory
    holding one subdirectory per run id, defaulting to the crew runs directory
    when omitted. ``now_seconds`` pins the observation the live-run wall
    derives from and exists so a test can fix a fixture's present; omitted,
    the wall measures to the moment of reading.
    """

    record = _find_run(runs, run_id)
    if record is None:
        return UNMEASURED
    root = _resolve_streams_root(streams_root)
    key = accumulation_key(record)
    folded_rows = 0
    total_input = 0
    total_cached = 0
    total_output = 0
    measured_streams = 0
    unmeasured_streams = 0
    elapsed: float | None = None
    generation: float | None = None
    derived_from_stamps = False
    for row in runs:
        # Keying is the exclusion in action: a shadow's accumulation key is
        # its own run id, so it folds only when the query is its own row and
        # never into its primary's total, however the shadow is recorded.
        if accumulation_key(row) != key:
            continue
        folded_rows += 1
        paths = _row_streams(row, root)
        input_total, cached_total, output_total, measured, unmeasured = (
            _measure_streams(paths)
        )
        total_input += input_total
        total_cached += cached_total
        total_output += output_total
        measured_streams += measured
        unmeasured_streams += unmeasured
        elapsed, generation, derived = _fold_time(
            elapsed, generation, row, now_seconds=now_seconds
        )
        derived_from_stamps = derived_from_stamps or derived
    machine = None
    if elapsed is not None and generation is not None and not derived_from_stamps:
        machine = round(elapsed - generation, 3)
    return AccumulatedRunSpend(
        run_id=key,
        durable=is_durable(record),
        folded_run_count=folded_rows,
        measured_stream_count=measured_streams,
        unmeasured_stream_count=unmeasured_streams,
        cumulative_input_tokens=total_input,
        cumulative_cached_input_tokens=total_cached,
        cumulative_output_tokens=total_output,
        elapsed_seconds=elapsed,
        generation_seconds=generation,
        machine_seconds=machine,
        elapsed_from_stamps=derived_from_stamps,
    )
