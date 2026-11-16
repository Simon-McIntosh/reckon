"""A stream shows work whichever engine wrote it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery_wait


def _stream(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "stream.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "system"}, {"type": "assistant"}],
        [
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message"}},
        ],
        [
            {"type": "session", "id": "s"},
            {"type": "turn_start"},
            {"type": "message_end", "message": {"role": "assistant"}},
        ],
        [
            {"type": "session", "id": "s"},
            {"type": "turn_end", "message": {"role": "assistant"}},
        ],
    ],
    ids=["bare-assistant", "thread-item", "settled-message", "ended-turn"],
)
def test_a_record_of_the_model_answering_shows_work(tmp_path: Path, records):
    assert recovery_wait._stream_holds_assistant_record(_stream(tmp_path, *records))


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "system"}],
        [{"type": "thread.started"}, {"type": "turn.started"}],
        [{"type": "item.started", "item": {"type": "command_execution"}}],
        [{"type": "item.completed"}],
        [
            {"type": "session", "id": "s"},
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "message_end", "message": {"role": "toolResult"}},
        ],
        [{"type": "message_end"}],
    ],
    ids=[
        "launch-only",
        "thread-opened",
        "item-started",
        "item-without-body",
        "tool-result-only",
        "message-without-body",
    ],
)
def test_a_stream_with_no_answer_from_the_model_shows_none(tmp_path: Path, records):
    assert not recovery_wait._stream_holds_assistant_record(_stream(tmp_path, *records))
