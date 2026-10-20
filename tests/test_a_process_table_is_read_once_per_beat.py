"""The environment of each process is read once, not once per run and beat.

``_processes_owning_run`` finds a run's reparented worker by the environment
mark the launcher wrote into it. Its walk opens ``/proc/<pid>/environ`` for every
process on the node, and it is reached once per beat for every run whose
recorded pid has gone, so the same environments were re-read several times a
second. A process's environment changes only when it execs, which keeps the pid
and the kernel start tick and replaces the program, so a reading is reused until
the pid, its start tick or its executable identity changes — and another user's
process, whose environment cannot be read at all, is skipped by the owner of its
``/proc`` entry without ever being opened. The identity carries the program's
path and the address of its text, because a process that re-execs the same
binary is a different program in the same path.

Every case starts a real, live child carrying a run id so the instrument is
shown to see a process known to be present before any absence is believed. The
declared negative control removes the executable identity from the reused key:
a process whose environment was read between its fork and its exec, and which
then execs carrying the run mark, is found on the next beat — and a key that
holds only the pid and the start tick would miss it.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon.crew import process_signal as ps
from reckon.crew import recovery_liveness, runs
from reckon.crew.process_signal import RUN_ID_ENV, _processes_owning_run

NEGATIVE_CONTROL_MUTATION = (
    "a process whose environment is read between its fork and its exec, then "
    "execs carrying the run mark, is found on the next beat"
)

# Far longer than any single read waits; every case reaps the child it starts.
_CHILD_SLEEP_SECONDS = 300.0
_MARK_WAIT_SECONDS = 10.0


def _unique_run_id() -> str:
    """A run id no other process on this machine carries."""
    return f"r-envindex-{os.getpid()}-{os.urandom(6).hex()}"


def _child_env(run_id: str, home: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RECKON_HOME", None)
    env[RUN_ID_ENV] = run_id
    if home is not None:
        env["RECKON_HOME"] = home
    return env


def _start_child(run_id: str, home: str | None) -> subprocess.Popen:
    """A live child carrying ``run_id`` and, when given, ``RECKON_HOME``."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECONDS})"],
        env=_child_env(run_id, home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(child: subprocess.Popen) -> None:
    """Stop a child and wait for it, escalating only if it will not end."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def _named_for(run_id: str, home: Path) -> list[int]:
    """``_processes_owning_run`` for ``run_id`` against ``home``'s crew."""
    return _processes_owning_run(run_id, str(home / "crew"))


def _wait_for_mark(run_id: str, home: Path, pid: int) -> list[int]:
    """Wait until the scan names ``pid`` for ``home``, or return the last read.

    The positive control every assertion rests on: a child spawned but not yet
    executed is not yet readable through ``/proc``, so a scan that does not name
    it proves nothing.
    """
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    named = _named_for(run_id, home)
    while pid not in named and time.monotonic() < deadline:
        time.sleep(0.05)
        named = _named_for(run_id, home)
    return named


@pytest.fixture(autouse=True)
def empty_index() -> Iterator[None]:
    """Each case starts and ends with an empty environment index."""
    ps._ENVIRON_INDEX.clear()
    yield
    ps._ENVIRON_INDEX.clear()


@pytest.fixture
def environ_opens(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every pid whose environment was opened, in order."""
    opened: list[int] = []
    read = ps._process_environment

    def counted(pid: int) -> dict[str, str] | None:
        opened.append(pid)
        return read(pid)

    monkeypatch.setattr(ps, "_process_environment", counted)
    return opened


def test_the_scan_names_the_live_child(tmp_path: Path) -> None:
    """The positive control: the walk sees a child known to be carrying the mark."""
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, str(home))
    try:
        assert _wait_for_mark(run_id, home, child.pid) == [child.pid]
    finally:
        _reap(child)


def test_a_run_answered_from_the_index_reads_each_process_once(
    tmp_path: Path, environ_opens: list[int]
) -> None:
    """Several runs answered in one beat open one environment per process.

    Three runs whose recorded pids have gone each ask the walk to name their
    worker in the same beat. The child is read on the first ask and answered
    from the index on the other two, so the walk opens its environment once
    rather than once per run.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, str(home))
    try:
        assert child.pid in _wait_for_mark(run_id, home, child.pid)
        opened_on_first_ask = environ_opens.count(child.pid)
        # Two further runs needing the scan in the same beat.
        _named_for(_unique_run_id(), home)
        _named_for(_unique_run_id(), home)
        assert environ_opens.count(child.pid) == opened_on_first_ask
        assert opened_on_first_ask >= 1
    finally:
        _reap(child)


def test_a_second_beat_opens_no_environment_for_an_unchanged_process(
    tmp_path: Path, environ_opens: list[int]
) -> None:
    """A process that has not execd is not re-read on the next beat."""
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, str(home))
    try:
        assert child.pid in _wait_for_mark(run_id, home, child.pid)
        assert child.pid in environ_opens

        environ_opens.clear()
        assert child.pid in _named_for(run_id, home)

        assert child.pid not in environ_opens
    finally:
        _reap(child)


