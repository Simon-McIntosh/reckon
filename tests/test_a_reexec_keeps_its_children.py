"""A follower's launched workers survive its own process image replacement.

A follower that adopts newly installed code replaces its own process image
with ``os.execv``. The replacement keeps the pid and every parent-child
relationship and destroys every thread and all module state, so a reaper
thread and the launched-worker set built before the swap vanish even though
the process is still the parent of the workers it launched. A worker that
finishes after the swap can then never be collected by anyone: its parent is
alive, so it is never reparented to the init process that would collect it,
and the new image has forgotten it. A zero-signal liveness probe answers
``os.kill`` against the defunct entry with success, so the finished run reads
as still running and the owning session's next resume is refused with a
live-process reason.

The remedy under test carries the outstanding launched pids across the
replacement in the environment — the same carrier the reloader already uses
for its reader checkpoint — and the replacement image registers them exactly
as if it had launched them, because it is the same process and is still their
parent. A second defect, that the reaper starter trusts a recorded thread
object rather than a live thread, is tested beside it because it produces the
same symptom by a different route.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew.dispatch import (
    _LAUNCHED_WORKER_REAPER,
    _LAUNCHED_WORKERS,
    _LAUNCHED_WORKERS_HANDOVER_ENV,
    _LAUNCHED_WORKERS_LOCK,
    _adopt_launched_workers_from_reexec,
    _ensure_launched_worker_reaper,
    _export_launched_workers_for_reexec,
    _spawn,
)

# A worker that writes a marker to its event stream, sleeps past the handover,
# and exits cleanly, as a finished sweep resumption would.
_WORKER = """\
import sys, time
print('launched-worker-ran', flush=True)
time.sleep({sleep})
print('finished', flush=True)
"""


def _live_reaper_count() -> int:
    """Live reaper threads in this process, whatever their origin.

    The count is read fresh every call so an assertion never depends on how
    many predecessors left a daemon reaper behind in this shared process.
    """
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "reckon-worker-reaper" and thread.is_alive()
    )


def _pid_gone(pid: int, within: float = 6.0) -> bool:
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


def _no_defunct_child_under_self() -> bool:
    """Whether this process has no child left to wait on.

    Only this test process calls ``waitpid(-1, ...)``; the reapers wait on
    specific registered pids, so a successful non-blocking sweep here is a
    faithful reading of "no corpse of ours".
    """
    try:
        return os.waitpid(-1, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def _wait_reaped(*pids: int) -> None:
    """Wait for every pid to leave the process table, leaving no corpse.

    Each test reaps what it spawned before it ends so a later test's defunct
    sweep never trips on a predecessor's residue.
    """
    for pid in pids:
        assert _pid_gone(pid), f"pid {pid} was not collected before the test ended"


def _spawn_worker(tree: Path, marker: str, sleep: float = 2.0) -> tuple[int, Path]:
    """Launch one real worker through the production spawn path and return it.

    ``_spawn`` is the single funnel every launch goes through; registering a
    child with it is what the launched-worker set and the reaper own.
    """
    plan = _backends.LaunchPlan(
        backend="probe",
        dialect="probe",
        argv=[
            sys.executable,
            "-c",
            _WORKER.format(sleep=sleep) + f"print({marker!r})",
        ],
        cwd=str(tree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    log = tree / f"{marker}.log"
    prompt = tree / f"{marker}.prompt.txt"
    prompt.write_text("continue\n", encoding="utf-8")
    pid = _spawn(
        plan,
        log_path=log,
        stderr_path=tree / f"{marker}.stderr.log",
        prompt_path=prompt,
    )
    return pid, log


@pytest.fixture(autouse=True)
def _registry_isolation() -> None:
    """Each test owns the launched-worker registry, not the previous one.

    The reaper threads are process-lifetime daemons and cannot be stopped
    here; what each test must own is the registry and the recorded holder, so
    a handover or reaper-start assertion never reads a predecessor's residue.
    """
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _LAUNCHED_WORKER_REAPER["thread"] = None
    os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, None)
    yield
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _LAUNCHED_WORKER_REAPER["thread"] = None
    os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, None)


def test_pids_outstanding_at_a_replacement_land_in_the_new_registry(
    tmp_path: Path,
) -> None:
    """The replacement image owns the pids the old image left behind.

    Two launched workers are still running when the follower replaces itself,
    and the handover is asserted on the registry the new image built from it —
    the set of pids it will collect — rather than on the carrier that crossed
    the boundary. The pids go through the production spawn path so they are
    real children of this process, which is what makes them collectable.
    """
    tree = tmp_path
    tree.mkdir(exist_ok=True)
    first, _ = _spawn_worker(tree, "one", sleep=2.0)
    second, _ = _spawn_worker(tree, "two", sleep=2.0)
    _export_launched_workers_for_reexec()

    # The replacement destroys module state, registry and reaper thread alike.
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _LAUNCHED_WORKER_REAPER["thread"] = None

    _adopt_launched_workers_from_reexec()
    with _LAUNCHED_WORKERS_LOCK:
        assert {first, second} == _LAUNCHED_WORKERS
    # Both children exit after the handover and are collected before this test
    # ends, leaving nothing defunct under this process for a successor to trip on.
    _wait_reaped(first, second)
    assert _no_defunct_child_under_self()


def test_a_carried_worker_that_finishes_after_the_replacement_is_collected(
    tmp_path: Path,
) -> None:
    """The new image collects a worker the old image launched before the swap.

    The worker is spawned, still alive when the handover is taken, and exits
    only afterwards — the exact ordering that used to strand a defunct child
    under the follower — and is collected by the reaper the new image owns,
    asserted against the real spawned child.
    """
    tree = tmp_path
    tree.mkdir(exist_ok=True)
    pid, log = _spawn_worker(tree, "carried", sleep=1.5)
    _export_launched_workers_for_reexec()

    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _LAUNCHED_WORKER_REAPER["thread"] = None

    _adopt_launched_workers_from_reexec()
    # The child was still running at handover and exits afterwards; the new
    # image collects it rather than leaving a corpse for a zero-signal probe.
    assert _pid_gone(pid), "the carried worker was not collected after the swap"
    assert _no_defunct_child_under_self()
    assert "launched-worker-ran" in log.read_text(encoding="utf-8")


def test_a_replacement_with_nothing_outstanding_hands_over_nothing() -> None:
    """A follower that launched nothing hands its replacement nothing.

    The ordinary steady state of a follower is a clean handover: an empty
    registry carries no pids, so the replacement starts empty and never
    fabricates work for its successor.
    """
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    # A stale carrier from an earlier image that was never consumed must not
    # leak forward into a successor that launched nothing of its own.
    os.environ[_LAUNCHED_WORKERS_HANDOVER_ENV] = json.dumps([1, 2, 3])

    _export_launched_workers_for_reexec()
    assert _LAUNCHED_WORKERS_HANDOVER_ENV not in os.environ

    _adopt_launched_workers_from_reexec()
    with _LAUNCHED_WORKERS_LOCK:
        assert set() == _LAUNCHED_WORKERS


@pytest.mark.parametrize(
    "carrier",
    [None, "", "[]", "not-json", '{"not": "a list"}', "123", "[1,,2]"],
    ids=[
        "absent",
        "empty-string",
        "empty-list",
        "garbage",
        "object",
        "scalar",
        "broken-list",
    ],
)
def test_an_absent_empty_or_unparseable_carrier_starts_the_new_image_clean(
    carrier: str | None,
) -> None:
    """A malformed handover is an empty registry, never a refusal to start.

    A follower that refuses to start is worse than one that misses a reap, so
    none of these carriers raises and none contributes a pid; the new image
    simply begins with no reaper obligation.
    """
    if carrier is None:
        os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, None)
    else:
        os.environ[_LAUNCHED_WORKERS_HANDOVER_ENV] = carrier
    _adopt_launched_workers_from_reexec()  # must not raise
    with _LAUNCHED_WORKERS_LOCK:
        assert set() == _LAUNCHED_WORKERS


def test_the_reaper_starter_trusts_a_live_thread_not_a_recorded_object() -> None:
    """A recorded reaper object is replaced, never just trusted.

    The stale-object guard is the second defect in this area: a reaper thread
    that has ended for any reason leaves its object in the holder, and a
    start-once check keyed to the object would then stop reaping silently for
    the life of the process. Repeated calls must start exactly one live reaper,
    and a fresh one must replace a recorded reaper whose thread has ended.
    """
    before = _live_reaper_count()
    _LAUNCHED_WORKER_REAPER["thread"] = None
    _ensure_launched_worker_reaper()
    after_first = _live_reaper_count()
    assert after_first == before + 1
    first = _LAUNCHED_WORKER_REAPER["thread"]
    assert first is not None and first.is_alive()

    _ensure_launched_worker_reaper()
    assert _live_reaper_count() == after_first
    assert _LAUNCHED_WORKER_REAPER["thread"] is first

    # The recorded thread ends, as an uncaught exception in the poll loop
    # would end it; the holder keeps the corpse. The next launch must start a
    # live reaper rather than trust it.
    with _LAUNCHED_WORKERS_LOCK:
        ended = threading.Thread(target=lambda: None, name="reckon-worker-reaper")
        ended.start()
        ended.join()
        _LAUNCHED_WORKER_REAPER["thread"] = ended

    _ensure_launched_worker_reaper()
    assert _LAUNCHED_WORKER_REAPER["thread"] is not ended
    assert _LAUNCHED_WORKER_REAPER["thread"].is_alive()
    assert _live_reaper_count() == after_first + 1


def test_a_caller_managed_child_is_still_never_collected_after_a_handover(
    tmp_path: Path,
) -> None:
    """The reaper's reach ends at the registered set, across the handover too.

    A child a caller waits on itself — a condition probe, a lane probe — is
    never registered, so the handover carries nothing about it and nothing
    attempts to collect it. The caller's own wait must still succeed after the
    replacement, which a rehearsed collect would have stolen.
    """
    managed = subprocess.Popen(
        [sys.executable, "-c", _WORKER.format(sleep=1.0)],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _export_launched_workers_for_reexec()
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _LAUNCHED_WORKER_REAPER["thread"] = None
    _adopt_launched_workers_from_reexec()

    # The caller's own wait is still owed and still succeeds: only one waiter
    # wins a child, so a successful wait here is proof the reaper never took it.
    got, status = os.waitpid(managed.pid, 0)
    assert got == managed.pid
    assert os.WEXITSTATUS(status) == 0


def test_the_reloader_exports_launched_pids_beside_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reloader hands the pids over at the moment it replaces itself.

    The handover belongs in the replacement path's preparation step, beside
    the reader checkpoint written just before ``os.execv`` — the call site the
    follower actually reaches. The pids appear in the environment the
    replacement image inherits, next to the checkpoint it will resume from.
    """
    from reckon import cli
    from reckon.crew import runs as runs_module

    tree = tmp_path
    tree.mkdir(exist_ok=True)
    pid, _ = _spawn_worker(tree, "handed", sleep=2.0)

    stamps = iter(["old-stamp", "new-stamp"])
    monkeypatch.setattr(runs_module, "follower_code_stamp", lambda: next(stamps))
    reloader = cli._FollowerReloader("proj", None)

    captured: list[str] = []

    def capture_execv(exe: str, argv: list[str]) -> None:
        captured.append(os.environ.get(_LAUNCHED_WORKERS_HANDOVER_ENV, ""))
        raise OSError("execv captured, not executed")

    monkeypatch.setattr(cli.os, "execv", capture_execv)
    reloader.poll({})
    assert captured == [json.dumps([pid])]
    _wait_reaped(pid)
