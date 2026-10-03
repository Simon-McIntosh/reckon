"""A resumed stream observation counts malformed lines over the whole stream.

``observe_log`` extends a previous observation from a byte cursor instead of
re-reading the stream, so the records a resumed read parses are only those
written since the cursor. The malformed-line count describes the stream the
observation is of, so it is carried across the resume with the fold rather
than measured from the appended segment alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon import _backends

CODEX = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}

_BEFORE = "malformed before the resume offset"
_AFTER = "malformed after the resume offset"


def _record(index: int) -> str:
    return json.dumps({"type": "item.completed", "item": {"index": index}})


def _write_lines(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _resume_of(path: Path, observation: _backends.Observation) -> dict:
    """The resume a caller builds from a previous observation of this stream."""
    offset = int(observation.stream_state["offset"])
    return {
        "offset": offset,
        "state": observation.stream_state,
        "head": _backends.stream_head_fingerprint(path, offset=offset),
    }


def test_resumed_observation_counts_malformed_lines_whole(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    _write_lines(stream, [_record(1), _BEFORE, _record(2), _record(3)])

    first = _backends.observe_log(backend_name="b", backend=CODEX, log_path=stream)
    assert first.malformed_lines == 1, (
        "the fixture must carry a malformed line before the cursor"
    )

    _write_lines(stream, [_record(4), _AFTER, _record(5)])
    appended = stream.stat().st_size - int(first.stream_state["offset"])

    _backends.take_parsed_stream_bytes()
    resumed = _backends.observe_log(
        backend_name="b",
        backend=CODEX,
        log_path=stream,
        resume=_resume_of(stream, first),
    )
    assert _backends.take_parsed_stream_bytes() == appended, (
        "the resumed read must parse only the records written past the cursor"
    )

    whole = _backends.observe_log(backend_name="b", backend=CODEX, log_path=stream)

    assert whole.malformed_lines == 2, (
        "the fixture must carry a malformed line after the cursor too"
    )
    assert resumed.malformed_lines == whole.malformed_lines, (
        "a resumed observation must count malformed lines over the whole stream"
    )
