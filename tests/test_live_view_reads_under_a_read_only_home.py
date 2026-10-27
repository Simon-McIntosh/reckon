"""The live view reads a project under a read-only crew home.

A worker's fence mounts the config home read-only, so any write the live view
takes on its way to an answer is one it cannot take there. This exercises the
read the way a fenced worker makes it: a fixture crew home holding the
project's runs, with the watch directory stripped of write permission before
the call.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import crew, mcp


def _crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _seed_run(project: str, run_id: str) -> None:
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "phase": "working",
            "node": {"id": "worker", "plan": "delivery"},
        },
    )


def _read_only(path: Path) -> Path:
    """Make a directory refuse writes, or fail the test proving it did not."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
    with pytest.raises(PermissionError):
        (path / "probe").write_text("x")
    return path


def _dead_seat_record(project: str) -> dict:
    """A seat record naming a process that has exited."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    start = str(crew._process_start_time(process.pid))
    process.terminate()
    process.wait(timeout=5)
    return {
        "project": project,
        "pid": process.pid,
        "pid_start_time": start,
        "host": socket.gethostname(),
    }


def test_live_view_reads_when_the_watch_directory_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "synthetic-project"
    _crew_home(tmp_path, monkeypatch)
    _seed_run(project, "live-run")
    _seed_run(project, "second-run")
    seat = crew.watch_lock_path(project)
    watch = _read_only(seat.parent)
    try:
        body = mcp._crew(project, view="live")
    finally:
        os.chmod(watch, stat.S_IRWXU)

    assert body["ok"] is True
    assert sorted(str(row["run_id"]) for row in body["runs"]) == [
        "live-run",
        "second-run",
    ]
    assert body["watcher"]["pointer_count"] == 2
    assert body["watcher"]["status"] == "unwatched"
    # A read creates nothing on the way to its answer, so the locked path a
    # fenced worker cannot touch is still absent after the read.
    assert not seat.exists()


def test_read_only_home_reports_a_stale_seat_without_repairing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "synthetic-project"
    _crew_home(tmp_path, monkeypatch)
    _seed_run(project, "live-run")
    seat = crew.watch_lock_path(project)
    seat.parent.mkdir(parents=True, exist_ok=True)
    stale = _dead_seat_record(project)
    with seat.open("a+b") as handle:
        crew._write_watch_record(handle, stale)
    # A read-only mount refuses the file as well as the directory: a directory
    # without write permission still allows an existing file in it to be
    # rewritten, which is not what the fence does.
    os.chmod(seat, stat.S_IRUSR)

    watch = _read_only(seat.parent)
    try:
        body = mcp._crew(project, view="live")
    finally:
        os.chmod(watch, stat.S_IRWXU)

    assert body["ok"] is True
    assert sorted(str(row["run_id"]) for row in body["runs"]) == ["live-run"]
    assert body["watcher"]["status"] == "unwatched"
    # A read-only home cannot repair the record, so the read reports the dead
    # seat rather than raising on a repair it cannot make.
    with seat.open("rb") as handle:
        assert crew._read_watch_record(handle) == stale
