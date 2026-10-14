"""A run's processes end with the run, whichever pid still names them.

A supervisor that exits ahead of its worker — a stop, a crash, or an
intermediary that returns while the worker runs on — leaves the worker
reparented to init and outside any process group ``crew stop`` can signal. The
worker's own children are then left working too. This locks the reach in: a
signal the run sends is delivered to every process the run owns, found by
descent from the run's recorded pids and by the run id in each process's
environment, and the record names the pids that were ended. A process that
merely holds the run's worktree is reported and never signalled.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon.crew import recovery, routing
from reckon.crew.dispatch_launch import _supervisor_exit_record
from reckon.crew.recovery_liveness import _process_children, process_alive
from reckon.crew.runs import pointer_path, run_dir

HOST = socket.gethostname()
MARK = routing.RUN_ID_ENV
# The declared negative control mutates the reach to the form before the
# extension: the supervisor pid alone is signalled and the worker's child lives.
NEGATIVE_CONTROL_ENV = "RECKON_TEST_SIGNAL_SUPERVISOR_ALONE"


def _reach() -> bool:
    """Whether the reach under test is applied, or the negative control is."""
    return os.environ.get(NEGATIVE_CONTROL_ENV) != "1"


def _alive(pid: int) -> bool:
    """Whether the process table still names a running process at ``pid``."""
    return process_alive(pid) is True


def _wait_gone(pids: list[int], *, deadline_seconds: float = 5.0) -> bool:
    """Poll until none of ``pids`` is alive, or the bound elapses."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            return True
        time.sleep(0.02)
    return False


_HOLDER_SOURCE = r"""
import os
import sys

children = int(sys.argv[1])
hold = int(sys.argv[2])
if sys.argv[3] != "-":
    os.chdir(sys.argv[3])
for _ in range(children):
    if os.fork() == 0:
        os.read(hold, 1)
        os._exit(0)
os.write(int(sys.argv[4]), b"x")
os.close(int(sys.argv[4]))
os.read(hold, 1)
"""


def _fork_holder(
    *,
    run_id: str | None = None,
    children: int = 0,
    cwd: Path | None = None,
    new_session: bool = False,
) -> tuple[int, int]:
    """Start a root process that holds, with ``children`` holdable children.

    The root is exec'd rather than left as a fork, so its environment — the
    run id mark a launcher writes — is visible in ``/proc/<pid>/environ``,
    which reports the environment the process was exec'd with rather than one
    the interpreter changed in place afterwards. Its children inherit the same
    environment, the shape a launcher leaves behind. Every process reads the
    same hold pipe, so closing the write end releases the whole tree. ``cwd``
    moves the root into a directory, which is how a process that owns nothing
    still comes to hold a tree.
    """
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    for fd in (ready_w, hold_r):
        os.set_inheritable(fd, True)
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        if new_session:
            os.setsid()
        environment = dict(os.environ)
        if run_id is not None:
            environment[MARK] = run_id
        try:
            # The child is exec'd, not forked and left running, so its
            # environment is the one the kernel will report: a launcher's run
            # id mark must be visible in /proc/<pid>/environ, which reflects
            # the exec-time environment rather than later in-process changes.
            os.execvpe(  # noqa: S606 — a fixed interpreter and argv, no shell
                sys.executable,
                [
                    sys.executable,
                    "-c",
                    _HOLDER_SOURCE,
                    str(children),
                    str(hold_r),
                    str(cwd) if cwd is not None else "-",
                    str(ready_w),
                ],
                environment,
            )
        except OSError:
            os._exit(1)
    os.close(ready_w)
    os.close(hold_r)
    os.read(ready_r, 1)
    os.close(ready_r)
    return pid, hold_w


@contextmanager
def _tree(
    run_id: str | None,
    *,
    children: int = 0,
    cwd: Path | None = None,
    new_session: bool = False,
) -> Iterator[list[int]]:
    """A running process tree, released and checked by pid on the way out."""
    root, hold_w = _fork_holder(
        run_id=run_id, children=children, cwd=cwd, new_session=new_session
    )
    pids = [root]
    try:
        yield pids
    finally:
        with contextlib.suppress(OSError):
            os.close(hold_w)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(root, 0)
        # The root's children are not this process's to reap: poll the table
        # until they have left it, and never leave one running.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.02)
        for pid in pids:
            assert process_alive(pid) is not True, f"pid {pid} survived its tree"


