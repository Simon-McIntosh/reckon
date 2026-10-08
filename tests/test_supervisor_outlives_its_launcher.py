"""The per-run supervisor survives the process that launched it.

Off the fleet a dispatch starts the run's supervisor through a short-lived
intermediate, so the supervisor is never the launcher's child: when the launcher
ends — a coordinator turn finishing, a shell returning — the supervisor is not
in its process tree and the run it carries is not orphaned mid-flight.

The launcher must still return the pid ``crew stop`` signals, so the supervisor
is the leader of its own session and process group, with the worker spawned
inside that group. These tests start a supervisor from a long-lived child
process — a launcher that is still running when the checks are taken — and read
the kernel's own record of the supervisor's parent, session, group and open
descriptors.
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

import reckon.crew.dispatch_launch as dispatch_launch_module

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
launch = importlib.import_module("reckon.crew.dispatch_launch")

launch._supervisor_argv = lambda *, spec_path: [
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

# The same launcher, but holding an inheritable pipe across the spawn so the
# supervisor's open descriptors can be checked for a leak the launcher caused.
CHILD_LAUNCHER_WITH_PIPE = """
import importlib
import os
import sys
import time
from pathlib import Path

d = importlib.import_module("reckon.crew.dispatch")
launch = importlib.import_module("reckon.crew.dispatch_launch")

launch._supervisor_argv = lambda *, spec_path: [
    sys.executable,
    "-c",
    "import time; time.sleep(120)",
]

read_fd, write_fd = os.pipe()
os.set_inheritable(read_fd, True)
os.set_inheritable(write_fd, True)

spec = Path(sys.argv[1])
directory = Path(sys.argv[2])
pid = d._start_supervisor(spec, directory, "r-stub")
print(
    pid,
    os.readlink(f"/proc/self/fd/{read_fd}"),
    os.readlink(f"/proc/self/fd/{write_fd}"),
    flush=True,
)
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


def _descendant_pids(root: int) -> set[int]:
    """Every live pid whose ancestor chain reaches ``root``."""
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fields = _proc_fields(int(entry))
        if fields is not None:
            parents[int(entry)] = int(fields[1])
    descendants: set[int] = set()
    grew = True
    while grew:
        grew = False
        for pid, ppid in parents.items():
            if pid not in descendants and (ppid == root or ppid in descendants):
                descendants.add(pid)
                grew = True
    return descendants


def _fd_targets(pid: int) -> set[str]:
    """The link target of every open descriptor a live pid holds."""
    targets: set[str] = set()
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return targets
    for name in names:
        with contextlib.suppress(OSError):
            targets.add(os.readlink(f"/proc/{pid}/fd/{name}"))
    return targets


def _wait_until(predicate, *, timeout: float = 10.0, detail: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition {detail or predicate} not met in {timeout:g}s")


@contextlib.contextmanager
def _supervisor_from(script: str, tmp_path: Path):
    """Start a supervisor from a long-lived child running ``script``.

    Yields (launcher_pid, supervisor_pid, reported_tokens) with both processes
    still alive, and reaps every process it started once the caller is done.
    """
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    spec_path = run_directory / "supervisor.json"
    spec_path.write_text("{}\n", encoding="utf-8")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_repo_root())
    environment["RECKON_FLEET_SPAWN"] = ""

    launcher = subprocess.Popen(
        [sys.executable, "-c", script, str(spec_path), str(run_directory)],
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
        tokens = line.split()
        supervisor_pid = int(tokens[0])
        _wait_until(lambda: _alive(supervisor_pid), detail="the supervisor is alive")
        yield launcher.pid, supervisor_pid, tokens[1:]
    finally:
        if supervisor_pid is not None:
            # The supervisor is not this process's child, so killing it and
            # waiting for the kernel to drop it is how it is reaped.
            with contextlib.suppress(ProcessLookupError):
                os.kill(supervisor_pid, signal.SIGKILL)
            with contextlib.suppress(AssertionError):
                _wait_until(lambda: not _alive(supervisor_pid))
        if launcher.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=10)
        launcher.stdout.close()
        launcher.stderr.close()


@pytest.fixture()
def launched_supervisor(tmp_path: Path):
    """A supervisor launched from a child that reports only its pid."""
    with _supervisor_from(CHILD_LAUNCHER, tmp_path) as launched:
        yield launched


@pytest.fixture()
def launched_supervisor_with_pipe(tmp_path: Path):
    """A supervisor launched from a child holding an inheritable pipe."""
    with _supervisor_from(CHILD_LAUNCHER_WITH_PIPE, tmp_path) as launched:
        yield launched


def test_supervisor_has_a_parent_other_than_the_launcher(launched_supervisor) -> None:
    launcher_pid, supervisor_pid, _ = launched_supervisor
    assert _alive(supervisor_pid)
    ppid, _, _ = _proc_ids(supervisor_pid)
    assert ppid != launcher_pid


def test_supervisor_leads_its_own_session_and_group(launched_supervisor) -> None:
    _, supervisor_pid, _ = launched_supervisor
    _, pgrp, session = _proc_ids(supervisor_pid)
    assert pgrp == supervisor_pid
    assert session == supervisor_pid


def test_supervisor_survives_a_kill_of_the_launchers_process_tree(
    launched_supervisor,
) -> None:
    launcher_pid, supervisor_pid, _ = launched_supervisor
    tree = _descendant_pids(launcher_pid)
    # The launcher must have descendants for the kill to mean anything: with the
    # supervisor reparented away, an empty tree would prove nothing.
    assert launcher_pid not in tree
    for pid in tree | {launcher_pid}:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    _wait_until(
        lambda: not _alive(launcher_pid), detail="the launcher's tree is gone"
    )
    time.sleep(0.5)
    assert _alive(supervisor_pid), "the supervisor died with the launcher's tree"


def test_supervisor_inherits_no_launcher_descriptor(
    launched_supervisor_with_pipe,
) -> None:
    launcher_pid, supervisor_pid, pipe_targets = launched_supervisor_with_pipe
    assert len(pipe_targets) == 2, pipe_targets
    # Positive control: the pipe is live in the launcher, so its absence from the
    # supervisor is the descriptor having been closed and not a pipe that was
    # never opened.
    launcher_fds = _fd_targets(launcher_pid)
    assert set(pipe_targets) <= launcher_fds, (pipe_targets, launcher_fds)
    supervisor_fds = _fd_targets(supervisor_pid)
    assert not (set(pipe_targets) & supervisor_fds), (
        f"the supervisor inherited the launcher's pipe descriptors: "
        f"{set(pipe_targets) & supervisor_fds}"
    )


def test_launcher_that_reports_no_pid_is_refused(tmp_path, monkeypatch) -> None:
    # An intermediate that exits without printing stands in for one that failed
    # before it could start a supervisor; its own stderr is the only account of
    # why, so the refusal must carry it.
    monkeypatch.setattr(
        dispatch_launch_module,
        "_SUPERVISOR_LAUNCHER_SOURCE",
        "import sys; sys.stderr.write('no supervisor argv\\n'); sys.exit(3)",
    )
    with pytest.raises(dispatch_module.CrewError) as error:
        dispatch_module._spawn_detached_supervisor(
            ["true"], tmp_path / "supervisor.stderr.log"
        )
    message = str(error.value)
    assert "exited without reporting a pid" in message
    assert "no supervisor argv" in message
    assert "3" in message