def test_gone_pids_are_dropped_from_the_index(tmp_path: Path) -> None:
    """A pid that has left the table is not kept in the index."""
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, str(home))
    _wait_for_mark(run_id, home, child.pid)
    assert child.pid in ps._ENVIRON_INDEX

    _reap(child)
    _named_for(run_id, home)

    assert child.pid not in ps._ENVIRON_INDEX


def test_a_pid_whose_start_tick_changed_is_read_afresh(tmp_path: Path) -> None:
    """A pid the kernel recycles is read again rather than answered from the index.

    The index holds the pid under a start tick the running process does not
    have, which is what a recycled pid looks like. The next walk must read the
    environment afresh and find the mark the stale reading did not carry.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    child = _start_child(run_id, str(home))
    try:
        assert child.pid in _wait_for_mark(run_id, home, child.pid)
        tick, exe, stack = ps._process_identity(child.pid)
        assert tick is not None
        ps._ENVIRON_INDEX[child.pid] = (
            (f"{tick}-elsewhere", exe, stack),
            {RUN_ID_ENV: "someone-elses-run"},
        )

        assert child.pid in _named_for(run_id, home)
        assert ps._ENVIRON_INDEX[child.pid][0] == (tick, exe, stack)
    finally:
        _reap(child)


def test_another_users_process_is_skipped_without_an_environment_open(
    tmp_path: Path, environ_opens: list[int]
) -> None:
    """A process owned by another user is skipped before its environment is read.

    The other user's process is found in the live table as the positive
    control, so the absence of an open is the owner check at work rather than a
    table that never held it.
    """
    foreign = [
        int(name)
        for name in os.listdir("/proc")
        if name.isdigit() and ps._process_owner(int(name)) not in (None, os.geteuid())
    ]
    if not foreign:
        pytest.skip("no process owned by another user is present to skip")
    other_pid = foreign[0]

    _named_for(_unique_run_id(), tmp_path / "home")

    assert other_pid not in environ_opens


def test_a_process_read_before_its_exec_is_read_afresh_after_it_execs(
    tmp_path: Path, environ_opens: list[int]
) -> None:
    """A process whose environment is read before its exec is found after it.

    The child starts on the interpreter with its launcher's environment — no
    mark for this run, and the executable the parent runs — and blocks until the
    walk has read it. It then execs a different program carrying the run mark.
    The executable changed, so the next beat reads afresh and names it; a key
    holding only the pid and the start tick would answer from the pre-exec
    reading and would never see the mark.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    script = (
        "import os, sys\n"
        "sys.stdin.read(1)\n"
        "environment = dict(os.environ)\n"
        f"environment['RECKON_RUN_ID'] = {run_id!r}\n"
        f"environment['RECKON_HOME'] = {str(home)!r}\n"
        "os.execve('/bin/sh', ['sh', '-c', 'exec sleep 300'], environment)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Read the child's environment before it execs: no mark for this run.
        _wait_for_read(run_id, home, child.pid, environ_opens)
        assert _named_for(run_id, home) == []
        assert (ps._ENVIRON_INDEX[child.pid][1] or {}).get(RUN_ID_ENV) != run_id

        assert child.stdin is not None
        child.stdin.write(b"x")
        child.stdin.flush()
        assert child.pid in _wait_for_mark(run_id, home, child.pid)
    finally:
        child.kill()
        child.wait(timeout=10)


def test_a_process_that_reexecs_the_same_binary_is_read_afresh(
    tmp_path: Path, environ_opens: list[int]
) -> None:
    """A same-binary re-exec carrying the run mark is not answered from before it.

    The child starts on the interpreter with its launcher's environment — no
    mark for this run — and blocks until the walk has read it. It then execs the
    *same interpreter path* with the run mark in its environment. The pid, the
    start tick and the executable path are all unchanged, so a key holding the
    path alone answers from the pre-exec reading and never sees the mark. The
    base of the process's stack is placed afresh by the kernel on every exec,
    and it is what moves here: the case asserts the start tick and the path are
    unchanged and that the two identities still differ.
    """
    home = tmp_path / "home"
    home.mkdir()
    run_id = _unique_run_id()
    script = (
        "import os, sys\n"
        "sys.stdin.read(1)\n"
        "environment = dict(os.environ)\n"
        f"environment['RECKON_RUN_ID'] = {run_id!r}\n"
        f"environment['RECKON_HOME'] = {str(home)!r}\n"
        f"os.execve({sys.executable!r}, [{sys.executable!r}, '-c', "
        f"'import time; time.sleep({_CHILD_SLEEP_SECONDS})'], environment)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Read the child's environment before it execs: no mark for this run.
        _wait_for_read(run_id, home, child.pid, environ_opens)
        before = ps._process_identity(child.pid)
        assert _named_for(run_id, home) == []
        assert (ps._ENVIRON_INDEX[child.pid][1] or {}).get(RUN_ID_ENV) != run_id

        assert child.stdin is not None
        child.stdin.write(b"x")
        child.stdin.flush()
        assert child.pid in _wait_for_mark(run_id, home, child.pid)
        after = ps._process_identity(child.pid)
    finally:
        child.kill()
        child.wait(timeout=10)

    assert before[0] == after[0], "the start tick is unchanged by an exec"
    assert before[1] == after[1], (
        "the executable path is unchanged by a same-binary re-exec"
    )
    assert before != after, "a re-exec moves the identity even when the path does not"


def _wait_for_read(run_id: str, home: Path, pid: int, opened: list[int]) -> None:
    """Wait until a scan has read ``pid``'s environment, or fail."""
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    while pid not in opened and time.monotonic() < deadline:
        _named_for(run_id, home)
    assert pid in opened


def test_a_reparented_worker_is_found_by_the_liveness_read_and_the_stop_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker reparented away from its launcher is still found by both doors.

    A shell backgrounds the worker and exits, so the worker is reparented to
    init while it still carries the run mark. The liveness read's environment
    reach and the stop path's process walk must both name it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    run_id = _unique_run_id()
    worker_pid_file = tmp_path / "worker.pid"
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import os, time\n"
        f"open({str(worker_pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep({_CHILD_SLEEP_SECONDS})\n"
    )
    subprocess.run(
        [
            "/bin/sh",
            "-c",
            (f'"{sys.executable}" "{worker_script}" >/dev/null 2>&1 & echo launched'),
        ],
        env=_child_env(run_id, str(home)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    worker_pid = _wait_for_pid_file(worker_pid_file)
    try:
        assert worker_pid in _wait_for_mark(run_id, home, worker_pid)

        run_directory = runs.run_dir(run_id)
        run_directory.mkdir(parents=True, exist_ok=True)
        dead_pid = _absent_pid()
        (run_directory / "worker.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "pid": dead_pid,
                    "pid_start_time": None,
                    "launched_at": "2026-10-17T00:00:00Z",
                }
            )
        )
        record = {
            "run_id": run_id,
            "pid": dead_pid,
            "pid_start_time": None,
            "launcher_host": socket.gethostname(),
        }

        assert recovery_liveness._env_scan_run_pid(record) == worker_pid
        assert worker_pid in ps._run_process_pids(None, run_id=run_id, run_dir=None)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(worker_pid, 9)


def _wait_for_pid_file(path: Path) -> int:
    """The pid the worker wrote for itself, once it has started."""
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError(f"the worker never reported its pid at {path}")


def _absent_pid() -> int:
    """A pid no process holds, so it reads as a recorded pid that has gone."""
    for candidate in range(2_000_000, 2_001_000):
        if not Path(f"/proc/{candidate}").exists():
            return candidate
    raise AssertionError("no absent pid found in the scan range")
