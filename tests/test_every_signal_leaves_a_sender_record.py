"""Every signalling path leaves a sender record before it signals.

A kill that cannot be attributed is indistinguishable from a kill that never
happened: the survivor's stream simply stops. These cases drive the shared
signal home against a synthesised live run in a temporary directory and assert
the record it writes names the caller, the reason and the target, so the next
SIGTERM is read from the victim's own run directory.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import routing


class _LiveRun:
    """A synthesised live run: a real process and a run directory to record in."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,
        )
        # The recorded start time the identity check refuses a mismatch against.
        deadline = time.monotonic() + 5.0
        self.pid_start_time: str | None = None
        while time.monotonic() < deadline:
            self.pid_start_time = routing._process_start_time(self.process.pid)
            if self.pid_start_time:
                break
            time.sleep(0.02)

    def records(self) -> list[dict]:
        path = self.run_dir / routing.SENDER_RECORD_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)


@pytest.fixture
def live_run(tmp_path: Path):
    run = _LiveRun(tmp_path / "runs" / "r-sender-record")
    try:
        yield run
    finally:
        run.stop()


def test_signal_process_group_writes_a_sender_record_naming_the_reason(
    live_run: _LiveRun,
) -> None:
    """The named check: driving _signal_process_group leaves its caller's record.

    The signal is not delivered — both os.kill and os.killpg are recorded
    instead — so the child survives to be inspected, and the record is the
    only evidence the call produced.
    """
    delivered: list[tuple[str, int]] = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(routing.os, "kill", lambda pid, sig: delivered.append(("kill", pid)))
    monkey.setattr(
        routing.os, "killpg", lambda pgid, sig: delivered.append(("killpg", pgid))
    )
    try:
        routing._signal_process_group(
            live_run.process.pid,
            live_run.pid_start_time,
            reason="delivered-review-outlived-grace",
            run_dir=live_run.run_dir,
        )
    finally:
        monkey.undo()

    records = live_run.records()
    assert len(records) == 1
    record = records[0]
    assert record["reason"] == "delivered-review-outlived-grace"
    assert record["target_pid"] == live_run.process.pid
    assert record["target_pgid"] == live_run.process.pid
    assert record["sender_pid"] == os.getpid()
    assert record["sender_argv0"] == sys.argv[0]
    assert record["time"]
    assert delivered, "the signal home was reached"


def test_signal_worker_writes_the_record_before_delivering(
    live_run: _LiveRun,
) -> None:
    """The record precedes the signal: it exists even when the signal cannot land.

    Delivery is made to raise the way a vanished process makes it raise, so a
    write ordered after the signal would have nothing on disk to show.
    """

    def vanish(pid, sig):
        raise ProcessLookupError

    monkey = pytest.MonkeyPatch()
    monkey.setattr(routing.os, "kill", vanish)
    monkey.setattr(routing.os, "killpg", vanish)
    try:
        result = routing.signal_worker(
            live_run.process.pid,
            signal.SIGTERM,
            reason="dispatch-rollback",
            run_dir=live_run.run_dir,
        )
    finally:
        monkey.undo()

    assert result is False
    record = live_run.records()[0]
    assert record["reason"] == "dispatch-rollback"
    assert record["target_pid"] == live_run.process.pid


def test_no_run_directory_leaves_no_record(live_run: _LiveRun) -> None:
    """A sender whose target is not a run names no directory and writes nothing.

    The session-start copy and the standing suite are signalled through the
    same home, and neither has a run directory to write into.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(routing.os, "kill", lambda pid, sig: None)
    monkey.setattr(routing.os, "killpg", lambda pgid, sig: None)
    try:
        routing.signal_worker(live_run.process.pid, signal.SIGTERM)
    finally:
        monkey.undo()

    assert live_run.records() == []


def test_run_directory_of_names_the_run_from_id_or_log_path(tmp_path: Path) -> None:
    """The run directory is resolved from the id first, the stream path second."""
    from reckon.crew.runs import run_dir

    assert routing.run_directory_of({"run_id": "r-abc"}) == run_dir("r-abc")
    assert (
        routing.run_directory_of({"log_path": str(tmp_path / "r-xyz" / "stream.jsonl")})
        == tmp_path / "r-xyz"
    )
    assert routing.run_directory_of({}) is None
    assert routing.run_directory_of(None) is None
