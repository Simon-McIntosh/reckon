"""A steady tick reads only the bytes each stream gained, for every reader.

The three production readers that asked the events cache for a whole stream —
the capacity check, the admission scan and ``observe_log`` — each answered from
a whole read whenever the byte-bounded cache had dropped the stream's records.
The population here is forty two-megabyte streams, eighty megabytes against a
thirty-two megabyte bound, so every one of them is evicted between the passes.
One full record set is read, five hundred bytes are appended to each stream,
and the same three readers run again one tick later. The second pass must read
at most two thousand bytes per stream, and every reader's answer must equal
what a fresh whole parse answers at the same tick.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew import dispatch_sessions, recovery_stream

STREAM_COUNT = 40
STREAM_BYTES = 2 * 1024 * 1024
APPEND_BYTES = 500
BYTES_PER_STREAM = 2_000
FIRST_TICK = 12.0
SECOND_TICK = 72.0
BACKEND = {"command": "claude"}


def _line(event: dict) -> str:
    return json.dumps(event) + "\n"


def _filler(index: int, run_id: str) -> str:
    return _line(
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "session_id": run_id,
            "message": {
                "id": f"m-{index}",
                "model": "claude-x",
                "role": "assistant",
                "content": [{"type": "text", "text": "x" * 800}],
            },
        }
    )


def _tail(run_id: str) -> list[str]:
    """The records that make each reader's answer something other than None."""
    return [
        _line(
            {
                "type": "turn.completed",
                "timestamp": "2026-01-01T00:01:00Z",
                "session_id": run_id,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 0,
                },
            }
        ),
        _line(
            {
                "type": "turn.failed",
                "timestamp": "2026-01-01T00:02:00Z",
                "session_id": run_id,
                "error": "Selected model is at capacity: claude-opus-4",
            }
        ),
        _line(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:03:00Z",
                "session_id": run_id,
                "message": {
                    "id": "m-refused",
                    "model": "<synthetic>",
                    "role": "assistant",
                    "content": [{"type": "text", "text": _REFUSAL_TEXT}],
                },
                "error": "invalid_request",
            }
        ),
        _line(_zero_result(run_id, "blocking_limit")),
    ]


_REFUSAL_TEXT = "Prompt is too long"


def _zero_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _zero_result(run_id: str, terminal_reason: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "terminal_reason": terminal_reason,
        "is_error": True,
        "session_id": run_id,
        "result": _REFUSAL_TEXT,
        "usage": _zero_usage(),
    }


def _append_line(run_id: str) -> str:
    """One complete record of exactly ``APPEND_BYTES`` bytes.

    The record spends tokens and reports no duration, so the wall rate the
    stream answers is the caller's own elapsed time rather than a span the
    record carries. A folded stream that answer is re-rated from must keep the
    inputs that rate is computed from, not the rate a previous clock produced.
    """
    event = _zero_result(run_id, "end_turn")
    event["usage"] = {**_zero_usage(), "input_tokens": 800, "output_tokens": 400}
    event["result"] = ""
    padding = APPEND_BYTES - len(_line(event))
    event["result"] = "0" * padding
    line = _line(event)
    assert len(line) == APPEND_BYTES, len(line)
    return line


def _write_stream(path: Path, run_id: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        written = 0
        index = 0
        while written < STREAM_BYTES:
            record = _filler(index, run_id)
            handle.write(record)
            written += len(record)
            index += 1
        for record in _tail(run_id):
            handle.write(record)


def _records() -> list[dict]:
    records = []
    for index in range(STREAM_COUNT):
        run_id = f"r-steady-{index:02d}"
        directory = crew.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        stream = directory / "stream.jsonl"
        _write_stream(stream, run_id)
        (directory / "manifest.md").write_text(
            "---\nnode: steady\nstatus: running\n---\n\nbody\n", encoding="utf-8"
        )
        records.append(
            {
                "run_id": run_id,
                "project": "steady",
                "node": {"id": run_id, "plan": "steady-plan", "time_budget": "20m"},
                "phase": "working",
                "launcher_host": "a-login-node-that-is-not-this-one",
                "process_alive": True,
                "backend": "claude",
                "launch": "cli",
                "command": "claude",
                "agent": {"command": "claude", "model": "claude-opus-4"},
                "created_at": "2026-01-01T00:00:00Z",
                "log_path": str(stream),
            }
        )
    return records


class _CountingHandle:
    """One open stream handle, counting the bytes it hands back."""

    def __init__(self, handle, path: str, reads: dict) -> None:
        self._handle = handle
        self._path = path
        self._reads = reads

    def _count(self, count: int) -> None:
        self._reads[self._path] = self._reads.get(self._path, 0) + count

    def read(self, *args):
        data = self._handle.read(*args)
        self._count(len(data))
        return data

    def readline(self, *args):
        data = self._handle.readline(*args)
        self._count(len(data))
        return data

    def readlines(self, *args):
        data = self._handle.readlines(*args)
        self._count(sum(len(line) for line in data))
        return data

    def __iter__(self):
        for line in self._handle:
            self._count(len(line))
            yield line

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._handle, name)


