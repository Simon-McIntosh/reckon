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
from os import PathLike
from pathlib import Path

UNMEASURED = "unmeasured"

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
