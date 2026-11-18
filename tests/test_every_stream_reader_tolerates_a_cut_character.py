"""Every reader of a worker stream reads one cut inside a character.

A worker can die, or be read, part way through writing a multibyte UTF-8
character, so a stream's last line may end on a partial character. A strict
decode raised out of the reader: a promotion that derives a run's figures from
its stream then failed on every attempt, because a finished stream never
changes, and the run could not be promoted at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon import capabilities
from reckon.crew import runs


def _cut_stream(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    event = {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}
    stream.write_bytes(json.dumps(event).encode() + b"\n" + "—".encode()[:1])
    return stream


def test_a_runs_tool_steps_read_a_cut_stream(tmp_path: Path) -> None:
    stream = _cut_stream(tmp_path / "run")
    run = {"run_id": "r-cut", "manifest_path": str(stream.parent / "manifest.md")}
    capabilities._tool_steps(run)


def test_the_stream_event_reader_reads_a_cut_stream(tmp_path: Path) -> None:
    stream = _cut_stream(tmp_path / "run")
    events = list(runs.read_stream_events(stream))
    assert events
