"""A classification reads only the stream lines appended since its last scan.

A stream only grows by appending, so a scan that reached its end answers every
later pass from the bytes appended since; a stream that was replaced or
truncated is scanned again from the start. Before this, every classification of
every live run decoded the whole of that run's newest stream, which the decode
census measured as the largest remaining per-tick read.

The reading is counted rather than described: a recording stand-in for
``json.loads`` counts the calls made from the counted modules and keeps the text
of each, so a pass that re-read an unchanged stream shows up as decodes and a
pass that read nothing new shows up as none. The two per-tick pointer-field
passes are counted the same way, by the module the decode happens in rather than
by the function that asked for it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery_wait, recovery_watch, station, station_store
from reckon.crew.run_paths import crew_home

THIS_HOST = socket.gethostname()
# A fixed clock and a fixed stream mtime: the verdict may carry either, so
# nothing but the stream's own contents is allowed to move between two passes.
NOW_SECONDS = 1_800_000_000.0
STREAM_WRITTEN_AT = NOW_SECONDS - 1
MOMENT = datetime.fromtimestamp(NOW_SECONDS, tz=UTC)
# The orientation write is the first thing a dispatch writes: a manifest with no
# verdict yet, whose run reads as not-yet-acting until an assistant record lands.
ORIENTATION_WRITE = "orientation_worktree: /w\n"
RUNS = 30
LINES = 2_000

RECOVERY_WAIT = "reckon.crew.recovery_wait"
STATION = "reckon.crew.station"
STATION_STORE = "reckon.crew.station_store"


class Decodes:
    """A recording stand-in for ``json.loads``, counted by the calling module."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, modules: Iterable[str]):
        self.modules = frozenset(modules)
        self.calls: list[tuple[str, str]] = []
        self._real = json.loads
        monkeypatch.setattr(json, "loads", self)

    def __call__(self, text: Any = "", *args: Any, **kwargs: Any) -> Any:
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller in self.modules:
            self.calls.append((str(caller), _text_of(text)))
        return self._real(text, *args, **kwargs)

    def reset(self) -> None:
        self.calls.clear()

    def texts(self) -> set[str]:
        return {text for _, text in self.calls}


def _text_of(text: Any) -> str:
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    return str(text)


def _write_stream(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(record) for record in records) + "\n"
    path.write_text(body, encoding="utf-8")
    os.utime(path, (STREAM_WRITTEN_AT, STREAM_WRITTEN_AT))


def _assistant_record() -> str:
    record = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "working"}]},
    }
    return json.dumps(record)


def _filler_lines(count: int) -> list[dict[str, Any]]:
    """Records a worker's stream holds before it acts: no assistant turn in them."""
    return [{"type": "system", "subtype": "init", "session_id": "s"}] + [
        {"type": "progress", "n": index} for index in range(count - 1)
    ]


def _build_run(home: Path, run_id: str, *, lines: int = LINES) -> dict[str, Any]:
    """One live run with an orientation-write manifest and a stream of filler."""
    stream = home / "runs" / run_id / "stream.jsonl"
    _write_stream(stream, _filler_lines(lines))
    manifest = home / "runs" / run_id / "manifest.md"
    manifest.write_text(ORIENTATION_WRITE, encoding="utf-8")
    return {
        "run_id": run_id,
        "project": "fixture",
        "session": "s",
        "phase": "working",
        "launcher_host": THIS_HOST,
        "pid": os.getpid(),
        "created_at": "2026-10-24T12:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "worktree": str(home / "trees" / run_id),
        "repo": str(home / "trees" / run_id),
    }


@pytest.fixture()
def decodes(monkeypatch: pytest.MonkeyPatch) -> Decodes:
    return Decodes(monkeypatch, (RECOVERY_WAIT, STATION, STATION_STORE))


@pytest.fixture()
def fleet(home: Path) -> list[dict[str, Any]]:
    """Thirty live runs, each with two thousand lines already in its stream."""
    assert home.is_dir()
    base = crew_home()
    return [_build_run(base, f"r-{index:03d}") for index in range(RUNS)]


def _classify(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        recovery_watch.classify_pointer(record, now_seconds=NOW_SECONDS)
        for record in records
    ]


def _prime(path: Path) -> None:
    """Ask a stream everything a classification asks, so its scan has reached its end.

    A stream that has been read by every reader is the state a live fleet is in
    after its first pass: the assistant scan has swept the file and the two tail
    readers have answered from its end, so the next pass has nothing left to
    read but the bytes appended since.
    """
    recovery_wait._stream_holds_assistant_record(path)
    recovery_wait._last_tool_call(path)
    recovery_wait._newest_stream_last_record_type({"log_path": str(path)})


