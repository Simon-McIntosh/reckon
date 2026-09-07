"""A finished process still waiting on the table is not a running run.

``process_alive`` probes a pid with a zero signal and reports whatever the
probe says. A zombie is a process-table entry whose process has exited and
whose exit status the parent has not yet collected; the kernel accepts a zero
signal against it, so the probe reports it alive. The reaper that collects
launched workers runs on a background thread, so there is always a window in
which a finished run's pid is a zombie — and during that window every reader
gets the wrong answer in the dangerous direction: the run reads as "still
running", holds its slot, refuses its own resume, and can only be cleared by
whichever party owns the parent. The fix narrows the probe: when the
per-process stat record reports the zombie state, the function answers "not
alive", because the process this caller is asking about has exited either way.
The change must narrow to the zombie case alone, so a pid that lives and one
whose stat record cannot be read at all behave exactly as before.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import pytest

from reckon.crew import runs


def _pid_max() -> int:
    """The largest process id the kernel will allocate; beyond it, no pid."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip())


def _stat_fields(pid: int) -> list[str]:
    """The per-process stat fields as the kernel writes them."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rfind(")") + 2 :].split()


def _wait_for_state(pid: int, state: str, within: float = 10.0) -> bool:
    """Whether the pid's kernel state becomes ``state`` within the window."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        fields = _stat_fields(pid)
        if fields and fields[0] == state:
            return True
        time.sleep(0.01)
    return False


def _spawn_holdable_child() -> tuple[int, int]:
    """Fork a child that stays alive and exits when its hold pipe closes.

    The child writes a readiness byte, then blocks reading the hold pipe.
    The parent keeps the write end of the hold pipe; closing it makes the
    child's read return and the child exit. Nothing reaps the child until the
    parent waits on it, so after it exits it sits in the zombie state — which
    is exactly the state this module now answers "not alive" for.
    """
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        try:
            os.write(ready_w, b"x")
            os.close(ready_w)
            os.read(hold_r, 1)
        finally:
            os._exit(0)
    os.close(ready_w)
    os.close(hold_r)
    os.read(ready_r, 1)
    os.close(ready_r)
    return pid, hold_w


def test_a_zombie_stat_record_reports_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel zombie state makes the probe answer not alive, without a
    signal probe at all — the process has exited regardless of who owns it."""
    monkeypatch.setattr(runs, "_process_state", lambda pid: "Z")
    assert runs.process_alive(12345) is False


@pytest.mark.parametrize("state", ["R", "S", "T", "D"])
def test_live_states_report_alive(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    """Running, sleeping, stopped and uninterruptible-sleep states all stay
    alive: the change narrows to the zombie case alone rather than treating
    every parsed state as a verdict."""
    monkeypatch.setattr(runs, "_process_state", lambda pid: state)
    # os.getpid() is a real, signalable process, so the probe path succeeds
    # and the state override is the only thing varied.
    assert runs.process_alive(os.getpid()) is True


def test_a_pid_with_no_stat_record_returns_as_it_does_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable stat record is not proof of death: the question falls
    through to the signal probe unchanged, in both directions."""
    monkeypatch.setattr(runs, "_process_state", lambda pid: None)
    assert runs.process_alive(os.getpid()) is True
    assert runs.process_alive(_pid_max() + 4096) is False


def test_an_unparsable_stat_record_returns_as_it_does_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat record that exists but yields no state field behaves as today
    rather than raising: the probe still answers, and unparsable is not a
    verdict about the process."""
    monkeypatch.setattr(runs, "_process_stat_fields", lambda pid: [])
    assert runs._process_state(os.getpid()) is None
    assert runs.process_alive(os.getpid()) is True


def test_no_pid_and_non_integer_pid_return_as_it_does_today() -> None:
    """The no-pid branch is untouched: falsy pids are None, and a pid that
    cannot be a process id is None too."""
    assert runs.process_alive(None) is None
    assert runs.process_alive(0) is None
    assert runs.process_alive("") is None
    assert runs.process_alive("not-a-pid") is None


def test_a_real_child_reports_alive_then_not_alive_after_exit() -> None:
    """The field offset is proven against the kernel, not a fixture.

    The same real child reports alive while it is running and not alive once
    it has exited without being waited on — the exact window in which a
    zero-signal probe alone reports a finished run as running.
    """
    pid, hold_w = _spawn_holdable_child()
    hold_open = True
    try:
        assert _stat_fields(pid)[0] in ("R", "S"), _stat_fields(pid)
        assert runs.process_alive(pid) is True, (
            "a live child reports alive before it exits"
        )
        os.close(hold_w)
        hold_open = False
        assert _wait_for_state(pid, "Z"), f"child {pid} did not reach zombie state"
        assert runs.process_alive(pid) is False, (
            "a zombie's process has exited; every caller asking whether work is "
            "still running wants no"
        )
    finally:
        if hold_open:
            os.close(hold_w)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