def _with_children(root: int) -> list[int]:
    """``root`` and the pids the kernel lists under it, as one list."""
    return [root, *_process_children(root)]  # type: ignore[misc]


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate run directories and pointers from the operator's crew state."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _write_pointer(run_id: str, record: dict) -> None:
    path = pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _write_worker_record(run_id: str, *, pid: int, start_time: str | None) -> Path:
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "worker.json"
    path.write_text(
        json.dumps(
            {"run_id": run_id, "attempt": 1, "pid": pid, "pid_start_time": start_time}
        ),
        encoding="utf-8",
    )
    return path


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


def test_sigterm_to_a_supervisor_leaves_no_worker_or_worker_child(
    crew_home: Path,
) -> None:
    # The reproduction: a supervisor whose worker has started a child of its
    # own. Signalling the supervisor must end the worker it holds and the child
    # under it, and the exit record must name the pids ended.
    run_id = "r-supervisor-sigterm"
    with _tree(run_id, children=1) as pids:
        root = pids[0]
        pids[:] = _with_children(root)
        assert len(pids) == 2, f"expected a worker and its child: {pids}"
        record_dir = run_dir(run_id)
        record_dir.mkdir(parents=True, exist_ok=True)
        _write_worker_record(
            run_id, pid=root, start_time=routing._process_start_time(root)
        )
        if _reach():
            ended = routing.signal_worker(
                root,
                signal.SIGTERM,
                reason="run-stop",
                run_dir=record_dir,
                run_id=run_id,
                reach_run=True,
            )
        else:
            # The declared mutation: the supervisor pid alone, the form before
            # the extension, so the worker's child survives and the case fails.
            delivered = routing.signal_worker(
                root, signal.SIGTERM, reason="run-stop", run_dir=record_dir
            )
            ended = [root] if delivered else []
        assert _wait_gone(pids), f"a process of the run survived: {pids}"
        exit_record = _supervisor_exit_record(
            run_id=run_id,
            attempt=1,
            worker_pid=root,
            launched_at="2026-10-14T00:00:00Z",
            status=None,
            run_directory=record_dir,
            ended_pids=ended,
        )
    # The negative control signals the supervisor pid alone: the child survives
    # and this assertion fails.
    assert set(ended) >= set(pids)
    assert set(exit_record["ended_pids"]) >= set(pids)


def test_crew_stop_ends_a_reparented_worker_and_its_children(
    crew_home: Path,
) -> None:
    # The supervisor is gone and the worker was reparented to init, so the
    # recorded pids name nothing. The worker and its child are found by the run
    # id in their environment, ended, and named on the stopped record.
    run_id = "r-reparented-worker"
    with _tree(run_id, children=1) as pids:
        worker = pids[0]
        pids[:] = _with_children(worker)
        assert len(pids) == 2, f"expected a worker and its child: {pids}"
        _write_pointer(
            run_id,
            {
                "run_id": run_id,
                "project": "fixture-project",
                "pid": _absent_pid(),
                "pid_start_time": None,
                "worktree": "",
                "phase": "working",
            },
        )
        _write_worker_record(run_id, pid=_absent_pid(), start_time=None)

        from reckon.crew import dispatch_sessions

        record = dispatch_sessions.terminate(run_id)

        assert record["alive_after"] == []
        assert set(record["ended_pids"]) >= set(pids)
        assert _wait_gone(pids), f"a process of the run survived: {pids}"


def test_an_identity_checked_signal_with_a_run_dir_reaches_the_whole_run(
    crew_home: Path,
) -> None:
    # Every caller ending a run goes through the identity-checked signal, which
    # hands over the run's directory. The reach runs from the pid it was handed
    # to the whole run: a worker reparented to init and its child, carrying the
    # run's env mark and unrelated to the handed pid, are ended too. The handed
    # pid leads its own session, the shape a caller that signalled its own group
    # would take down with it, and the identity check still guards it.
    run_id = "r-process-group-reach"
    with (
        _tree(run_id, children=1) as reparented,
        _tree(None, new_session=True) as handed,
    ):
        worker = reparented[0]
        reparented[:] = _with_children(worker)
        assert len(reparented) == 2, f"expected a worker and its child: {reparented}"
        handed_pid = handed[0]
        record_dir = run_dir(run_id)
        record_dir.mkdir(parents=True, exist_ok=True)
        _write_worker_record(run_id, pid=_absent_pid(), start_time=None)

        routing._signal_process_group(
            handed_pid,
            routing._process_start_time(handed_pid),
            run_dir=record_dir,
            reason="promotion-release",
        )

        all_pids = [handed_pid, *reparented]
        assert _wait_gone(all_pids), f"a process of the run survived: {all_pids}"