def test_a_second_pass_over_unchanged_streams_decodes_nothing(
    fleet: list[dict[str, Any]], decodes: Decodes
) -> None:
    """Two passes over the same fleet: the first reads the streams, the second none."""
    first = _classify(fleet)
    # The instrument is shown to see something known present before an absence
    # is reported: the first pass decoded the streams
    # (the second pass's own count at the revision before this change is
    # 60,030 decodes, which is what the negative control reproduces).
    assert len(decodes.calls) >= RUNS * LINES // 2
    decodes.reset()

    second = _classify(fleet)

    assert decodes.calls == [], "a pass over unchanged streams read them again"
    assert second == first, "the reused scan answered a different verdict"


def test_one_appended_assistant_line_is_the_only_record_decoded(
    fleet: list[dict[str, Any]], decodes: Decodes
) -> None:
    """One appended line is decoded by the next pass, and it flips that run's verdict."""
    first = _classify(fleet)
    target = fleet[7]
    stream = Path(target["log_path"])
    _prime(stream)
    appended = _assistant_record()
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(appended + "\n")
    os.utime(stream, (STREAM_WRITTEN_AT, STREAM_WRITTEN_AT))
    decodes.reset()

    second = _classify(fleet)

    assert decodes.texts() == {appended}, decodes.texts()
    assert second[7]["recovery"] != first[7]["recovery"], (
        "the appended assistant record did not change the run's reading"
    )
    assert second[:7] + second[8:] == first[:7] + first[8:]
    assert first[7]["recovery"] == "resume", first[7]["recovery"]


def test_a_truncated_stream_is_scanned_again(
    fleet: list[dict[str, Any]], decodes: Decodes
) -> None:
    """A stream shorter than the bytes a scan read through is read from its start."""
    _classify(fleet)
    target = fleet[3]
    stream = Path(target["log_path"])
    kept = _filler_lines(25)
    _write_stream(stream, kept)
    decodes.reset()

    _classify(fleet)

    decoded = decodes.texts()
    assert {json.dumps(record) for record in kept} <= decoded
    assert len(decoded) >= len(kept)


def test_a_record_still_being_written_waits_for_its_bytes(
    home: Path, decodes: Decodes
) -> None:
    """A stream's last newline is the scan's offset until its record completes."""
    assert home.is_dir()
    record = _build_run(crew_home(), "r-partial", lines=12)
    stream = Path(record["log_path"])
    _prime(stream)
    pending = _assistant_record()
    half = len(pending) // 2
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(pending[:half])
    os.utime(stream, (STREAM_WRITTEN_AT, STREAM_WRITTEN_AT))

    first = _classify([record])[0]

    scan = recovery_wait._STREAM_SCANS[str(stream)]
    complete_end = stream.stat().st_size - half
    assert scan.assistant_offset == complete_end, (
        "the scan consumed the bytes of a record that has not landed"
    )
    assert first["recovery"] == "resume", first["recovery"]

    with stream.open("a", encoding="utf-8") as handle:
        handle.write(pending[half:] + "\n")
    os.utime(stream, (STREAM_WRITTEN_AT, STREAM_WRITTEN_AT))
    decodes.reset()

    second = _classify([record])[0]

    assert decodes.texts() == {pending}, decodes.texts()
    assert second["recovery"] != first["recovery"], second["recovery"]


CODEX_STREAM = [
    {"type": "session_configured", "backend": "codex", "dialect": "codex"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "working"}},
]


def test_two_configured_ticks_over_an_unchanged_store_decode_no_document(
    home: Path, tmp_path: Path, decodes: Decodes
) -> None:
    """A codex run's stored columns survive ingest, and a quiet tick decodes nothing."""
    assert home.is_dir()
    base = crew_home()
    run_id = "r-codex"
    stream = base / "runs" / run_id / "stream.jsonl"
    _write_stream(stream, CODEX_STREAM)
    pointer = base / "live" / f"{run_id}.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": "fixture",
                "session": "s",
                "phase": "working",
                "launcher_host": "another-node",
                "backend": "codex",
                "dialect": "codex",
                "log_path": str(stream),
                "manifest_path": str(base / "runs" / run_id / "manifest.md"),
            }
        ),
        encoding="utf-8",
    )

    with station_store.StationStore(tmp_path / "station") as store:
        store.tick(home=base)
        stored = (
            store.connect()
            .execute(
                "SELECT backend, dialect FROM pointers WHERE run_id = ?", (run_id,)
            )
            .fetchall()
        )
        assert [(row["backend"], row["dialect"]) for row in stored] == [
            ("codex", "codex")
        ]

        node = station.Station(store, home=base, projects=dict)
        decodes.reset()
        node._tick(now=MOMENT)
        decodes.reset()

        node._tick(now=MOMENT)

    assert decodes.calls == [], "a quiet tick decoded a document none of it changed"
