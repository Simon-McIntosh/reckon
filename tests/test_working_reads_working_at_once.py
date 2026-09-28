"""A working worker reads working within one poll.

The follower's row state for a live run was read from the pointer's stored
phase, which only ``observe`` advances: a worker that had been thinking and
editing for an hour kept the ``starting`` label its launcher wrote, and no
dispatched -> working transition was ever emitted unless a coordinator happened
to fold the run's stream by hand. The run's own newest stream settles it — an
assistant record is the worker's first turn under way — so the row's phase is
derived from that evidence, and the stored label stands only where no stream
holds one.

Each case holds one live pointer and a real process, and drives the ticker a
coordinator watches: the baseline a fresh reader sees, then the transition the
next poll emits once the record lands. The classifier's two phase readings and
the live view's row are asserted beside it, because the pane and the tool must
agree on the word they report for the same run.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import mcp
from reckon.crew import recovery, runs

HOST = socket.gethostname()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every case at a temporary configuration home, and prove it.

    The receipt is the resolved home rather than a fingerprint of the real one:
    this workstation is where a live fleet writes, so a before-and-after
    fingerprint of the operator's home would fail on a peer session's pointer
    rather than on anything this module did. A home inside the case's own
    temporary tree is the stronger statement — nothing here can reach the real
    one, whatever it writes.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert runs.crew_home().is_relative_to(tmp_path)


# The project these stub pointers belong to.
PROJECT = "phase-fixture"

# The window a run is judged against. Wide, because every case here is about a
# run that is working: a narrow window would move the reading for a reason this
# module is not measuring.
STALL_WINDOW = "1h"


class _PublishedNothingError(AssertionError):
    """A watcher reached a tick with nothing to publish, when a row was owed.

    A watcher with nothing to report does not return — it sleeps and reads
    again — so a case that expects a transition reads past the moment the row
    was due and finds this instead. It is an assertion rather than a marker
    because the only thing it can mean is that the fleet said something other
    than what the case named.
    """


class _Publisher:
    """A watcher's sleeper, changing the world between two of its reads.

    The tick is the one moment a case may change what the watcher sees: it
    reads the fleet at the top of each pass and sleeps at the bottom. A pass
    that had a row to publish returns before it ever reaches the sleeper, so
    the count read afterwards is the statement "that many polls saw the world
    as it was and published nothing".
    """

    def __init__(self, actions: Sequence[Callable[[], None]] = ()) -> None:
        self._actions = list(actions)
        self.spent = 0

    def __call__(self, _seconds: float) -> None:
        if self.spent >= len(self._actions):
            raise _PublishedNothingError(
                "the watcher published nothing for this run, so the transition "
                "it was polled for never came"
            )
        action = self._actions[self.spent]
        self.spent += 1
        action()


def _append(path: Path, *records: dict[str, Any]) -> Callable[[], None]:
    """A tick action that appends records to one stream."""

    def append() -> None:
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    return append


def _write_stream(path: Path, records: Sequence[dict[str, Any]]) -> Path:
    """Write one stream of records, in the order an engine writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _start_worker() -> subprocess.Popen:
    """A real process for the pointer's pid, so liveness is the table's word."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _survivors(pids: Sequence[int], *, grace: float = 5.0) -> list[int]:
    """Which of these pids still runs after a bounded grace.

    Bounded, because a kill is not instantaneous in the process table: a single
    reading taken beside the signal reports a survivor that is already gone,
    and an unbounded wait would hang the suite on a defect instead of reporting
    one.
    """
    deadline = time.monotonic() + grace
    while True:
        live = [pid for pid in pids if runs.process_alive(pid) is True]
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(0.02)


def _end_worker(worker: subprocess.Popen) -> None:
    """End the process a case started and fail if any part of it survives."""
    worker.terminate()
    worker.wait()
    stragglers = _survivors([worker.pid])
    assert not stragglers, f"processes this case started survived it: {stragglers}"


def _pointer(
    run_id: str,
    *,
    pid: int,
    stream: Path,
    phase: str = "starting",
) -> dict[str, Any]:
    """One live pointer on the reading host, whose run directory holds its stream.

    The stream lives in the run directory rather than beside the pointer, which
    is where a run's own streams are written and therefore the only place a
    resumed turn's ``resume-*.jsonl`` can be found.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: in-progress\ncommits:\nblockers:\n",
        encoding="utf-8",
    )
    if not stream.exists():
        _write_stream(stream, [{"type": "thread.started"}])
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "s22-coord",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": STALL_WINDOW},
        "phase": phase,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "worker.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": HOST,
        "worktree": str(directory / "tree"),
        "base_sha": "",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _ticker(publisher: Callable[[float], None]) -> Iterator[dict[str, Any]]:
    """The surface a coordinator watches: one baseline, then transitions."""
    return recovery.watch_ticker(
        PROJECT,
        stall_window=STALL_WINDOW,
        poll_interval=0,
        sleeper=publisher,
    )


