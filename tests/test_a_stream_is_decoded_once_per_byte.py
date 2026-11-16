"""A stream is decoded only for the bytes it gained since this process read it.

A terminal run's stream no longer grows, yet every producer pass asked for a
completion stamp, a budget timing and an observation, and each of those read
the whole stream again. The fixture here is a crew home of forty terminal runs
whose streams hold five thousand records each; each pass asks all three
readers for every run. What is measured is ``json.loads`` calls made from
``reckon._backends``: the first pass pays for every record it reads, and a
second pass over unchanged streams must pay nothing — the same stamps, timings
and budgets, answered from the summary the stream's identity keyed. Appending
ten records must decode exactly those ten; truncating a stream must read the
replacement whole.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon import _backends, crew
from reckon.crew import promotion_checks, recovery_stream

RUN_COUNT = 40
LINES = 5_000


class RecordingJson:
    """A stand-in for the ``json`` module that counts the decodes it serves."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls = 0

    def loads(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._real.loads(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _event(index: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"2026-11-15T01:{index // 60:02d}:{index % 60:02d}Z",
            "session_id": "00000000-0000-4000-8000-000000000001",
            "message": {
                "id": f"msg_{index:06d}",
                "model": "claude-sonnet-5",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "x" * 40}],
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 11,
                    "cache_read_input_tokens": 7,
                    "output_tokens": 3,
                },
            },
        }
    )


def _stream_text(start: int, count: int) -> str:
    return "".join(_event(index) + "\n" for index in range(start, start + count))


def _tail_text(count: int) -> str:
    lines = []
    for index in range(count):
        event = json.loads(_event(index))
        event["timestamp"] = f"2027-11-15T01:00:{index:02d}Z"
        lines.append(json.dumps(event) + "\n")
    return "".join(lines)


def _write_stream(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _record(run_id: str, stream: Path) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "launch": "cli",
        "backend": "claude",
        "command": "claude",
        "log_path": str(stream),
        "budget": {},
    }


@pytest.fixture()
def terminal_runs(home: Path) -> list[dict[str, Any]]:
    """Forty terminal runs, each with a five-thousand-record stream."""
    records = []
    for index in range(RUN_COUNT):
        run_id = f"r-terminal-{index:02d}"
        stream = crew.run_dir(run_id) / "stream.jsonl"
        _write_stream(stream, _stream_text(0, LINES))
        records.append(_record(run_id, stream))
    # Every run is terminal: its last record is the harness's result.
    for record in records:
        stream = Path(record["log_path"])
        with stream.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "duration_ms": 1200,
                        "usage": {"input_tokens": 9, "output_tokens": 4},
                    }
                )
                + "\n"
            )
    return records


@pytest.fixture(autouse=True)
def _cold_stream_summaries() -> None:
    _backends._STREAM_SUMMARIES.clear()
    _backends._PARSED_STREAMS.clear()
    recovery_stream._COMPLETION_STAMPS.clear()


def _pass(records: list[dict[str, Any]]) -> list[tuple[Any, Any, Any]]:
    """Read every run through all three stream readers, in one pass."""
    answers = []
    for record in records:
        measures = promotion_checks._terminal_stream_data(record)
        stamp = recovery_stream._stream_completion_stamp(record)
        budget = recovery_stream._stream_budget(record)
        answers.append((measures, stamp, dict(budget) if budget else budget))
    return answers


def test_a_second_pass_over_unchanged_streams_decodes_nothing(
    terminal_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = RecordingJson(json)
    monkeypatch.setattr(_backends, "json", recorder)

    first = _pass(terminal_runs)
    first_calls = recorder.calls
    assert first_calls >= RUN_COUNT * LINES, (
        "the instrument must see the first pass's decodes",
        first_calls,
    )

    recorder.calls = 0
    second = _pass(terminal_runs)

    assert recorder.calls == 0, (
        "an unchanged stream was decoded again",
        recorder.calls,
    )
    assert second == first


def test_ten_appended_records_are_the_only_decodes(
    terminal_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = RecordingJson(json)
    monkeypatch.setattr(_backends, "json", recorder)
    _pass(terminal_runs)

    grown = terminal_runs[7]
    stream = Path(grown["log_path"])
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(_tail_text(10))

    recorder.calls = 0
    _pass(terminal_runs)

    assert recorder.calls == 10, (
        "a grown stream was decoded for more than its new records",
        recorder.calls,
    )


def test_a_truncated_stream_is_decoded_whole(
    terminal_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = RecordingJson(json)
    monkeypatch.setattr(_backends, "json", recorder)
    _pass(terminal_runs)

    truncated = terminal_runs[11]
    stream = Path(truncated["log_path"])
    kept = 1_000
    _write_stream(stream, _stream_text(0, kept))

    recorder.calls = 0
    _pass(terminal_runs)

    assert recorder.calls == kept, (
        "a truncated stream was not read whole",
        recorder.calls,
    )
