"""The per-run supervisor survives the process that launched it.

Off the fleet a dispatch starts the run's supervisor by forking it. Forked with
a single ``Popen`` the supervisor is the launcher's child, so when the launcher
ends — a coordinator turn finishing, a shell returning — the supervisor goes
with it and the run it is carrying is orphaned mid-flight. The launcher instead
double-forks: an intermediate starts the supervisor in a session of its own and
exits at once, and the launching process is never the supervisor's parent.

The launcher must still return the pid ``crew stop`` signals, so the supervisor
is the leader of its own session and process group, with the worker spawned
inside that group. These tests start a supervisor from a long-lived child
process — a launcher that is still running when the checks are taken — and read
the kernel's own record of the supervisor's parent, session and group.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The launcher child starts a supervisor whose argv is a sleep stub and reports
# the pid the launch returned, then stays alive so its own pid is a moving
# target the checks can compare against.
CHILD_LAUNCHER = """
import importlib
import sys
import time
from pathlib import Path

d = importlib.import_module("reckon.crew.dispatch")

d._supervisor_argv = lambda *, spec_path: [
    sys.executable,
    "-c",
    "import time; time.sleep(120)",
]

spec = Path(sys.argv[1])
directory = Path(sys.argv[2])
pid = d._start_supervisor(spec, directory, "r-stub")
print(pid, flush=True)
time.sleep(120)
"""


def _repo_root() -> Path:
    return Path(dispatch_module.__file__).resolve().parents[2]


def _proc_fields(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name, or None."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name may itself hold spaces and parentheses, so the fields
    # that follow begin after the final ')'.
    return raw[raw.rindex(")") + 2 :].split()


def _proc_state(pid: int) -> str | None:
    fields = _proc_fields(pid)
    return fields[0] if fields else None


def _proc_ids(pid: int) -> tuple[int, int, int]:
    """(ppid, pgrp, session) as the kernel holds them for a live pid."""
    fields = _proc_fields(pid)
    assert fields is not None, f"pid {pid} is gone"
    return int(fields[1]), int(fields[2]), int(fields[3])


def _alive(pid: int) -> bool:
    return _proc_state(pid) not in (None, "Z")


def _wait_until(pid: int, predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not reach the awaited state in {timeout:g}s")


@pytest.fixture()
def launched_supervisor(tmp_path: Path):
    """A supervisor launched from a long-lived child process.

    Yields (launcher_pid, supervisor_pid) with both processes still alive, and
    reaps every process it started once the test is done.
    """
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    spec_path = run_directory / "supervisor.json"
    spec_path.write_text("{}\n", encoding="utf-8")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_repo_root())
    environment["RECKON_FLEET_SPAWN"] = ""

    launcher = subprocess.Popen(
        [sys.executable, "-c", CHILD_LAUNCHER, str(spec_path), str(run_directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=environment,
    )
    supervisor_pid: int | None = None
    try:
        line = launcher.stdout.readline()
        assert line.strip(), f"the launcher reported no pid: {launcher.stderr.read()}"
        supervisor_pid = int(line.strip())
        _wait_until(supervisor_pid, lambda: _alive(supervisor_pid))
        yield launcher.pid, supervisor_pid
    finally:
        if supervisor_pid is not None:
            # The supervisor is not this process's child, so killing it and
            # waiting for the kernel to drop it is how it is reaped.
            with contextlib.suppress(ProcessLookupError):
                os.kill(supervisor_pid, signal.SIGKILL)
            with contextlib.suppress(AssertionError):
                _wait_until(supervisor_pid, lambda: not _alive(supervisor_pid))
        if launcher.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=10)
        launcher.stdout.close()
        launcher.stderr.close()


def test_supervisor_has_a_parent_other_than_the_launcher(launched_supervisor) -> None:
    launcher_pid, supervisor_pid = launched_supervisor
    assert _alive(supervisor_pid)
    ppid, _, _ = _proc_ids(supervisor_pid)
    assert ppid != launcher_pid


def test_supervisor_leads_its_own_session_and_group(launched_supervisor) -> None:
    _, supervisor_pid = launched_supervisor
    _, pgrp, session = _proc_ids(supervisor_pid)
    assert pgrp == supervisor_pid
    assert session == supervisor_pid


def test_supervisor_survives_a_signal_to_the_launchers_group(
    launched_supervisor,
) -> None:
    launcher_pid, supervisor_pid = launched_supervisor
    os.killpg(launcher_pid, signal.SIGTERM)
    time.sleep(0.5)
    assert _alive(supervisor_pid)