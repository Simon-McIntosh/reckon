"""Each pane row reads only what another project's events file appended.

A session-scoped follower adds the session's figures from every other project's
events file to each row it formats. Walking each file back from its tail on
every row made the row cost grow with the file's history, and a follower busy
formatting rows never reached its review reflex. The newest partition-carrying
record is cached per file and only the appended bytes are read on later rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery_watch


def _line(counts: dict | None, n: int) -> str:
    record = {"run_id": f"r-{n}", "project": "other"}
    if counts is not None:
        record["session_counts"] = counts
    return json.dumps(record) + "\n"


@pytest.fixture()
def walks(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    recovery_watch._NEWEST_SESSION_EVENT_CACHE.clear()
    calls: list[Path] = []
    real = recovery_watch._newest_session_event_walk

    def counting(path: Path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(recovery_watch, "_newest_session_event_walk", counting)
    yield calls
    recovery_watch._NEWEST_SESSION_EVENT_CACHE.clear()


def test_an_unchanged_file_is_walked_once(tmp_path: Path, walks: list[Path]) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_line({"s": {"working": 1}}, 1) + _line(None, 2), encoding="utf-8")

    first = recovery_watch._newest_session_event(path)
    second = recovery_watch._newest_session_event(path)

    assert first is not None and first["run_id"] == "r-1"
    assert second == first
    assert len(walks) == 1


def test_an_appended_partition_record_is_read_without_a_walk(
    tmp_path: Path, walks: list[Path]
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_line({"s": {"working": 1}}, 1), encoding="utf-8")
    recovery_watch._newest_session_event(path)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(_line(None, 2))
    unchanged = recovery_watch._newest_session_event(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_line({"s": {"working": 3}}, 3))
    newest = recovery_watch._newest_session_event(path)

    assert unchanged is not None and unchanged["run_id"] == "r-1"
    assert newest is not None and newest["run_id"] == "r-3"
    assert len(walks) == 1


def test_a_replaced_file_is_walked_again(tmp_path: Path, walks: list[Path]) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_line({"s": {"working": 1}}, 1), encoding="utf-8")
    recovery_watch._newest_session_event(path)

    replacement = tmp_path / "events.new"
    replacement.write_text(_line({"s": {"working": 9}}, 9), encoding="utf-8")
    replacement.replace(path)
    event = recovery_watch._newest_session_event(path)

    assert event is not None and event["run_id"] == "r-9"
    assert len(walks) == 2


def test_a_vanished_file_reads_as_nothing(tmp_path: Path, walks: list[Path]) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_line({"s": {"working": 1}}, 1), encoding="utf-8")
    recovery_watch._newest_session_event(path)
    path.unlink()

    assert recovery_watch._newest_session_event(path) is None