class _CountingReads:
    """A stand-in that observes every byte read from the streams it was given."""

    def __init__(self, paths: list[Path]) -> None:
        self.paths = {str(path.resolve()) for path in paths}
        self.bytes: dict[str, int] = {}

    def reset(self) -> None:
        self.bytes.clear()

    def total(self) -> int:
        return sum(self.bytes.values())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = Path.open

        def counted(self_path, *args, **kwargs):
            handle = original(self_path, *args, **kwargs)

            resolved = str(Path(self_path).resolve())
            if resolved not in self.paths:
                return handle
            return _CountingHandle(handle, resolved, self.bytes)

        monkeypatch.setattr(Path, "open", counted)


def _tick(records: list[dict], elapsed: float) -> dict[str, tuple]:
    """Run the three steady readers over every stream, as one tick does."""
    answers: dict[str, tuple] = {}
    for record in records:
        refusal = dispatch_sessions._latest_attempt_capacity_refusal(record)
        admission = recovery_stream._admission_refusal(record, memo={})
        observation = _backends.observe_log(
            backend_name="claude",
            backend=BACKEND,
            log_path=record["log_path"],
            elapsed_seconds=elapsed,
        )
        answers[record["run_id"]] = (
            refusal,
            admission,
            observation.as_dict(),
        )
    return answers


def _whole_answers(record: dict, elapsed: float) -> tuple:
    """What a whole read of one stream answers at ``elapsed``.

    Written from the scans the readers performed before they folded: every
    record of the stream is read and the last thing each reader saw is kept.
    The two arms share no state and no reader, so comparing a tick's answer to
    this is a check on the folded path rather than a restatement of it. The
    observation is the backend's own whole-stream read at the same clock.
    """
    lines = Path(record["log_path"]).read_text(encoding="utf-8").splitlines()
    events, _malformed = _backends.parse_events(lines)
    kind = ""
    detail = ""
    refusal_reason = ""
    terminal_reason = ""
    zero_token_error = False
    for event in events:
        name = str(event.get("type") or "")
        if name in dispatch_sessions._TERMINAL_STREAM_KINDS:
            kind = name
            detail = _backends._error_detail(event)
        if name == "assistant":
            message = event.get("message")
            if (
                isinstance(message, dict)
                and str(message.get("model") or "") == recovery_stream._SYNTHETIC_MODEL
                and str(event.get("error") or "") == "invalid_request"
            ):
                text = recovery_stream._assistant_refusal_text(message)
                if text:
                    refusal_reason = text
        elif name == "result":
            terminal_reason = str(event.get("terminal_reason") or "")
            if recovery_stream._result_turned_no_tokens(event):
                zero_token_error = True
    refusal = None
    if kind == "turn.failed" and detail.startswith(
        dispatch_sessions.MODEL_CAPACITY_REFUSAL_PREFIX
    ):
        agent = record.get("agent")
        model = ""
        if isinstance(agent, dict):
            model = str(agent.get("model") or "").strip()
        refusal = model or None
    admission = None
    if refusal_reason and terminal_reason == "blocking_limit" and zero_token_error:
        admission = {"reason": refusal_reason, "terminal_reason": terminal_reason}
    observation = _backends.observe_stream(
        backend_name="claude",
        backend=BACKEND,
        lines=lines,
        elapsed_seconds=elapsed,
    )
    return refusal, admission, observation.as_dict()


