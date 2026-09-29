"""A live worker's orientation stub is not an unwritten manifest.

Every dispatch's first write records where it is working — the worktree, the
base commit, the paths it may write — and nothing else, because there is
nothing else to report yet. A manifest holding only those keys and no status
line is therefore a run a minute into its turn, not a delivery that failed to
declare a verdict. The classifier already reads such a run as working from the
evidence its phase derivation uses: the run's own newest stream carries an
assistant record, so the worker has been thinking and editing rather than
sitting silent. Reading the recovery word from the absent status instead
contradicts that phase, and a reader following the fleet is told to resume a
run whose stream is still growing.

Only the run's motion buys the reading. The second case is the same stub on a
process the table cannot name, and it keeps exactly the classification the stub
carried before: the two cases differ in nothing but the process table's answer,
so the pair measures the liveness gate rather than the file.

The worker in each case is a child of this test, terminated and reaped by the
test that started it, and every directory the classifier reaches resolves under
``tmp_path``, so the real fleet's home is never read or written.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs

HOST = socket.gethostname()

# The manifest a dispatch's first write leaves: the three orientation keys in
# the order the dispatch asks for them, and no status line.
ORIENTATION_STUB = (
    "orientation_worktree: /tmp/nowhere\n"
    "orientation_base_sha: 00000000000000000000000000000000000000ac\n"
    'orientation_write_paths: ["reckon/crew/recovery.py"]\n'
)

# The worker's own first turn as its stream records it. The record's presence
# is the evidence, not its text: this is the newest stream's assistant record
# the phase derivation reads as work in motion.
ASSISTANT_RECORD = (
    '{"type":"assistant","message":{"role":"assistant",'
    '"content":[{"type":"text","text":"orienting in the worktree"}]}}'
)

# The fixture is dispatched well before it is read, so the reader's grace for a
# run that has just appeared cannot be what settles its verdict.
DISPATCH_AGE_SECONDS = 600


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)


@contextmanager
def _started_worker() -> Iterator[tuple[int, str]]:
    """A worker process this test starts, ended and reaped by this test.

    The receipt is taken after the wait, with the classifier's own instrument:
    the pid the fixture named must no longer answer the process table, so no
    process a case started can outlive it and be read by a later one.
    """
    child = subprocess.Popen(["sleep", "300"])
    started = recovery._process_start_time(child.pid)
    assert started is not None, child.pid
    try:
        yield child.pid, started
    finally:
        with contextlib.suppress(ProcessLookupError):
            child.terminate()
        with contextlib.suppress(ChildProcessError):
            child.wait()
        assert child.poll() is not None, child.pid
        assert recovery._process_start_time(child.pid) != started, (
            f"the worker {child.pid} this case started still answers the process table"
        )


def _stub_run(tmp_path: Path, run_id: str, *, pid: int, pid_start_time: str) -> dict:
    """One pointer carrying a worker's orientation stub and one assistant turn."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text(ASSISTANT_RECORD + "\n", encoding="utf-8")
    manifest = directory / "manifest.md"
    manifest.write_text(ORIENTATION_STUB, encoding="utf-8")
    # The stub is what the worker wrote on its way in; the stream is the work
    # itself, written since. Only the stub's age matters: it is well past a
    # file that just moved, so nothing here rests on the moment of the read.
    stamp = time.time() - DISPATCH_AGE_SECONDS
    os.utime(manifest, (stamp, stamp))
    dispatched = datetime.now(tz=UTC) - timedelta(seconds=DISPATCH_AGE_SECONDS)
    return {
        "run_id": run_id,
        "project": "fixture-project",
        "session": "fixture-session",
        "node": {"id": run_id, "plan": "fixture-plan", "time_budget": "40m"},
        "role": "implement",
        # The launcher's label before the worker was observed: the phase is
        # read from the run's own evidence, which is where the stream's turn
        # has to answer for it.
        "phase": "dispatching",
        "created_at": dispatched.isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "worktree": str(tmp_path / f"{run_id}-worktree"),
        "process_alive": True,
        "pid": pid,
        "pid_start_time": pid_start_time,
        "launcher_host": HOST,
    }


def _write_pointer(tmp_path: Path, run_id: str, pointer: dict) -> None:
    """Publish the fixture as the live pointer a reader is shown."""
    path = crew.pointer_path(run_id)
    assert path.is_relative_to(tmp_path), path
    crew._write_json(path, pointer)


def test_a_live_orientation_stub_reads_the_work_it_is_doing(tmp_path: Path) -> None:
    """The stub beside a working process is work in motion, not a missing word."""
    run_id = "r-orientation-stub-live"
    with _started_worker() as (pid, started):
        pointer = _stub_run(tmp_path, run_id, pid=pid, pid_start_time=started)
        _write_pointer(tmp_path, run_id, pointer)
        row = recovery.classify_pointer(pointer, now_seconds=time.time())

        assert row["process_alive"] is True, row["process_alive"]

    assert row["recovery_classification"] != "unwritten", (
        "a live worker whose manifest holds only its orientation write must not "
        f"read unwritten; the row reads {row['recovery_classification']!r} "
        f"(classification {row['classification']!r}, phase {row['phase']!r}, "
        f"detail {row['detail']!r})"
    )
    assert row["recovery"] == "observe", row["recovery"]
    assert row["classification"] == "running", row["classification"]
    assert row["phase"] == "working", row["phase"]


def test_the_same_stub_beside_a_gone_worker_keeps_its_reading(tmp_path: Path) -> None:
    """The dead control: nothing but the process table's answer is changed.

    Liveness beside the run's own stream is what buys the reading, so with the
    process ended before the read the stub classifies exactly as it did before:
    the run is live-shaped but not proven working, and the recovery word still
    names the missing verdict.
    """
    run_id = "r-orientation-stub-dead"
    with _started_worker() as (pid, started):
        pass
    pointer = _stub_run(tmp_path, run_id, pid=pid, pid_start_time=started)
    _write_pointer(tmp_path, run_id, pointer)

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["process_alive"] is False, row["process_alive"]
    assert row["classification"] == "running", row["classification"]
    assert row["recovery_classification"] == "unwritten", row["recovery_classification"]
    assert row["recovery"] == "resume", row["recovery"]
