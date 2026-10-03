"""All live-run readers share one parsed stream across repeated classifications."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from reckon import _backends, crew, mcp_views
from reckon.crew import recovery

MOMENT = 1_800_000_000.0
EVENT_COUNT = 20_000


def _event(index: int) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "session_id": "session-cache",
                "message": {"content": [{"type": "text", "text": str(index)}]},
            }
        )
        + "\n"
    )


def _pointer() -> dict:
    directory = crew.run_dir("r-stream-cache")
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    with stream.open("w", encoding="utf-8") as handle:
        for index in range(EVENT_COUNT):
            handle.write(_event(index))
    manifest = directory / "manifest.md"
    manifest.write_text("---\nnode: cache\nstatus: running\n---\n\nbody\n")
    return {
        "run_id": "r-stream-cache",
        "project": "stream-cache",
        "node": {"id": "cache", "plan": "cache-plan", "time_budget": "20m"},
        "phase": "working",
        "launcher_host": "a-login-node-that-is-not-this-one",
        "process_alive": False,
        "backend": "claude",
        "launch": "cli",
        "command": "claude",
        "created_at": "2026-01-01T00:00:00Z",
        "manifest_path": str(manifest),
        "log_path": str(stream),
    }


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(
        pointer, moment=MOMENT, stall_seconds=3600, cache={}
    )


def test_watcher_snapshots_parse_once_and_extend_from_the_append(
    isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _pointer()
    stream = Path(pointer["log_path"])
    original = _backends.parse_events
    parses: list[int] = []

    def counting(lines):
        materialised = list(lines)
        parses.append(len(materialised))
        return original(materialised)

    monkeypatch.setattr(_backends, "parse_events", counting)
    monkeypatch.setattr(recovery, "_read_classification_memo", lambda _record: {})
    if os.environ.get("RECKON_TEST_DISABLE_STREAM_CACHE") == "1":
        cached = _backends.cached_stream_events

        def uncached(path):
            _backends._PARSED_STREAMS.clear()
            return cached(path)

        monkeypatch.setattr(_backends, "cached_stream_events", uncached)

    def parity(actual: dict) -> None:
        # Force a full parse through the same classifier, preserving the warm
        # entry and excluding the reference arm from the measured parse count.
        stored = _backends._PARSED_STREAMS.copy()
        _backends._PARSED_STREAMS.clear()
        try:
            with monkeypatch.context() as patch:
                patch.setattr(_backends, "parse_events", original)
                expected = _snapshot(pointer)
        finally:
            _backends._PARSED_STREAMS.clear()
            _backends._PARSED_STREAMS.update(stored)
        assert actual == expected

    full_snapshots = 0
    for _ in range(10):
        before = len(parses)
        parity(_snapshot(pointer))
        full_snapshots += any(count >= EVENT_COUNT for count in parses[before:])
    assert full_snapshots <= 1, (full_snapshots, parses)

    parses.clear()
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(_event(EVENT_COUNT))
    parity(_snapshot(pointer))
    assert parses == [1], parses


def test_partition_reuses_all_three_stream_readers(
    isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _pointer()
    stream = Path(pointer["log_path"])
    original = _backends.parse_events
    parses: list[int] = []

    def counting(lines):
        materialised = list(lines)
        parses.append(len(materialised))
        return original(materialised)

    monkeypatch.setattr(_backends, "parse_events", counting)
    monkeypatch.setattr(recovery, "_read_classification_memo", lambda _record: {})
    monkeypatch.setattr(
        mcp_views, "_recorded_live_run_classifications", lambda _project: {}
    )
    _backends._PARSED_STREAMS.clear()
    admission_opens: list[Path] = []
    original_open = Path.open

    def tracking_open(self, *args, **kwargs):
        if (
            self == stream
            and inspect.currentframe().f_back.f_code.co_name == "_admission_refusal"
        ):
            admission_opens.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    for index in range(10):
        in_flight, interrupted = mcp_views.partition_live_runs(
            "stream-cache", [pointer]
        )
        assert in_flight or interrupted
        if index == 0:
            admission_opens.clear()
    assert sum(count >= EVENT_COUNT for count in parses) <= 1, parses
    assert admission_opens == [], "warm admission checks must reuse parsed events"
