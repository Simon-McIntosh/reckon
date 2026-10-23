"""A finished run's completion stamp is resolved once while its streams stand.

Every producer pass asks every live run whose worker has gone for its stream
completion stamp, and resolving it parses every stream the run wrote. Those
streams no longer grow, so the stamp is kept by the stat identity of each
stream file: an unchanged set is answered without a parse, and a write to any
stream, or a new resume beside it, resolves it afresh.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reckon.crew import promotion, recovery_stream


@pytest.fixture()
def resolved(monkeypatch) -> list[str]:
    """Count full resolutions, each answering with the stream's current size."""
    calls: list[str] = []

    def resolve(record):
        calls.append(record["run_id"])
        size = sum(len(p.read_bytes()) for p in sorted(_streams(record)))
        return SimpleNamespace(completed_at=f"2026-10-23T00:00:{size % 60:02d}Z")

    monkeypatch.setattr(promotion, "_terminal_stream_data", resolve)
    monkeypatch.setattr(recovery_stream, "_COMPLETION_STAMPS", {}, raising=False)
    return calls


def _streams(record):
    from reckon.crew import metering

    return metering.run_streams(record["log_path"])


def _record(tmp_path, name="r-finished"):
    run = tmp_path / name
    run.mkdir()
    stream = run / "stream.jsonl"
    stream.write_text('{"type": "result"}\n')
    return {"run_id": name, "launch": "cli", "backend": "b", "log_path": str(stream)}


def test_unchanged_streams_are_not_parsed_again(tmp_path, resolved):
    record = _record(tmp_path)

    first = recovery_stream._stream_completion_stamp(record)
    for _ in range(5):
        again = recovery_stream._stream_completion_stamp(record)

    assert resolved == ["r-finished"], resolved
    assert again == first


def test_an_appended_stream_is_resolved_afresh(tmp_path, resolved):
    record = _record(tmp_path)
    recovery_stream._stream_completion_stamp(record)

    with open(record["log_path"], "a") as handle:
        handle.write('{"type": "result", "late": true}\n')

    recovery_stream._stream_completion_stamp(record)
    assert len(resolved) == 2, resolved


def test_a_new_resume_stream_is_resolved_afresh(tmp_path, resolved):
    record = _record(tmp_path)
    recovery_stream._stream_completion_stamp(record)

    (tmp_path / "r-finished" / "resume-1.jsonl").write_text('{"type": "result"}\n')

    recovery_stream._stream_completion_stamp(record)
    assert len(resolved) == 2, resolved


def test_an_in_harness_run_has_no_stamp(tmp_path, resolved):
    record = {**_record(tmp_path), "launch": "in-harness"}

    assert recovery_stream._stream_completion_stamp(record) is None
    assert resolved == []