def _row(pointer: dict[str, Any]) -> dict[str, Any]:
    """The classifier's reading, the one every row and snapshot is built from."""
    return recovery.classify_pointer(pointer, now_seconds=recovery._utc_seconds())


def test_an_assistant_record_in_the_stream_advances_the_row(tmp_path: Path) -> None:
    """The first poll that sees an assistant record emits the transition.

    The run is written as a launcher leaves it: a live pointer still at the
    pre-spawn label, with a stream holding only the engine's opening record.
    The record lands between two polls, and the poll after it is the one that
    must report the worker as working.
    """
    run_id = "r-assistant-in-stream"
    stream = runs.run_dir(run_id) / "stream.jsonl"
    worker = _start_worker()
    try:
        _pointer(run_id, pid=worker.pid, stream=stream)
        publisher = _Publisher([_append(stream, {"type": "assistant"})])
        ticker = _ticker(publisher)
        try:
            baseline = next(ticker)
            transition = next(ticker)
        finally:
            ticker.close()
    finally:
        _end_worker(worker)

    assert baseline["run_id"] == run_id, baseline
    assert baseline["to_state"] == "dispatched", baseline
    assert publisher.spent == 1, publisher.spent
    assert transition["run_id"] == run_id, transition
    assert transition["from_state"] == "dispatched", transition
    assert transition["to_state"] == "working", transition


def test_an_assistant_record_in_a_resume_stream_advances_the_row(
    tmp_path: Path,
) -> None:
    """A turn that resumed writes a newer stream, and that stream decides.

    Same pointer and same stored label as the case above; the only difference
    is which of the run's streams carries the record, so a reader that opened
    ``stream.jsonl`` by name would report this run as still starting.
    """
    run_id = "r-assistant-in-resume"
    directory = runs.run_dir(run_id)
    stream = directory / "stream.jsonl"
    resume = directory / "resume-1.jsonl"
    worker = _start_worker()
    try:
        _pointer(run_id, pid=worker.pid, stream=stream)
        publisher = _Publisher(
            [_append(resume, {"type": "system"}, {"type": "assistant"})]
        )
        ticker = _ticker(publisher)
        try:
            baseline = next(ticker)
            transition = next(ticker)
        finally:
            ticker.close()
    finally:
        _end_worker(worker)

    assert baseline["to_state"] == "dispatched", baseline
    assert resume.stat().st_mtime >= stream.stat().st_mtime, (resume, stream)
    assert transition["from_state"] == "dispatched", transition
    assert transition["to_state"] == "working", transition


def test_a_stream_with_only_a_system_record_keeps_the_stored_label(
    tmp_path: Path,
) -> None:
    """No assistant record means no advance: the launcher's label stands.

    The negative reading beside the two above. A stream that exists is not
    evidence of work — an engine opens one and writes its opening records
    before the model has answered anything — so the run stays in the bucket its
    stored phase names.
    """
    run_id = "r-system-record-only"
    stream = runs.run_dir(run_id) / "stream.jsonl"
    worker = _start_worker()
    try:
        pointer = _pointer(run_id, pid=worker.pid, stream=stream)
        _write_stream(stream, [{"type": "system"}, {"type": "turn.started"}])
        publisher = _Publisher([lambda: None])
        ticker = _ticker(publisher)
        try:
            baseline = next(ticker)
        finally:
            classified = _row(pointer)
            ticker.close()
    finally:
        _end_worker(worker)

    assert baseline["to_state"] == "dispatched", baseline
    assert classified["phase"] == "starting", classified["phase"]
    assert classified["stored_phase"] == "starting", classified["stored_phase"]
    assert classified["effective_phase"] == "starting", classified["effective_phase"]


def test_the_live_view_carries_the_derived_phase(tmp_path: Path) -> None:
    """The tool reads the derivation the pane does, under the key ``phase``.

    The live view's row is the classifier's own projection, so this is the same
    reading the watcher reduces — asserted under the name the row carries it,
    so a view that renamed the field would be renamed here too rather than
    passing on a different key.
    """
    run_id = "r-live-view-phase"
    stream = runs.run_dir(run_id) / "stream.jsonl"
    worker = _start_worker()
    try:
        _pointer(run_id, pid=worker.pid, stream=stream)
        _append(stream, {"type": "assistant"})()
        rows = mcp._crew(PROJECT, view="live")["runs"]
    finally:
        _end_worker(worker)

    assert [row["run_id"] for row in rows] == [run_id], rows
    assert rows[0]["phase"] == "working", rows[0]
    assert rows[0]["stored_phase"] == "starting", rows[0]
