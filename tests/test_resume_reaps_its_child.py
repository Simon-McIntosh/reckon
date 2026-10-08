"""A launched worker never lingers as an unreaped child of its launcher.

A worker is a direct child of whatever process called the spawn: an ordinary
dispatch CLI for a normal launch, or the follower for a resume the sweep makes.
The shorter-lived of those two is already dead before its worker finishes, and
init adopts the orphan, so for a plain dispatch the child is reaped by someone
and nobody notices anything. A long-lived launcher — a follower that sweeps the
resume-ready queue on a cadence — is still alive when its worker finishes, and
nobody waits on the child, so it sits in the process table as a defunct entry
for as long as the follower lives. A zero-signal liveness probe answers
``os.kill`` against a defunct pid with success, so the finished resume reads as
still running and the next resume of the same run is refused.

The remedy under test is on the spawning side: every launch registers the pid
with a reaper owned by the launching process, and the reaper owns the wait the
caller is too short-lived or too busy to make. These tests assert the process
state the liveness check would see — a finished worker that is gone from the
process table and no longer alive to a probe — rather than asserting that some
reap call was made.

Each assertion asks only about the pids its own case launched: a corpse that
some other case on the same pytest worker left behind belongs to that case, and
asking about every child of the process made this file red on the order in
which xdist happened to distribute its neighbours.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import reckon.crew.dispatch_launch as dispatch_launch_module
from reckon import _backends
from reckon.crew.dispatch import _spawn
from reckon.crew.runs import process_alive

# reckon.crew.dispatch is reachable only through import_module: the crew module
# defines a dispatch function that shadows the submodule on attribute lookup.
crew_dispatch = importlib.import_module("reckon.crew.dispatch")

# A worker that writes one marker to its event stream and exits cleanly, as a
# finished sweep resumption would.
_WORKER = "import sys\nprint('launched-worker-ran')\nsys.stdout.flush()\n"


def _worker_plan(tree: Path, marker: str) -> _backends.LaunchPlan:
    """A plan whose worker prints the marker and exits zero."""
    return _backends.LaunchPlan(
        backend="probe",
        dialect="probe",
        argv=[sys.executable, "-c", _WORKER + f"print({marker!r})"],
        cwd=str(tree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )


def _spawn_worker(tree: Path, marker: str) -> tuple[int, Path]:
    """Launch one worker through the production spawner; return pid and log."""
    log = tree / "worker.log"
    prompt = tree / "prompt.txt"
    prompt.write_text("continue\n", encoding="utf-8")
    pid = _spawn(
        _worker_plan(tree, marker),
        log_path=log,
        stderr_path=tree / "worker.stderr.log",
        prompt_path=prompt,
    )
    return pid, log


def _pid_gone(pid: int, within: float = 5.0) -> bool:
    """Whether the pid left the process table within the window.

    A reaped child is gone from /proc; a defunct one is not, which is exactly
    the difference the reaper is supposed to make.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not os.path.exists(f"/proc/{pid}"):
            return True
        time.sleep(0.05)
    return False


def _pid_defunct_within(pid: int, within: float = 5.0) -> bool:
    """Whether the pid has exited and is still sitting unreaped in the table.

    ``/proc`` keeps a zombie's entry until somebody waits on it, so an entry
    whose state is ``Z`` is exactly the unreaped corpse a long-lived launcher
    used to leave behind; a reaped pid has no entry at all.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        # The comm field can itself hold spaces and parentheses, so the state
        # is the first token after the final ')'.
        if stat.rsplit(")", 1)[-1].split()[0] == "Z":
            return True
        time.sleep(0.05)
    return False


def _no_defunct_launched_child(pids: list[int]) -> bool:
    """Whether none of the pids this case launched is left unreaped.

    Only the given pids are asked about. A defunct child answers a
    non-blocking wait with its own pid — the state every caller here treats as
    a failure — a running child answers (0, 0), and a reaped one is no longer
    a child at all (ECHILD). A corpse another case on the same xdist worker
    left behind is neither reaped here, which would steal its owner's exit
    status, nor reported, which made this file red on test order alone.
    """
    for pid in pids:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if waited == pid:
            return False
    return True


def test_a_shortlived_launcher_leaves_a_worker_that_survives_and_completes(
    tmp_path: Path,
) -> None:
    """An ordinary dispatch's worker is still detached and still finishes.

    The dispatch CLI exits moments after spawning; the worker must keep running
    in its own session, write its stream, and be adopted by init rather than
    taken down with the launcher.
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    log = tree / "worker.log"
    prompt = tree / "prompt.txt"
    prompt.write_text("continue\n", encoding="utf-8")
    # The worker lingers briefly so the test can observe it after the parent
    # that spawned it has already exited.
    sleepy = _WORKER + "import time; time.sleep(1.0)\nprint('finished')\n"
    spawner = (
        "import sys\n"
        "from pathlib import Path\n"
        "from reckon import _backends\n"
        "from reckon.crew.dispatch import _spawn\n"
        "plan = _backends.LaunchPlan("
        "backend='probe', dialect='probe', argv=[sys.executable, '-c',"
        f" {sleepy!r}], cwd={str(tree)!r}, stdin_text='', environment={{}},"
        " final_message_path=None, resumed_session=None)\n"
        "pid = _spawn(plan, log_path=Path('worker.log'), "
        "stderr_path=Path('worker.stderr.log'), prompt_path=Path('prompt.txt'))\n"
        "print(pid)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", spawner],
        cwd=tree,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    pid = int(completed.stdout.split()[-1])
    # The launcher is already gone; the worker must still be its own session
    # leader, detached from any process group a dying parent could take down.
    assert os.getsid(pid) == pid
    assert _pid_gone(pid, within=5.0)
    text = log.read_text(encoding="utf-8")
    assert "launched-worker-ran" in text
    assert "finished" in text


