"""A foreign run's worker record cannot defer that run's terminal manifest.

The crew home is shared across login nodes while the run directory's worker
record carries a pid and no host of its own, so its number is meaningful only on
the machine that issued it. The classifier reads a terminal manifest as
provisional while a worker that could have superseded it is alive: the manifest
may be the verdict a superseded attempt left, and a superseded verdict must not
be read as delivery. That reading takes two facts, a live worker and a launch
after the manifest's last write, and the worker fact is the guard this file is
about. Read from a foreign run's worker record's number on a host that never
issued it, a number that happens to be live here hands the run a life this host
cannot support, and the run's own terminal manifest is then deferred as
superseded — a delivered run read as still running, kept out of promotion.

The one fact that separates the two cases below is the host the run names as its
launcher, so a failure names the gate rather than a neighbour: a run launched
elsewhere is read through its terminal manifest, and the same run launched here
still defers it, because this host issued the worker pid and its probe is a
reading rather than a borrowing.

The worker record must be one this host can probe before the deferral is even
possible, so each case asserts the worker pid is genuinely held here first. The
declared negative control restores the bare process-table probe on the worker
record's pid; the foreign case then borrows that life again and the run reads
running, and the source scan that forbids a bare pid under a record is red at
the same time.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from tests import test_a_live_run_never_reads_dead as liveness

HOST = socket.gethostname()

# A host this machine is not: the pointer names somewhere else, so the worker
# record's number is asked on a machine that never issued it.
FOREIGN_HOST = "another-login-node"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every pointer and run directory this module writes in the temp tree."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))


def _stamp(seconds_before_now: float) -> str:
    """A UTC stamp in the form the supervisor writes a worker launch time."""
    moment = datetime.now(UTC) - timedelta(seconds=seconds_before_now)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_worker_record(run_id: str, pid: int, *, launched_at: str) -> None:
    """The worker record a supervisor leaves for the attempt it started."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": 1,
                "pid": pid,
                "pid_start_time": runs._process_start_time(pid),
                "launched_at": launched_at,
                "backend": "claude",
                "argv": [],
            }
        ),
        encoding="utf-8",
    )


def _run_with_a_terminal_manifest(
    tmp_path: Path,
    run_id: str,
    *,
    launcher_host: str,
    worker_pid: int,
) -> dict:
    """A live pointer whose terminal manifest predates its worker's launch.

    The manifest is a complete verdict written three hundred seconds ago, and
    the worker record's launch is now: the superseded clause's second fact is
    satisfied by construction, so the deferral turns on the worker fact alone.
    """
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=liveness._absent_pid(),
        phase="starting",
        manifest_body=f"node: {run_id}\nstatus: complete\n",
    )
    pointer["launcher_host"] = launcher_host
    manifest = Path(pointer["manifest_path"])
    written = time.time() - 300
    os.utime(manifest, (written, written))
    _write_worker_record(run_id, worker_pid, launched_at=_stamp(seconds_before_now=0))
    return pointer


def test_a_foreign_worker_record_does_not_defer_a_terminal_manifest(
    tmp_path: Path,
) -> None:
    """A number live here is not the worker of a run launched somewhere else."""
    run_id = "r-foreign-terminal-manifest"
    with liveness._live_child() as local_pid:
        pointer = _run_with_a_terminal_manifest(
            tmp_path, run_id, launcher_host=FOREIGN_HOST, worker_pid=local_pid
        )
        # The instrument sees the number really is held here: without this the
        # case could pass because nothing was live to borrow.
        assert runs.process_alive(local_pid) is True, (
            "the worker record's pid is not live here, so the borrowed-life "
            "case is not under test"
        )
        # And the clause's second fact holds, so absent the host gate the
        # deferral would fire and the run would read running.
        assert (
            recovery._worker_launched_after_manifest(
                pointer, Path(pointer["manifest_path"])
            )
            is True
        ), (
            "the worker launch does not postdate the manifest, so no deferral "
            "is under test"
        )

        row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] != "running", row["detail"]
    assert row["classification"] == "scoring", row["detail"]
    assert row["detail"] != "the process is alive", row["detail"]
    assert row["manifest_status"] == "complete", row["manifest_status"]
    assert row["terminal_at"] is not None, row["terminal_at"]
    assert row["process_alive"] is not True, row["process_alive"]


def test_the_same_run_launched_here_still_defers_its_terminal_manifest(
    tmp_path: Path,
) -> None:
    """The control the host gate is measured against.

    The same fixture and the same terminal manifest, with the one fact changed:
    this host launched the run, so the worker record's number was issued here and
    asking the process table about it is an observation. The deferral is then the
    right reading and must still be emitted.
    """
    run_id = "r-local-terminal-manifest"
    with liveness._live_child() as local_pid:
        pointer = _run_with_a_terminal_manifest(
            tmp_path, run_id, launcher_host=HOST, worker_pid=local_pid
        )
        row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "running", row["detail"]
    assert row["detail"] == "the process is alive", row["detail"]
    assert row["manifest_status"] is None, row["manifest_status"]
