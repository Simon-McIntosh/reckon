"""A manifest read mid-write on another node is not drawn halted.

A run's manifest is rewritten in place, so a reader can catch it between the
truncate and the write and find a body that no parser can read. When the run
was launched on another host, the pointer's pid names a process this reader's
process table cannot look up, so liveness reads unknown rather than gone. The
classifier's hoisted ``process_gone`` verdict counted that unknown as gone, so
such a run reached the ``unreadable`` arm while its worker was writing, and a
run that read ``complete`` seconds later was drawn halted in the interval.

The run's own stream answers what the process table cannot: a worker still
writing has an assistant turn in its newest stream, and one written inside the
stall window the stall clock already applies is a run in motion — so the
unknown liveness is read the way the orientation-write exemption reads it, and
the run is classified from that life rather than from its half-written file. A
process observed to have ended is still gone whatever its stream holds, and a
stream past the stall window shows nothing in motion, so both still read
``unreadable``.

Everything the classifier resolves through the environment is pointed at
``tmp_path`` so the real crew directories are untouched.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from reckon.crew import recovery

# A body with no ``key: value`` line at all, of the shape a manifest presents
# between a rewrite's truncate and its write: prose and headings, no field a
# reader can resolve, so no status and no delivery to judge.
MID_WRITE_BODY = "# manifest\n\nstill writing the status line\n"
ASSISTANT_RECORD = '{"type":"assistant","message":"working"}\n'
OTHER_HOST = "another-login-node"


@pytest.fixture()
def gone_pid():
    """A pid this test owned and has ended, so the process table answers gone."""
    child = subprocess.Popen(["sleep", "300"])
    child.terminate()
    child.wait()
    return child.pid


def _pointer(
    run_id: str,
    *,
    manifest: Path,
    stream: Path,
    created_at: float,
    launcher_host: str,
    pid: int | None = None,
    process_alive: bool | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "40m"},
        "phase": "working",
        "attempt": 1,
        "created_at": recovery.datetime.fromtimestamp(
            created_at, tz=recovery.timezone.utc
        ).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": process_alive,
        "pid": pid,
        "launcher_host": launcher_host,
    }


def _manifest_path(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id / "manifest.md"


def _stream_path(tmp_path: Path, run_id: str, *, age_seconds: float) -> Path:
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(ASSISTANT_RECORD)
    stamp = time.time() - age_seconds
    os.utime(stream, (stamp, stamp))
    return stream


def _write(path: Path, text: str, *, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def test_a_foreign_run_mid_write_within_the_window_is_not_unreadable(
    tmp_path: Path, home: Path
) -> None:
    # The run was launched on another host, so liveness is unknown here; its
    # manifest is caught mid-write and cannot be parsed; its newest stream
    # carries an assistant turn written inside the stall window. The stream is
    # what the process table cannot supply, so the run reads from that life
    # rather than as an unreadable delivery.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-foreign-mid-write")
    _write(manifest, MID_WRITE_BODY, age_seconds=5)
    pointer = _pointer(
        "r-foreign-mid-write",
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-foreign-mid-write", age_seconds=10),
        created_at=now - 30,
        launcher_host=OTHER_HOST,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] != "unreadable"


def test_a_foreign_run_quiet_past_the_window_reads_unreadable(
    tmp_path: Path, home: Path
) -> None:
    # The same foreign run with the same mid-write manifest, its newest stream
    # last written past the stall window: nothing is in motion, so the missing
    # verdict is genuinely an unreadable delivery.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-foreign-quiet")
    _write(manifest, MID_WRITE_BODY, age_seconds=3000)
    pointer = _pointer(
        "r-foreign-quiet",
        manifest=manifest,
        stream=_stream_path(
            tmp_path,
            "r-foreign-quiet",
            age_seconds=recovery.LOG_STALE_AFTER_SECONDS + 100,
        ),
        created_at=now - 3000,
        launcher_host=OTHER_HOST,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "unreadable"


def test_a_run_whose_process_is_gone_with_a_fresh_stream_reads_unreadable(
    tmp_path: Path, home: Path, gone_pid: int
) -> None:
    # On this host the process table answers: the worker is gone. A fresh
    # stream is not a life, and a process observed to have ended is gone
    # whatever its stream holds, so the mid-write manifest is the unreadable
    # delivery it looks like.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-local-gone")
    _write(manifest, MID_WRITE_BODY, age_seconds=5)
    pointer = _pointer(
        "r-local-gone",
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-local-gone", age_seconds=10),
        created_at=now - 30,
        launcher_host=socket.gethostname(),
        pid=gone_pid,
        process_alive=True,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "unreadable"


def test_the_real_crew_home_is_untouched(tmp_path: Path, home: Path) -> None:
    # Every environment-resolved directory this module reads is under the test's
    # own home, so classifying a foreign run writes nothing to the operator's.
    from tests.conftest import real_store_is_not_a_fixture_target

    real_store_is_not_a_fixture_target(home)
