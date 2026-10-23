"""A run's terminal lane signal parses its stream once while the stream stands.

Every producer pass asks every run whose worker has gone for the cause its last
terminal result names, and finding that result parses the whole stream. An
unchanged stream has the same newest result, so it is kept by the stream's stat
identity and parsed again only after a write.
"""

from __future__ import annotations

import json

import pytest

from reckon.crew import recovery_memo


@pytest.fixture()
def opened(monkeypatch) -> list[str]:
    monkeypatch.setattr(recovery_memo, "_NEWEST_RESULTS", {}, raising=False)
    names: list[str] = []
    real = recovery_memo.Path.open

    def counting(self, *args, **kwargs):
        names.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(recovery_memo.Path, "open", counting)
    return names


def _stream(tmp_path, *events):
    run = tmp_path / "r-ended"
    run.mkdir(exist_ok=True)
    stream = run / "stream.jsonl"
    stream.write_text("".join(json.dumps(event) + "\n" for event in events))
    return stream


def _record(stream):
    return {"run_id": "r-ended", "log_path": str(stream), "model": "m"}


RATE_LIMITED = {
    "type": "result",
    "is_error": True,
    "timestamp": "2026-10-23T05:00:00Z",
    "result": "API Error: rate limit reached for this model",
}


def test_an_unchanged_stream_is_parsed_once(tmp_path, opened):
    stream = _stream(tmp_path, {"type": "assistant"}, RATE_LIMITED)

    first = recovery_memo._terminal_lane_signal(_record(stream))
    after_first = opened.count("stream.jsonl")
    for _ in range(5):
        again = recovery_memo._terminal_lane_signal(_record(stream))

    assert after_first >= 1
    assert opened.count("stream.jsonl") == after_first, opened
    assert again == first
    assert first[0] is not None and first[0].get("kind") == "rate-limit", first


def test_a_written_stream_is_read_again(tmp_path, opened):
    stream = _stream(tmp_path, RATE_LIMITED)
    recovery_memo._terminal_lane_signal(_record(stream))

    with stream.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "timestamp": "2026-10-23T05:01:00Z",
                }
            )
            + "\n"
        )

    cause, stamp = recovery_memo._terminal_lane_signal(_record(stream))
    assert (cause, stamp) == (None, "2026-10-23T05:01:00Z")
