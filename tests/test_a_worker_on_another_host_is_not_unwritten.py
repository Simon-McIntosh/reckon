"""A worker writing its first status on another host is not drawn halted.

A run is often launched on one login node while the reader classifying it sits
on another. The pid on the run's pointer names a process that host cannot look
up, so liveness reads unknown rather than alive. Between the orientation write
and the worker's first status there is no verdict in the manifest, and the
classifier would call that gap ``unwritten`` and send a reader to resume work
that is in flight — the same false-halted row a proven-live worker is spared.

The run's own stream answers the question the process table cannot: a worker
still writing has an assistant turn in its newest stream, and one written
inside the stall window the stall clock already applies is a run in motion. A
pointer whose worker is known to be gone still reads ``unwritten``, because a
dead process left its stub behind as the missing verdict it may be.

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

ORIENTATION_ONLY = (
    "orientation_worktree: /tmp/nowhere\n"
    "orientation_base_sha: " + "0" * 40 + "\n"
    'orientation_write_paths: ["x.py"]\n'
)
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
    tmp_path: Path,
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


def test_a_foreign_run_writing_within_the_window_is_not_unwritten(
    tmp_path: Path, home: Path
) -> None:
    # The run was launched on another host, so this reader has no process table
    # to ask; its stream is fresh and carries an assistant turn, so it is in
    # motion and keeps its classification rather than reading unwritten.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-foreign")
    _write(manifest, ORIENTATION_ONLY, age_seconds=30)
    pointer = _pointer(
        tmp_path,
        "r-foreign",
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-foreign", age_seconds=10),
        created_at=now - 30,
        launcher_host=OTHER_HOST,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["recovery_classification"] != "unwritten"
    assert row["recovery_classification"] == row["classification"]


def test_a_foreign_run_quiet_past_the_window_reads_unwritten(
    tmp_path: Path, home: Path
) -> None:
    # The same foreign run, its newest stream last written past the stall window
    # the stall clock applies: nothing is being produced, so the missing verdict
    # is genuinely unwritten.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-foreign-quiet")
    _write(manifest, ORIENTATION_ONLY, age_seconds=3000)
    pointer = _pointer(
        tmp_path,
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

    assert row["recovery_classification"] == "unwritten"


def test_a_local_run_whose_pid_is_gone_reads_unwritten(
    tmp_path: Path, home: Path, gone_pid: int
) -> None:
    # On this host the process table answers: the worker is gone, so the stub it
    # left is the missing verdict it may be, whatever its stream holds.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-local-gone")
    _write(manifest, ORIENTATION_ONLY, age_seconds=30)
    pointer = _pointer(
        tmp_path,
        "r-local-gone",
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-local-gone", age_seconds=10),
        created_at=now - 30,
        launcher_host=socket.gethostname(),
        pid=gone_pid,
        process_alive=True,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["recovery_classification"] == "unwritten"


def test_the_real_crew_home_is_untouched(tmp_path: Path, home: Path) -> None:
    # Every environment-resolved directory this module reads is under the test's
    # own home, so classifying a foreign run writes nothing to the operator's.
    from tests.conftest import real_store_is_not_a_fixture_target

    real_store_is_not_a_fixture_target(home)
