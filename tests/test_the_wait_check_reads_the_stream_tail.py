"""The bounded-wait check reads a worker's stream from its end.

The check runs for every quiet live run on every producer and follower pass, and
a worker's stream is megabytes long by the end of a turn. Reading it whole each
time put tens of megabytes a second per process on shared storage, so the check
must answer from the end of the stream while giving the same answer a full read
gives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery_wait


def _assistant_tool(name: str, command: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": name, "input": {"command": command}}
                ]
            },
        }
    )


def _assistant_text(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _tool_result(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]},
        }
    )


def _write(path: Path, lines: list[str], *, trailing_newline: bool = True) -> Path:
    body = "\n".join(lines)
    path.write_text(body + ("\n" if trailing_newline else ""), encoding="utf-8")
    return path


def _wait(path: Path) -> str | None:
    return recovery_wait._last_bounded_wait({"log_path": str(path)})


def test_a_long_stream_is_answered_from_its_end(tmp_path, monkeypatch) -> None:
    filler = [_assistant_text("x" * 1000) for _ in range(20_000)]
    log = _write(
        tmp_path / "stream.jsonl",
        [
            _assistant_tool("Bash", "pytest -q"),
            *filler,
            _assistant_tool("Bash", "sleep 60"),
        ],
    )
    size = log.stat().st_size
    read = {"bytes": 0}
    original_open = Path.open

    def counting_open(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self != log:
            return handle
        original_read = handle.read

        def read_counted(*read_args):
            data = original_read(*read_args)
            read["bytes"] += len(data)
            return data

        handle.read = read_counted
        return handle

    monkeypatch.setattr(Path, "open", counting_open)
    assert _wait(log) == "a 60s sleep"
    assert size > 10_000_000
    assert read["bytes"] < size // 100


def test_the_newest_tool_call_answers_not_an_earlier_wait(tmp_path) -> None:
    log = _write(
        tmp_path / "stream.jsonl",
        [
            _assistant_tool("Bash", "sleep 300"),
            _tool_result("done"),
            _assistant_tool("Bash", "make"),
        ],
    )
    assert _wait(log) is None


def test_the_last_block_of_a_line_with_two_tool_calls_answers(tmp_path) -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "make"}},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "sleep 45"},
                    },
                ]
            },
        }
    )
    log = _write(tmp_path / "stream.jsonl", [_assistant_text("start"), line])
    assert _wait(log) == "a 45s sleep"


def test_a_partial_last_line_is_skipped(tmp_path) -> None:
    log = _write(
        tmp_path / "stream.jsonl",
        [
            _assistant_tool("TaskOutput", "wait"),
            '{"type": "assistant", "message": {"cont',
        ],
        trailing_newline=False,
    )
    assert _wait(log) == "a TaskOutput task wait"


def test_a_tool_call_on_the_first_line_is_found(tmp_path) -> None:
    log = _write(
        tmp_path / "stream.jsonl",
        [
            _assistant_tool("Bash", "sleep 30"),
            *[_assistant_text("y" * 500) for _ in range(400)],
        ],
    )
    assert _wait(log) == "a 30s sleep"


@pytest.mark.parametrize("length", [70_000, 300_000, 9_000_000])
def test_a_line_longer_than_a_block_is_parsed_whole(tmp_path, length: int) -> None:
    long_command = "echo " + "z" * length + "; sleep 90"
    log = _write(
        tmp_path / "stream.jsonl",
        [
            _assistant_tool("Bash", "make"),
            _assistant_tool("Bash", long_command),
            _tool_result("ok"),
        ],
    )
    assert _wait(log) == "a 90s sleep"


def test_a_stream_with_no_tool_call_reads_as_no_wait(tmp_path) -> None:
    log = _write(
        tmp_path / "stream.jsonl", [_assistant_text("thinking") for _ in range(50)]
    )
    assert _wait(log) is None
