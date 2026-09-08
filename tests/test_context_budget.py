from __future__ import annotations

import json
from pathlib import Path

from reckon.crew.context_budget import UNMEASURED, measure_context_budget

COMPACTION_RECORD_TYPE = "system/compact_boundary"


def _write_stream(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    return path


def _compaction(pre_tokens: int) -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compact_metadata": {"trigger": "auto", "pre_tokens": pre_tokens},
    }


def test_three_compaction_records_are_counted(tmp_path: Path):
    context_sizes = [41_000, 53_000, 47_000]
    records = [
        {"type": "system", "subtype": "init"},
        *[_compaction(size) for size in context_sizes],
    ]

    measured = measure_context_budget(_write_stream(tmp_path / "stream.jsonl", records))

    assert measured["compaction_count"] == len(context_sizes)
    assert measured["largest_context_tokens"] == max(context_sizes)
    assert measured["compaction_record_type"] == COMPACTION_RECORD_TYPE
    assert measured["context_record_type"] == COMPACTION_RECORD_TYPE


def test_a_supported_stream_with_no_compaction_reports_zero(tmp_path: Path):
    stream = _write_stream(
        tmp_path / "stream.jsonl",
        [
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success"},
        ],
    )

    measured = measure_context_budget(stream)

    assert measured["compaction_count"] == len([])
    assert measured["largest_context_tokens"] == UNMEASURED
    assert measured["compaction_record_type"] == COMPACTION_RECORD_TYPE
    assert measured["context_record_type"] == UNMEASURED


def test_an_unrecognised_stream_is_unmeasured_and_does_not_raise(tmp_path: Path):
    stream = _write_stream(
        tmp_path / "stream.jsonl",
        [{"type": "unrelated", "usage": {"input_tokens": 900_000}}],
    )

    measured = measure_context_budget(stream)

    assert measured == {
        "compaction_count": UNMEASURED,
        "largest_context_tokens": UNMEASURED,
        "compaction_record_type": UNMEASURED,
        "context_record_type": UNMEASURED,
    }


def test_a_missing_stream_is_unmeasured_and_does_not_raise(tmp_path: Path):
    measured = measure_context_budget(tmp_path / "missing.jsonl")

    assert measured["compaction_count"] == UNMEASURED
    assert measured["largest_context_tokens"] == UNMEASURED


def test_an_unreadable_stream_is_unmeasured_and_does_not_raise(tmp_path: Path):
    stream = _write_stream(
        tmp_path / "unreadable.jsonl",
        [{"type": "system", "subtype": "init"}],
    )
    stream.chmod(0)
    try:
        measured = measure_context_budget(stream)
    finally:
        stream.chmod(0o600)

    assert measured["compaction_count"] == UNMEASURED
    assert measured["largest_context_tokens"] == UNMEASURED


def test_context_is_the_largest_boundary_not_the_aggregate_total(tmp_path: Path):
    context_sizes = [37_000, 61_000, 49_000]
    aggregate_total = sum(context_sizes) * len(context_sizes)
    records = [
        {"type": "system", "subtype": "init"},
        *[_compaction(size) for size in context_sizes],
        {
            "type": "turn.completed",
            "usage": {"input_tokens": aggregate_total},
        },
    ]

    measured = measure_context_budget(_write_stream(tmp_path / "stream.jsonl", records))

    assert measured["largest_context_tokens"] == max(context_sizes)
    assert measured["largest_context_tokens"] != aggregate_total