def test_a_finished_worker_leaves_no_defunct_child_under_the_launching_process(
    tmp_path: Path,
) -> None:
    """The sweep's long-lived launcher keeps no corpse after the worker ends.

    The test process plays the follower: it stays alive past the worker's
    exit, which is precisely the condition that used to strand a defunct
    child. After the worker finishes, the child must be gone from the process
    table, there must be no child left to wait on, and the liveness probe a
    resume consults must read it as no longer running.
    """
    tree = tmp_path
    tree.mkdir(exist_ok=True)
    pid, log = _spawn_worker(tree, "one")

    assert _pid_gone(pid, within=5.0), (
        f"worker {pid} was reaped by the reaper, not left defunct"
    )
    # Nothing of this launch is left to wait on now: a defunct child would
    # still be reapable here, so this is the state assertion of "no corpse".
    assert _no_defunct_launched_child([pid])
    assert process_alive(pid) is False, (
        "a resume durably consults process state; a reaped worker is not alive"
    )
    assert "launched-worker-ran" in log.read_text(encoding="utf-8"), (
        "the worker ran to completion and wrote its stream"
    )


def test_repeated_launches_accumulate_no_defunct_children(tmp_path: Path) -> None:
    """The guarantee holds per launch and across a sweep cadence, not once.

    A follower sweeps on a cadence, so the failure was recurring: every run the
    sweep woke left one more corpse under the follower. Launch several workers
    sequentially from the one long-lived process and require each to be reaped
    before the next is considered.
    """
    pids = []
    for index in range(3):
        pid, _log = _spawn_worker(tmp_path, f"launch-{index}")
        pids.append(pid)
        assert _pid_gone(pid, within=5.0), (
            f"launch {index}: worker {pid} reaped after finishing"
        )
    assert _no_defunct_launched_child(pids)


def test_the_followup_resume_of_the_same_run_is_not_refused_on_liveness(
    tmp_path: Path,
) -> None:
    """The exact condition behind the refusal reads clean after the worker ends.

    The refusal this whole node exists to remove is the one the next resume
    hits when the previous resume's worker sits unreaped: the process-table
    entry answers a zero signal, so the finished run reads as still having a
    live process. With the child reaped, the same probe the guard consults
    reports the run is no longer running, which is all the guard needs to let
    the next resume through.
    """
    tree = tmp_path
    tree.mkdir(exist_ok=True)
    pid, _log = _spawn_worker(tree, "resumed")
    assert _pid_gone(pid, within=5.0)
    # This is the process-state fact crew resume reads before refusing with
    # "still has a live process; observe or stop it before resuming".
    assert process_alive(pid) is not True


def test_an_unreaped_child_of_this_process_does_not_redden_the_reap_case(
    tmp_path: Path,
) -> None:
    """A corpse this process already held is not the launched worker's business.

    An xdist worker runs this file beside whatever earlier test left an exited,
    unreaped child of its own in the same process, and which earlier test that
    is depends on how the files are dealt out. The case leaves a corpse of
    exactly that shape — its own child, exited and deliberately not waited on —
    and then runs the reap case: the worker launched through the production
    spawner must still be reaped, and the per-pid assertion must still read
    clean. A whole-process wait instead reaps this stray corpse, reads a
    waitable child and reddens the case.
    """
    tree = tmp_path
    tree.mkdir(exist_ok=True)
    # A launcher that never waits: the shape an earlier case in this process
    # can leave behind. The handle is held for the life of the case, because
    # collecting the Popen would reap the child and dissolve the corpse this
    # case exists to keep in place.
    stray = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _pid_defunct_within(stray.pid), (
            "the case's own child exited and was left unreaped"
        )
        pid, _log = _spawn_worker(tree, "beside-an-unreaped-child")
        assert _pid_gone(pid, within=5.0), (
            f"worker {pid} was reaped although this process still holds an "
            "unrelated unreaped child"
        )
        assert _no_defunct_launched_child([pid]), (
            "the assertion asks only about the worker this case launched"
        )
    finally:
        stray.wait(timeout=10)


def test_a_worker_left_defunct_by_the_launcher_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker nobody reaps is reported, not quietly waited past.

    The launcher here really does leave its worker behind: the reaper's poll is
    switched off for one launch, which is the state the defect's launcher
    produced, and the worker stays in the process table. The case shows both
    of the facts the assertion reads — the pid still present, and still
    waitable — so a check that had stopped noticing a corpse reports it here
    rather than passing over it.
    """
    tree = tmp_path
    tree.mkdir(exist_ok=True)
    monkeypatch.setattr(dispatch_launch_module, "_reap_launched_workers", lambda: None)
    pid, _log = _spawn_worker(tree, "left-defunct")
    assert _pid_defunct_within(pid), (
        f"worker {pid} exited and nothing waited on it, which is the state "
        "the launcher under test used to leave behind"
    )
    assert not _pid_gone(pid, within=0.3), (
        "a defunct worker still has its entry in the process table"
    )
    assert not _no_defunct_launched_child([pid]), (
        "the assertion reports the worker the launcher left defunct; a check "
        "that passed here would miss the corpse entirely"
    )
