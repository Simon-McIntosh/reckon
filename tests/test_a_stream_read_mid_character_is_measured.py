"""A live stream cut inside a multibyte character is measured, not fatal.

A worker writes its stream while the station reads it, so a read can end part
way through a UTF-8 character. The decode error escaped the measurement and
failed every station tick that reached that run, so no tick finished and no
session's obligations were published.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import metering


def _usage_record() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        }
    )


def test_a_stream_ending_inside_a_character_is_measured(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    # The first byte of a three-byte character, as a writer leaves it mid-write.
    stream.write_bytes(_usage_record().encode() + b"\n" + "—".encode()[:1])
    usage = metering.measure_stream_tokens(stream)
    assert usage != metering.UNMEASURED
    assert usage.cumulative_output_tokens == 3


def test_a_whole_stream_is_measured_as_before(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(_usage_record() + "\n", encoding="utf-8")
    assert metering.measure_stream_tokens(stream).cumulative_output_tokens == 3


def _cut_stream(tmp_path: Path) -> Path:
    stream = tmp_path / "cut.jsonl"
    stream.write_bytes(_usage_record().encode() + b"\n" + "—".encode()[:1])
    return stream


def test_the_stream_time_span_reads_a_cut_stream(tmp_path: Path) -> None:
    assert metering._stream_time_span(_cut_stream(tmp_path)) is not None


def test_the_window_reading_reads_a_cut_stream(tmp_path: Path) -> None:
    from reckon.crew import window_reading

    events, reason = window_reading._events_from(_cut_stream(tmp_path))
    assert reason == ""
    assert events
