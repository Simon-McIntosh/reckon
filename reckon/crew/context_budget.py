"""Read context-compaction evidence without relabelling aggregate usage.

The evidence census behind this parser examined 3,428 crew streams and
4,876,735 records. Codex emitted 2,779 thread.started records, 2,779
turn.started records, 389,216 item started or completed records, 2,664
turn.completed records, and 32 turn.failed records; none carried compaction
or per-request context data. Claude emitted 62,890 assistant records, 579
result records, and several system subtypes. Its compaction-specific records
were 21 status rows announcing an attempt, 12 successes, 8 failures, and 12
compact_boundary rows across five streams. Only those boundaries carried
the reached size in compact_metadata.pre_tokens.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from os import PathLike
from pathlib import Path
from typing import Any

UNMEASURED = "unmeasured"

_CONTEXT_REFUSAL_TEXT = "Prompt is too long"
_COMPACTION_RECORD_TYPE = "system/compact_boundary"
_CLAUDE_RECORD_TYPES = {
    "assistant",
    "rate_limit_event",
    "result",
    "system",
    "tool_progress",
    "user",
}


def _unmeasured_result() -> dict[str, str]:
    return {
        "compaction_count": UNMEASURED,
        "largest_context_tokens": UNMEASURED,
        "compaction_record_type": UNMEASURED,
        "context_record_type": UNMEASURED,
    }


def measure_context_budget(stream_path: str | PathLike[str]) -> dict[str, int | str]:
    """Return compaction count and largest explicit pre-compaction size.

    A recognised Claude stream with no boundary has a measured count of zero
    and an unmeasured largest context. Other dialects and unreadable streams
    fail closed to explicit unmeasured markers.
    """
    try:
        lines = Path(stream_path).open(encoding="utf-8")  # noqa: SIM115
    except (OSError, TypeError, ValueError):
        return _unmeasured_result()

    compaction_count = 0
    context_sizes: list[int] = []
    saw_supported_dialect = False

    with lines:
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, Mapping):
                continue

            event_type = event.get("type")
            if event_type in _CLAUDE_RECORD_TYPES:
                saw_supported_dialect = True
            if event_type != "system" or event.get("subtype") != "compact_boundary":
                continue

            compaction_count += 1
            metadata = event.get("compact_metadata")
            pre_tokens = (
                metadata.get("pre_tokens") if isinstance(metadata, Mapping) else None
            )
            if (
                isinstance(pre_tokens, int)
                and not isinstance(pre_tokens, bool)
                and pre_tokens >= 0
            ):
                context_sizes.append(pre_tokens)

    if not saw_supported_dialect:
        return _unmeasured_result()
    return {
        "compaction_count": compaction_count,
        "largest_context_tokens": (max(context_sizes) if context_sizes else UNMEASURED),
        "compaction_record_type": _COMPACTION_RECORD_TYPE,
        "context_record_type": (
            _COMPACTION_RECORD_TYPE if context_sizes else UNMEASURED
        ),
    }


@dataclass(frozen=True, slots=True)
class RefusalRecord:
    """One run whose stream ended with the endpoint refusing its prompt.

    ``estimated_input_tokens`` is the smallest input the endpoint could have
    been holding when it refused: the context window the run's own result
    announced for the model it was served on.  It is an estimate because the
    refusal carries no usage of its own — the request that was rejected never
    produced a token count — so the announced window is the recorded figure
    that bounds it from below.  Absent an announced window the estimate is
    unmeasured rather than zero, and such a run cannot move the boundary.
    """

    run_id: str
    model: str | None
    records: int
    estimated_input_tokens: int | None
    estimated_input_tokens_basis: str
    announced_context_window: int | None
    max_output_tokens: int | None


def _refusal_record(path: Path) -> RefusalRecord | None:
    last_result: Mapping[str, Any] | None = None
    records = 0
    try:
        stream = path.open(encoding="utf-8")
    except OSError:
        return None
    with stream:
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, Mapping):
                continue
            records += 1
            if event.get("type") == "result":
                last_result = event
    if last_result is None or not last_result.get("is_error"):
        return None
    detail = str(last_result.get("result") or "")
    if _CONTEXT_REFUSAL_TEXT not in detail:
        return None

    model: str | None = None
    window: int | None = None
    output_reservation: int | None = None
    model_usage = last_result.get("modelUsage")
    if isinstance(model_usage, Mapping):
        for name, entry in model_usage.items():
            if not isinstance(entry, Mapping):
                continue
            candidate = entry.get("contextWindow")
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and (window is None or candidate < window)
            ):
                window = candidate
                model = str(name)
                reservation = entry.get("maxOutputTokens")
                output_reservation = (
                    reservation
                    if isinstance(reservation, int)
                    and not isinstance(reservation, bool)
                    else None
                )

    return RefusalRecord(
        run_id=path.parent.name,
        model=model,
        records=records,
        estimated_input_tokens=window,
        estimated_input_tokens_basis=(
            "announced-context-window" if window is not None else UNMEASURED
        ),
        announced_context_window=window,
        max_output_tokens=output_reservation,
    )


def refusal_census(
    runs_dir: str | PathLike[str], *, model: str | None = None
) -> dict[str, Any]:
    """Census recorded runs whose stream ends by refusing the prompt.

    A boundary belongs to one lane, so the census can be narrowed to the runs a
    lane's own model served: pooling every model would take the minimum across
    lanes and refuse each of them at the tightest one's limit, which is a
    different figure from any lane's own.  Runs excluded by that narrowing are
    reported rather than dropped, so a caller can see what the figure was taken
    over.

    The effective refusal boundary is the lowest input estimate among the runs
    counted: a node whose own estimate reaches that figure is in the band the
    endpoint has already been observed to reject, so it is the tightest boundary
    the recordings support.  An empty census yields an absent boundary rather
    than zero, so a lane with no recorded refusal keeps its declared window
    instead of refusing everything.
    """
    root = Path(runs_dir)
    records = [
        record
        for record in (
            _refusal_record(path) for path in sorted(root.glob("*/stream.jsonl"))
        )
        if record is not None
    ]
    counted = [record for record in records if model is None or record.model == model]
    excluded = [record for record in records if record not in counted]
    estimates = [
        record.estimated_input_tokens
        for record in counted
        if record.estimated_input_tokens is not None
    ]
    return {
        "runs_dir": str(root),
        "model": model,
        "refused_runs": [asdict(record) for record in counted],
        "excluded_runs": [asdict(record) for record in excluded],
        "effective_boundary_tokens": min(estimates) if estimates else UNMEASURED,
        "boundary_basis": (
            "lowest-estimate-among-refused-runs" if estimates else "no-recorded-refusal"
        ),
    }
