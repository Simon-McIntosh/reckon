"""The follow read loop delivers a stream record once, whole, or not at all.

A producer appends one JSON record per line and may still be writing the last
one when the follow loop reads. ``readline`` returns the bytes written so far,
with no newline, and admitting that fragment as a whole record delivers the
record truncated while its completion arrives as a second fragment. Snapping
the reader's opening offset to the last newline narrows the window but does not
close it: the loop can still open on a record whose newline has not landed.

The contract now is that a half-written line is held: the read loop leaves its
handle at the line's start, records no offset past it, and returns the record
whole and once when the completion lands. The boundary probe that gives the
reader its opening offset scans backward in bounded chunks, so finding one
newline no longer costs a full-file read.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import runs

PROJECT = "whole-lines-proj"
SESSION = "s1"
RUN_A = "r-whole-a"

ARM_LIFETIME = 0.8


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture(autouse=True)
def _a_live_owner(monkeypatch):
    """Isolate every arming from an owner stamped into the ambient environment.

    A follower stamps its own pid into ``RECKON_FOLLOWER_OWNER`` for the
    processes it launches, and an arming that reads that owner as its own ends
    at its first wait pass. The variable is removed before each case, and the
    resolved owner cleared with it because the identity is cached on the module
    after its first read; the previous cache is restored afterwards so nothing
    leaks into another file in the process.
    """
    previous = runs._RESOLVED_FOLLOWER_OWNER.resolved
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    monkeypatch.setattr(runs._RESOLVED_FOLLOWER_OWNER, "resolved", None)
    yield
    runs._RESOLVED_FOLLOWER_OWNER.resolved = previous


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _record(run_id: str, node: str, *, state: str, previous: str | None) -> dict:
    return {
        "project": PROJECT,
        "event": "transition",
        "run_id": run_id,
        "node": node,
        "session": SESSION,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": _iso(time.time()),
        "legacy": False,
    }


def _arm(*, sleeper, lifetime: float = ARM_LIFETIME) -> list[dict]:
    """Run one arming of the real follower and collect every row it drew."""
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=lifetime,
        sleeper=sleeper,
    )
    return list(generator)


def test_a_record_half_written_across_a_loop_read_arrives_once_whole(home) -> None:
    """A record the producer completes after the loop's first read arrives once.

    The producer writes the first half of a record — the loop is armed while it
    is mid-write, so the loop's first read finds a line that has no newline yet.
    The completion lands on the arming's first wait pass, after that read. The
    record must reach the pane exactly once and parseable; a loop that admitted
    the fragment would deliver nothing here, because the truncated line parses
    as the unattributable legacy form the session-scoped filter drops, and the
    completion would be read as a second fragment.
    """
    _write_pointer(home, RUN_A, "node-a", phase="working")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)

        text = json.dumps(_record(RUN_A, "node-a", state="blocked", previous="working"))
        cut = len(text) // 2
        head, tail = text[:cut], text[cut:]

        with stream_path.open("a", encoding="utf-8") as handle:
            handle.write(head)

        completed: list[bool] = []

        def sleeper(_interval: float) -> None:
            if not completed:
                with stream_path.open("a", encoding="utf-8") as handle:
                    handle.write(tail + "\n")
                completed.append(True)

        rows = _arm(sleeper=sleeper)

    blocked = [
        row
        for row in rows
        if row.get("run_id") == RUN_A and str(row.get("to_state")) == "blocked"
    ]
    assert completed, (
        "the arming never reached a wait pass; the record was never completed"
    )
    assert len(blocked) == 1, (
        f"the half-written record must be delivered once and whole; got {rows!r}"
    )


def test_the_boundary_probe_reads_bounded_bytes_on_a_large_stream(tmp_path) -> None:
    """The boundary probe reads at most one chunk to find the last newline.

    The stream is over a megabyte, so a probe that read the whole file into
    memory for one byte of information would read the whole of it. The last
    record is short, so the last newline sits inside the final 64 KiB chunk and
    the backward scan stops there.
    """
    stream = tmp_path / "stream.jsonl"
    line = json.dumps({"i": "x" * 200}) + "\n"
    with stream.open("w", encoding="utf-8") as handle:
        written = 1_200_000
        while written > 0:
            handle.write(line)
            written -= len(line)
    size = stream.stat().st_size
    assert size > 1_000_000, size

    counter = [0]

    class _CountingHandle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

        def read(self, n: int = -1) -> bytes:
            data = self._handle.read(n)
            counter[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

    class _MeasuredPath:
        """A path whose reads are counted, so the probe's own cost is measured."""

        def __init__(self, path):
            self._path = path

        def stat(self):
            return self._path.stat()

        def open(self, *args, **kwargs):
            return _CountingHandle(self._path.open(*args, **kwargs))

    boundary = cli._follow_boundary(_MeasuredPath(stream))

    assert counter[0] > 0, (
        "the probe read nothing, so this measures a wrapper, not the probe"
    )
    assert counter[0] <= 64 * 1024, (
        f"the probe read {counter[0]} bytes of a {size}-byte stream; the backward "
        "chunk scan must not read past the chunk holding the last newline"
    )
    assert boundary == size, (boundary, size)