def test_a_steady_tick_reads_only_what_the_streams_gained(
    isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = _records()
    streams = [Path(record["log_path"]) for record in records]
    total_stream_bytes = sum(stream.stat().st_size for stream in streams)

    # The population is what the measurement is about: a whole second pass is
    # only distinguishable from a resumed one if the cache cannot hold them.
    assert total_stream_bytes > _backends._PARSED_STREAM_LIMIT, (
        total_stream_bytes,
        _backends._PARSED_STREAM_LIMIT,
    )

    reads = _CountingReads(streams)
    reads.install(monkeypatch)

    first = _tick(records, FIRST_TICK)
    assert reads.total() >= total_stream_bytes, (
        "the instrument must see the first pass's whole reads",
        reads.total(),
        total_stream_bytes,
    )
    # The first tick folds every stream from its whole contents, so its answer
    # is the whole read's answer as well — including the wall rate, which the
    # stream reports no span for and the caller's clock supplies.
    for record in records:
        assert first[record["run_id"]] == _whole_answers(record, FIRST_TICK), record[
            "run_id"
        ]
    for stream in streams:
        with stream.open("a", encoding="utf-8") as handle:
            handle.write(_append_line(stream.parent.name))

    reads.reset()
    second = _tick(records, SECOND_TICK)
    spent = reads.total()
    print(
        f"steady_streams={STREAM_COUNT} population_bytes={total_stream_bytes} "
        f"second_pass_bytes={spent} per_stream={spent // STREAM_COUNT} "
        f"bound={STREAM_COUNT * BYTES_PER_STREAM}"
    )

    assert spent <= STREAM_COUNT * BYTES_PER_STREAM, (spent, reads.bytes)
    assert spent >= STREAM_COUNT * APPEND_BYTES, (
        "the instrument must see the appended bytes",
        spent,
    )

    # Every reader answers what a whole read of the same stream answers at this
    # tick. One reader's answer changed state across the append — the admission
    # marks are the last result's — so a stale fold cannot pass here.
    for record in records:
        assert second[record["run_id"]] == _whole_answers(record, SECOND_TICK), record[
            "run_id"
        ]

    refusal, admission, observation = second[records[0]["run_id"]]
    assert refusal == "claude-opus-4", refusal
    assert admission is None, admission
    assert first[records[0]["run_id"]][1] is not None
    # The appended record reports tokens and no span, so the rate the tick
    # answers must be the caller's clock's, not the one a fold was built under.
    rate = observation["throughput"]["wall_tokens_per_second"]
    assert rate is not None, observation["throughput"]


def test_a_rewrite_past_the_opening_is_still_read_again(
    isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream rewritten in place is read again, even where it is not the opening.

    The rewrite here is the one a stat result cannot see: the same inode, a
    larger size, and an opening byte-for-byte identical to the version the
    cursor recorded. What it moves is everything after the opening — which is
    what an offset into the stream depends on, and what the boundary sample
    before the offset is read for.
    """
    run_id = "r-rewritten"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"

    opening: list[str] = []
    opening_bytes = 0
    index = 0
    while opening_bytes < _backends._STREAM_HEAD_SAMPLE:
        record = _filler(index, run_id)
        opening.append(record)
        opening_bytes += len(record)
        index += 1
    prefix = "".join(opening)

    record = {
        "run_id": run_id,
        "project": "steady",
        "node": {"id": run_id, "plan": "steady-plan", "time_budget": "20m"},
        "phase": "working",
        "launcher_host": "a-login-node-that-is-not-this-one",
        "process_alive": True,
        "backend": "claude",
        "launch": "cli",
        "command": "claude",
        "agent": {"command": "claude", "model": "claude-opus-4"},
        "created_at": "2026-01-01T00:00:00Z",
        "log_path": str(stream),
    }
    first_body = "".join(_filler(i, run_id) for i in range(index, index + 20))
    first_body += _line(
        {
            "type": "turn.completed",
            "timestamp": "2026-01-01T00:01:00Z",
            "session_id": run_id,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    stream.write_text(prefix + first_body, encoding="utf-8")
    first_size = stream.stat().st_size

    reads = _CountingReads([stream])
    reads.install(monkeypatch)
    before = _backends.observe_log(
        backend_name="claude",
        backend=BACKEND,
        log_path=stream,
        elapsed_seconds=FIRST_TICK,
    )
    assert before.as_dict() == _whole_answers(record, FIRST_TICK)[2]

    # Rewritten in place: the opening is untouched, every record after it is a
    # different record, and the stream ends larger than it was.
    second_body = "".join(_filler(i, run_id) for i in range(500, 540))
    second_body += _line(
        {
            "type": "turn.failed",
            "timestamp": "2026-01-01T00:02:00Z",
            "session_id": run_id,
            "error": "Selected model is at capacity: claude-opus-4",
        }
    )
    stream.write_text(prefix + second_body, encoding="utf-8")
    assert stream.stat().st_size > first_size
    assert stream.read_bytes()[: len(prefix.encode())] == prefix.encode()

    reads.reset()
    after = _backends.observe_log(
        backend_name="claude",
        backend=BACKEND,
        log_path=stream,
        elapsed_seconds=SECOND_TICK,
    )
    whole = _whole_answers(record, SECOND_TICK)
    assert whole[0] == "claude-opus-4", whole[0]
    assert after.as_dict() == whole[2], after.as_dict()
    assert reads.total() >= stream.stat().st_size
