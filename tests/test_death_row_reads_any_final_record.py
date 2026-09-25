"""The death row reads the last complete stream record however large it is.

A worker killed mid-turn is reported on the snapshot that sees its process gone,
and the row names the record the death interrupted, read from the tail of the
run's newest stream. That read takes a window from the end of the file, which
answers correctly only while the final record fits inside it: a last record
taller than the window leaves the window entirely inside that record, no line
in it parses, and the reader answers "no last record". The run then reads as
working with no death clause — the stale row this reporting exists to remove.

The window is a floor rather than a limit: the read extends backwards until the
chunk holds a record boundary, so the last complete record is read whatever its
size. The common case — a last record of a few hundred bytes behind any length
of stream — still costs one window, because a chunk holding a newline holds
every complete record that ends in it.

The sizes here bracket the window. Each case pads the final record's text
field, and the record's own wrapper adds a fixed overhead, so the 64.5 KiB case
is a line that fits inside a 64 KiB window and the 65.5 KiB case is a line
larger than it. The 512 KiB case is far past it.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import pytest

from reckon import crew
from reckon.crew import recovery, runs

STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)
# The phrase only the death reading composes: it is the stream's tail being
# reported, which no other reader of this run states.
STREAM_TAIL_CLAUSE = "the stream's last record is"

# A stream long enough that reading it whole to answer the last record would be
# the defect the accounting case measures. The final record is small.
FILLER_RECORD_COUNT = 60_000
FILLER_RECORD = {"type": "assistant", "message": {"content": []}}


def _start_worker() -> subprocess.Popen:
    """A real process to kill, so liveness is the process table's answer."""
    return subprocess.Popen(["sleep", "120"])


def _kill(worker: subprocess.Popen) -> None:
    worker.kill()
    worker.wait()


def _run_directory(run_id: str) -> Path:
    directory = Path(crew.run_dir(run_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _manifest(directory: Path) -> Path:
    path = directory / "manifest.md"
    path.write_text(
        "node: the-worker\nstatus: in-progress\ncommits:\nblockers:\n",
        encoding="utf-8",
    )
    return path


def _final_record(padding_bytes: int) -> str:
    """The stream's last record, with a text field padded to the case's size."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x" * padding_bytes}]},
        }
    )


def _stream(directory: Path, *, padding_bytes: int) -> Path:
    """A stream whose final record is the padded one."""
    path = directory / "stream.jsonl"
    prefix = [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "assistant", "message": {"content": []}},
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in prefix)
        + _final_record(padding_bytes)
        + "\n",
        encoding="utf-8",
    )
    return path


def _pointer(directory: Path, *, run_id: str, pid: int) -> dict:
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": "the-worker", "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "pid": pid,
        "pid_start_time": runs._process_start_time(pid),
        "launcher_host": socket.gethostname(),
        "process_alive": None,
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "worktree": str(directory / "worktree"),
        "base_sha": "0" * 40,
    }


class _CountingHandle:
    """A stream handle that records how many bytes each read returned."""

    def __init__(self, inner, reads: list[int]) -> None:
        self._inner = inner
        self._reads = reads

    def read(self, *args, **kwargs) -> bytes:
        data = self._inner.read(*args, **kwargs)
        self._reads.append(len(data))
        return data

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> bool:
        return bool(self._inner.__exit__(*exc_info))

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextlib.contextmanager
def _counted_reads(reads: list[int]) -> Iterator[list[int]]:
    """Record every byte read through a Path handle in this block."""
    real_open = Path.open

    def counted_open(self, *args, **kwargs):
        return _CountingHandle(real_open(self, *args, **kwargs), reads)

    Path.open = counted_open
    try:
        yield reads
    finally:
        Path.open = real_open


@pytest.mark.parametrize(
    ("padding_bytes", "fits_window"),
    [
        pytest.param(1 * 1024, True, id="1KiB"),
        pytest.param(64_500, True, id="64.5KiB"),
        pytest.param(65_500, False, id="65.5KiB"),
        pytest.param(512 * 1024, False, id="512KiB"),
    ],
)
def test_a_final_record_reads_back_whatever_size_it_is(
    isolated_reckon_home: Path, padding_bytes: int, fits_window: bool
) -> None:
    """Every size yields the record's type and the death clause built on it.

    The run is killed mid-turn with a non-terminal manifest, so the only fact
    that decides between a death row and a stale working row is the type of the
    record the stream ends with.
    """
    run_id = f"r-final-record-{padding_bytes}"
    directory = _run_directory(run_id)
    worker = _start_worker()
    _stream(directory, padding_bytes=padding_bytes)
    _manifest(directory)
    pointer = _pointer(directory, run_id=run_id, pid=worker.pid)

    try:
        _kill(worker)
        last_record_type = recovery._newest_stream_last_record_type(pointer)
        snapshot = recovery._watch_snapshot(
            pointer, moment=time.time(), stall_seconds=STALL_SECONDS
        )
    finally:
        if worker.poll() is None:
            _kill(worker)

    # The reading the death row rests on, and the assertion a reader taking one
    # fixed window from the end fails for the sizes past that window.
    assert last_record_type == "assistant"

    detail = str(snapshot.get("detail") or "")
    assert snapshot["state"] == "blocked"
    assert STREAM_TAIL_CLAUSE in detail
    assert "assistant" in detail

    # The sizes are a boundary probe rather than four repeats only while they
    # straddle the window. Asserted here so that a window this list no longer
    # brackets reports itself instead of quietly covering one side.
    line = _size_of_final_line(padding_bytes)
    assert (line <= recovery._STREAM_TAIL_BYTES) is fits_window, (
        f"the final field padded to {padding_bytes} B serializes to a {line} B "
        f"line, which no longer sits on the side of the "
        f"{recovery._STREAM_TAIL_BYTES} B window this case declares"
    )


def _size_of_final_line(padding_bytes: int) -> int:
    """The byte length of the padded record's own line."""
    return len(_final_record(padding_bytes).encode("utf-8"))


def test_the_common_small_record_path_reads_one_window_not_the_stream(
    isolated_reckon_home: Path,
) -> None:
    """A small last record behind a long stream costs one window.

    The measured quantity is the bytes the file handle returned, recorded by
    wrapping it, and the same instrument is first shown reading that file whole
    — a counter that records nothing would otherwise report the cheapest
    possible answer as the correct one.
    """
    run_id = "r-long-stream-small-record"
    directory = _run_directory(run_id)
    stream = directory / "stream.jsonl"
    with stream.open("wb") as handle:
        handle.write((json.dumps(FILLER_RECORD) + "\n").encode() * FILLER_RECORD_COUNT)
        handle.write((_final_record(64) + "\n").encode())
    size = stream.stat().st_size
    assert size > 2 * 1024 * 1024
    pointer = _pointer(directory, run_id=run_id, pid=os.getpid())

    whole: list[int] = []
    with _counted_reads(whole):
        data = stream.read_bytes()
    assert len(data) == size
    assert sum(whole) == size, (sum(whole), size)

    reads: list[int] = []
    with _counted_reads(reads):
        last_record_type = recovery._newest_stream_last_record_type(pointer)

    assert last_record_type == "assistant"
    assert 0 < sum(reads) <= recovery._STREAM_TAIL_BYTES, (sum(reads), size)
