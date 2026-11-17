"""A stream the next pass asks for is read from where the last read reached.

The parsed-stream cache is bounded by the bytes it holds, and a fleet's live
streams are several times that bound, so a pass that touches every stream
evicts the contents of the stream the next pass asks for first. This cache
keeps a resume cursor per stream it has read, so an evicted stream that only
grew is read from that cursor rather than from its first byte, and a steady
tick pays for what was appended instead of for the population again.

The fixture is sixty streams of two megabytes, more than the cache holds. Every
stream is read once, five hundred bytes are appended to each, and every stream
is read again through both the events cache and the observation reader a tick
uses, with the bytes handed to the parser counted by a recording stand-in. The
second pass must parse at most sixty times the six hundred bytes a stream can
gain; and every stream's summary must equal the reading of a fresh whole-stream
parse, so a resumed read cannot be cheap by forgetting what it had read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon import _backends

STREAM_COUNT = 60
STREAM_BYTES = 2 * 1024 * 1024
APPENDED_BYTES = 500
# What the second pass may parse: the appended record of each stream, with the
# headroom the done-when states.
SECOND_PASS_ALLOWANCE = STREAM_COUNT * 600
BACKEND = {"launch": "cli", "command": "claude"}


class RecordingParse:
    """A stand-in for the cache's parser, counting the bytes it is handed."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.bytes = 0
        self.records = 0

    def __call__(self, lines: Any) -> Any:
        materialised = list(lines)
        self.bytes += sum(len(line.encode("utf-8")) for line in materialised)
        self.records += len(materialised)
        return self._real(materialised)


def _record(index: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"2026-11-17T16:{index // 60:02d}:{index % 60:02d}Z",
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


def _populate(root: Path) -> list[Path]:
    """Sixty streams, each of two megabytes of records."""
    line = _record(0) + "\n"
    text = line * (STREAM_BYTES // len(line))
    paths = []
    for index in range(STREAM_COUNT):
        path = root / f"stream-{index:02d}.jsonl"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def _appended_record() -> str:
    """One small record, so the headroom the allowance states is the record's."""
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:30:00Z",
            "session_id": "00000000-0000-4000-8000-000000000001",
            "message": {
                "id": "msg_appended",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
    )


def _append_a_record(path: Path) -> None:
    """Append exactly five hundred bytes, of which one record is complete."""
    appended = _appended_record() + "\n"
    padding = "y" * (APPENDED_BYTES - len(appended))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(appended + padding)


@pytest.fixture(autouse=True)
def _cold_stream_caches() -> None:
    """Start every case with no stream state carried in from another one."""
    for name in ("_PARSED_STREAMS", "_STREAM_CURSORS", "_STREAM_SUMMARIES"):
        cache = getattr(_backends, name, None)
        if cache is not None:
            cache.clear()


def _observe(path: Path) -> dict[str, Any]:
    return _backends.observe_log(
        backend_name="probe", backend=BACKEND, log_path=path
    ).as_dict()


def _whole_reading(path: Path) -> dict[str, Any]:
    """The same summary read from the stream with no cache in between.

    Only the complete records are read, which is what every streaming reader
    answers for: the append is five hundred bytes of which the last part is a
    record still being written, and a reader that counted that fragment would
    disagree with a summary that correctly left it for the read that finds it
    finished.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines.pop()
    return _backends.observe_stream(
        backend_name="probe",
        backend=BACKEND,
        lines=lines,
    ).as_dict()


def test_a_second_pass_pays_only_for_what_each_stream_gained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _populate(tmp_path)
    population = sum(path.stat().st_size for path in paths)
    assert population > _backends._PARSED_STREAM_LIMIT, (
        "the population must exceed the cache's byte bound",
        population,
        _backends._PARSED_STREAM_LIMIT,
    )

    recorder = RecordingParse(_backends.parse_events)
    monkeypatch.setattr(_backends, "parse_events", recorder)

    # The tick's own reader of each stream, which is what leaves the summary
    # and the resume cursor behind.
    for path in paths:
        _observe(path)
    assert recorder.records >= STREAM_COUNT * 1_000, (
        "the instrument must see the first pass's records",
        recorder.records,
    )

    for path in paths:
        _append_a_record(path)

    recorder.bytes = 0
    recorder.records = 0
    second_pass: list[dict[str, Any]] = []
    for path in paths:
        _backends.cached_stream_events(path)
        second_pass.append(_observe(path))
    assert recorder.bytes <= SECOND_PASS_ALLOWANCE, (
        "the second pass parsed more than the bytes each stream gained",
        recorder.bytes,
        SECOND_PASS_ALLOWANCE,
        recorder.records,
    )
    assert recorder.records >= STREAM_COUNT, (
        "the instrument must see the appended record of every stream",
        recorder.records,
    )

    for path, summary in zip(paths, second_pass, strict=True):
        assert summary == _whole_reading(path), (
            "a summary extended from the append disagrees with a whole read",
            path.name,
        )