def test_a_recycled_worker_record_pid_is_not_walked(crew_home: Path) -> None:
    # A worker record names a pid, and the kernel may have handed that number to
    # an unrelated process long after the worker it registered exited. Walking
    # from it would descend into that process's children and signal them, so the
    # walk is taken only while the pid still names the process the record
    # registered — proven here by the same process being reached once its own
    # start tick is the one recorded.
    run_id = "r-recycled-worker-record"
    with _tree(None, children=1) as stale:
        root = stale[0]
        stale[:] = _with_children(root)
        assert len(stale) == 2, f"expected a process and its child: {stale}"
        record_dir = run_dir(run_id)
        record_dir.mkdir(parents=True, exist_ok=True)

        _write_worker_record(run_id, pid=root, start_time="1")
        ended = routing.signal_worker(
            0,
            signal.SIGTERM,
            reason="reach-probe",
            run_dir=record_dir,
            run_id=run_id,
            reach_run=True,
            expected_start_time=None,
        )
        assert ended == [], f"a recycled pid was reached: {ended}"
        for pid in stale:
            assert _alive(pid), f"pid {pid} of an unrelated process was signalled"

        _write_worker_record(
            run_id, pid=root, start_time=routing._process_start_time(root)
        )
        ended = routing.signal_worker(
            0,
            signal.SIGTERM,
            reason="reach-probe",
            run_dir=record_dir,
            run_id=run_id,
            reach_run=True,
        )
        assert set(ended) >= set(stale), f"the recorded worker was missed: {ended}"
        assert _wait_gone(stale), f"a process of the run survived: {stale}"


def test_a_worktree_holder_with_no_mark_is_listed_and_left_alive(
    crew_home: Path,
) -> None:
    # A process that changed into the worktree carries no ownership mark, so it
    # is reported but never signalled — the reach must not widen into a
    # kill-everything.
    run_id = "r-worktree-holder"
    worktree = crew_home / "worktree"
    worktree.mkdir()
    with _tree(None, cwd=worktree) as pids:
        holder = pids[0]
        _write_pointer(
            run_id,
            {
                "run_id": run_id,
                "project": "fixture-project",
                "pid": _absent_pid(),
                "pid_start_time": None,
                "worktree": str(worktree),
                "phase": "working",
            },
        )

        from reckon.crew import dispatch_sessions

        record = dispatch_sessions.terminate(run_id)

        held_pids = {entry["pid"] for entry in record["held_by"]}
        assert holder in held_pids, record["held_by"]
        assert holder not in record["ended_pids"]
        assert holder not in record["alive_after"]
        assert _alive(holder), "the worktree holder must be left alive"


def test_a_run_whose_supervisor_exited_is_not_classified_complete(
    crew_home: Path, tmp_path: Path
) -> None:
    # The reproduced gap: the pointer's supervisor and the worker record's pid
    # are both gone while a process still names the run. The run must read
    # alive, so it is never classified complete and never sent on to scoring.
    run_id = "r-reparented-classification"
    manifest = tmp_path / "manifest.md"
    manifest.write_text("node: run\nstatus: complete\n", encoding="utf-8")
    with _tree(run_id) as pids:
        _write_worker_record(run_id, pid=_absent_pid(), start_time=None)
        pointer = {
            "run_id": run_id,
            "project": "fixture-project",
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": "2026-10-14T00:00:00Z",
            "manifest_path": str(manifest),
            "log_path": str(tmp_path / f"{run_id}.jsonl"),
            "process_alive": None,
            "pid": _absent_pid(),
            "pid_start_time": None,
            "launcher_host": HOST,
        }
        row = recovery.classify_pointer(pointer, now_seconds=time.time())
        assert row["process_alive"] is True
        assert row["classification"] == "running"
        assert row["classification"] not in ("promotable", "scoring")
        assert pids[0]
