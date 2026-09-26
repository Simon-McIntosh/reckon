"""The classifier does not call a live run unwritten while it is still writing.

A dispatch writes no manifest of its own, and a worker writes its manifest by
hand rather than atomically, so between launch and its first written verdict a
live run has no manifest to read. Reporting that gap as ``unwritten`` the
instant it opens makes a working run flicker ``working → unwritten → working``
within seconds of launch, and a reader following the fleet sees a fault that
never existed. The reader holds the word back in exactly two transient states —
inside the launch window, and while a manifest looks mid-rewrite — and keeps it
for what it names: a run past those windows with no verdict written.

Everything the classifier resolves through the environment is pointed at
``tmp_path`` so the real crew directories are untouched, and the process the
pointer names is a live child of this test, so liveness is proven rather than
stored.
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
IN_PROGRESS_MANIFEST = "node: n\nstatus: in-progress\ncommits: none\n"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def live_pid():
    """A live process owned by this test, so its pid answers the process table."""
    child = subprocess.Popen(["sleep", "300"])
    try:
        yield child.pid
    finally:
        child.terminate()
        child.wait()


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int,
    created_at: float,
    manifest: Path,
    stream: Path,
) -> dict:
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "40m"},
        "phase": "working",
        "created_at": recovery.datetime.fromtimestamp(
            created_at, tz=recovery.timezone.utc
        ).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": True,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": socket.gethostname(),
    }


def _manifest_path(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id / "manifest.md"


def _stream_path(tmp_path: Path, run_id: str) -> Path:
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    return stream


def _write(path: Path, text: str, *, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def test_a_fresh_run_with_only_its_orientation_write_is_not_unwritten(
    tmp_path: Path, crew_home: Path, live_pid: int
) -> None:
    # The worker's first write records where it is working and no status; read
    # seconds later it is a run in motion, not a delivery with a missing word.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-fresh")
    _write(manifest, ORIENTATION_ONLY, age_seconds=5)
    pointer = _pointer(
        tmp_path,
        "r-fresh",
        pid=live_pid,
        created_at=now - 30,
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-fresh"),
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["recovery_classification"] != "unwritten"
    assert row["recovery_classification"] == "running"
    assert row["classification"] == "running"


def test_a_manifest_caught_mid_rewrite_keeps_the_previous_reading(
    tmp_path: Path, crew_home: Path, live_pid: int
) -> None:
    # The previous read saw a working manifest; the next read catches the file
    # between a rewrite's truncate and write, four bytes and a second old. That
    # is the absence of a verdict in transit, so the reading does not move.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-rewrite")
    _write(manifest, IN_PROGRESS_MANIFEST, age_seconds=600)
    pointer = _pointer(
        tmp_path,
        "r-rewrite",
        pid=live_pid,
        created_at=now - 900,
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-rewrite"),
    )
    before = recovery.classify_pointer(pointer, now_seconds=now)
    assert before["recovery_classification"] != "unwritten"

    _write(manifest, "stat", age_seconds=1)

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["recovery_classification"] != "unwritten"
    assert row["recovery_classification"] == "running"


def test_a_shrunk_manifest_is_mid_rewrite_after_its_mtime_settles(
    tmp_path: Path, crew_home: Path, live_pid: int
) -> None:
    # The second signature of a truncating rewrite: the file is smaller than the
    # last read saw it, even though its mtime has moved past the short window. A
    # reader that only watched the clock would call this unwritten.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-shrunk")
    _write(manifest, IN_PROGRESS_MANIFEST, age_seconds=600)
    pointer = _pointer(
        tmp_path,
        "r-shrunk",
        pid=live_pid,
        created_at=now - 900,
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-shrunk"),
    )
    recovery.classify_pointer(pointer, now_seconds=now)

    _write(manifest, "stat", age_seconds=600)

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["recovery_classification"] != "unwritten"


def test_a_run_past_the_grace_with_no_verdict_still_reads_unwritten(
    tmp_path: Path, crew_home: Path, live_pid: int
) -> None:
    # The guard keeps the word's purpose: a live run well past its launch window
    # whose manifest carries no status is genuinely unwritten, so the reading is
    # not switched off, only deferred.
    now = time.time()
    manifest = _manifest_path(tmp_path, "r-stale")
    _write(manifest, ORIENTATION_ONLY, age_seconds=600)
    pointer = _pointer(
        tmp_path,
        "r-stale",
        pid=live_pid,
        created_at=now - 600,
        manifest=manifest,
        stream=_stream_path(tmp_path, "r-stale"),
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["recovery_classification"] == "unwritten"
