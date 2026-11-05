"""A session-scoped live read writes nothing.

A worker's fence mounts the crew home read-only, so every write the live view
takes on its way to an answer is one a fenced worker cannot take. Three of them
survive a project-wide read and only appear once the read names a session: the
follower registration is opened for append so its advisory lock can be taken, a
lease whose holder this host can read dead is unlinked as the read passes. The
first two arms below exercise the read the way the fence makes it — every file
and directory in the home stripped of write permission, including the seat,
which is opened read-only and cannot even be read.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reckon import crew, mcp
from reckon.crew import runs


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A crew home of this test's own, with no pointer to the real one."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, project: str, run_id: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "phase": "working",
            "created_at": runs._utc_now(),
            "log_path": str(log),
        },
    )


def _dead_pid() -> tuple[int, str]:
    """A pid this host has issued and the kernel has since taken back."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    start = str(crew._process_start_time(process.pid))
    process.terminate()
    process.wait(timeout=10)
    return process.pid, start


def _dead_lease(project: str, pid: int) -> Path:
    """A lease naming a local holder that has exited, renewed just now."""
    lease = runs.watch_host_lease(project).path
    lease.parent.mkdir(parents=True, exist_ok=True)
    runs._write_json(
        lease,
        {"host": socket.gethostname(), "pid": pid, "job": "", "token": "dead"},
    )
    return lease


def _dead_registration(project: str, session: str, pid: int, start: str) -> Path:
    path = runs.follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    runs._write_json(
        path,
        {
            "project": project,
            "session": session,
            "pid": pid,
            "pid_start_time": start,
            "delivery": "stream",
            "started_at": runs._utc_now(),
        },
    )
    return path


def _dead_seat(project: str, pid: int, start: str) -> Path:
    path = runs.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        crew._write_watch_record(
            handle,
            {
                "project": project,
                "pid": pid,
                "pid_start_time": start,
                "host": socket.gethostname(),
            },
        )
    return path


def _freeze(root: Path) -> None:
    """Strip write permission from every entry, files before directories."""
    for path in sorted(root.rglob("*"), key=lambda row: len(row.parts), reverse=True):
        if path.is_dir():
            os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
        else:
            os.chmod(path, stat.S_IRUSR)
    os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)


def _thaw(root: Path) -> None:
    os.chmod(root, stat.S_IRWXU)
    for path in root.rglob("*"):
        if path.is_dir():
            os.chmod(path, stat.S_IRWXU)
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _tree(root: Path) -> dict[str, tuple[int, Any]]:
    """Every entry under ``root``: its mode and its bytes, or why unreadable."""
    rows: dict[str, tuple[int, Any]] = {}
    for path in sorted(root.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        key = str(path.relative_to(root)) + ("/" if path.is_dir() else "")
        if path.is_dir():
            rows[key] = (mode, None)
            continue
        try:
            rows[key] = (mode, path.read_bytes())
        except PermissionError:
            rows[key] = (mode, "unreadable")
    return rows


def test_a_session_scoped_read_writes_nothing_under_a_read_only_home(
    home: Path,
) -> None:
    project = "synthetic-project"
    _write_pointer(home, project, "live-run")
    _write_pointer(home, project, "second-run")
    pid, start = _dead_pid()
    registration = _dead_registration(project, "theirs", pid, start)
    lease = _dead_lease(project, pid)
    seat = _dead_seat(project, pid, start)
    _freeze(home)
    # The seat is the one entry a fenced worker cannot even read: an unreadable
    # seat is an unknown holder, not an absent one.
    os.chmod(seat, 0o000)
    before = _tree(home)
    try:
        body = mcp._crew(project, view="live", session="theirs")
        # Photographed under the fence, before the modes are relaxed to let
        # this test clean up: a mode is part of what a write would move.
        after = _tree(home)
    finally:
        _thaw(home)

    assert "error" not in body, body.get("error")
    assert body["ok"] is True
    assert sorted(str(row["run_id"]) for row in body["runs"]) == [
        "live-run",
        "second-run",
    ]
    assert body["watcher"]["seat_held"] is None, "an unreadable seat is unknown"
    assert body["watcher"]["seat_readable"] is False
    # Both other branches are reached rather than skipped: the dead lease is
    # reported stale from a home the read may not write, and the registration is
    # read and counted as released rather than listed as delivering.
    assert body["watcher"]["stale_lease"] is not None, "the dead lease was read"
    assert body["watcher"]["followers_released"] == 1, "the registration was read"
    assert after == before, "the read left the home byte-identical"
    # Nothing was unlinked on the way past: the dead lease and the dead
    # registration are both still where the read found them.
    assert lease.exists()
    assert registration.exists()


def test_a_read_reports_a_dead_lease_instead_of_unlinking_it(home: Path) -> None:
    """A writable home makes the read able to unlink, which it must not do.

    The lease is a claim another process will make, and a read that removes it
    is a reader deciding ownership. Reporting it stale is the same fact without
    the write: the file stays, and the caller learns that the holder it names
    is gone from this host's table.
    """
    project = "synthetic-project"
    _write_pointer(home, project, "live-run")
    pid, start = _dead_pid()
    lease = _dead_lease(project, pid)
    _dead_seat(project, pid, start)

    body = mcp._crew(project, view="live", session="theirs")

    assert body["ok"] is True
    assert body["watcher"]["stale_lease"] == {
        "host": socket.gethostname(),
        "pid": pid,
        "job": "",
    }
    assert lease.exists(), "a read reports the stale lease and leaves it in place"
    assert lease.stat().st_size > 0, "and does not truncate it either"
