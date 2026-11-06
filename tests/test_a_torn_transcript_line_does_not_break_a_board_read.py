"""A transcript torn mid-character is read to its last whole line, not raised out of.

A harness appends to a transcript while a board read is scanning it, so the last
line can end inside a multi-byte character. The shared reader decodes with
replacement, so that line becomes an undecodable JSON line and is skipped as any
malformed line already is, and every whole line before it still yields its
event. Both the forward scan and the board's context-use read are exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import directory as directory_module
from reckon.crew.directory import _context_use, _last_assistant_request
from reckon.crew.dispatch_accounting import _jsonl_events

SESSION = "6f0a2b1c-1111-4a2b-9c3d-abcdef012345"


def _turn(*, identity: str, input_tokens: int, created: int, read: int) -> dict:
    """One assistant request, in the shape the harness writes."""
    return {
        "type": "assistant",
        "message": {
            "id": identity,
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": created,
                "cache_read_input_tokens": read,
                "output_tokens": 120,
            },
        },
    }


def _torn_transcript(path: Path) -> Path:
    """Write whole records followed by a last line torn inside one character."""
    records = [
        {"type": "user", "message": {"content": "start"}},
        _turn(identity="msg-first", input_tokens=5, created=1_000, read=0),
        _turn(identity="msg-last", input_tokens=40, created=60_000, read=190_000),
    ]
    whole = "".join(json.dumps(record) + "\n" for record in records)
    # The two bytes of "h" followed by the first byte of a three-byte character.
    path.write_bytes(whole.encode("utf-8") + b'{"type":"assistant","content":"h\xc3')
    return path


def test_every_whole_line_yields_its_event(tmp_path: Path) -> None:
    path = _torn_transcript(tmp_path / f"{SESSION}.jsonl")

    events = list(_jsonl_events(path))

    assert [event.get("type") for event in events] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert [event["message"].get("id") for event in events[1:]] == [
        "msg-first",
        "msg-last",
    ]


def test_the_board_reads_the_last_whole_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _torn_transcript(tmp_path / f"{SESSION}.jsonl")
    # A tail smaller than the torn record cannot see a whole line, so the read
    # falls through to the forward scan the dispatch accounting owns.
    monkeypatch.setattr(directory_module, "_TRANSCRIPT_TAIL_SIZES", (32,))

    request = _last_assistant_request(path)

    assert request is not None
    assert request["tokens"]["input_tokens"] == 250_040
    use = _context_use(SESSION, path)
    assert use["status"] == "measured"
    assert use["used_tokens"] == 250_040