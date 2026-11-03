"""A service whose lock file vanishes while it runs keeps exactly one copy.

Every case points the configuration home, the runtime directory and the state
directory at a temporary tree, and asserts that the machine's own runtime and
state directories are untouched. A synthetic service records itself in a
directory, one file per copy, so the number of copies is countable rather than
inferred from a lock.

The failure this guards is measured: two adopted services lost their lock and
pid files under the fleet runtime, the supervisor read the missing lock path as
an exited service, and it started second copies that ran beside the originals.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import fleet_supervisor

REPO_ROOT = str(Path(fleet_supervisor.__file__).resolve().parents[2])

SERVICE_SOURCE = (
    "import os, signal, sys, time\n"
    "instances, mode = sys.argv[1], sys.argv[2]\n"
    "os.makedirs(instances, exist_ok=True)\n"
    "open(os.path.join(instances, str(os.getpid())), 'w', encoding='utf-8')"
    ".write(mode)\n"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)
WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05


class FakeClock:
    """A clock the case advances by hand, so no backoff waits in real time."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _wait_for(predicate, *, message: str, timeout: float = WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_SECONDS)
    pytest.fail(message)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _instances(directory: Path) -> list[int]:
    if not directory.exists():
        return []
    pids = []
    for entry in directory.iterdir():
        with suppress(ValueError):
            pids.append(int(entry.name))
    return sorted(pids)


def _write_services(home, services: dict[str, list[str]]) -> None:
    path = home.config_home / "fleet" / "services.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(services), encoding="utf-8")


def _start(home) -> tuple[fleet_supervisor.DeclaredServices, int]:
    _write_services(home, {"demo": home.argv("run")})
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    manager.reload()
    pid = _wait_for(
        lambda: _instances(home.instances) or None,
        message="the declared service never started",
    )[0]
    _wait_for(
        lambda: fleet_supervisor.lock_held("demo", home.runtime),
        message="the service never took its lock",
    )
    # The positive control for the untouched assertion above: this case's
    # service files land in the temporary tree, and never in the machine's own.
    assert fleet_supervisor.service_lock_path(home.runtime, "demo").exists()
    assert fleet_supervisor.service_pid_path(home.runtime, "demo").exists()
    for directory in _real_directories():
        assert not _service_entries(directory), f"{directory} carries this case's files"
    return manager, pid


def _real_directories() -> tuple[Path, Path]:
    """The machine's own runtime and state directories, as this test sees them."""
    return fleet_supervisor.runtime_directory(), fleet_supervisor.state_directory()


def _service_entries(directory: Path) -> set[str]:
    """This case's service files in a directory, if the case wrote there."""
    if not directory.exists():
        return set()
    return {
        entry.name for entry in directory.iterdir() if entry.name.startswith("demo")
    }


@pytest.fixture()
def home(tmp_path):
    config_home = tmp_path / "config-home"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    instances = tmp_path / "instances"
    for directory in (config_home / "fleet", runtime, state):
        directory.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "service.py"
    script.write_text(SERVICE_SOURCE, encoding="utf-8")
    env = {
        **os.environ,
        "FLEET_RUNTIME_DIR": str(runtime),
        "FLEET_STATE_DIR": str(state),
        "XDG_CONFIG_HOME": str(config_home),
        "PYTHONPATH": REPO_ROOT,
    }
    home = SimpleNamespace(
        tmp_path=tmp_path,
        config_home=config_home,
        runtime=runtime,
        state=state,
        instances=instances,
        env=env,
        argv=lambda mode: [sys.executable, str(script), str(instances), mode],
    )
    before = [_service_entries(directory) for directory in _real_directories()]
    yield home
    for pid in _instances(instances):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with suppress(ChildProcessError):
            os.waitpid(pid, 0)
    survivors = [pid for pid in _instances(instances) if _alive(pid)]
    if survivors:
        pytest.fail(f"service copies survived the case: {survivors}")
    after = [_service_entries(directory) for directory in _real_directories()]
    assert after == before, "the case wrote the machine's own runtime or state"


def test_a_vanished_lock_file_starts_no_second_copy(home):
    """An adopted copy whose lock and pid files are deleted is still the copy.

    The supervisor is replaced by a fresh image, as after a re-exec, so the
    running copy is adopted rather than owned by this process. Deleting its
    lock and pid files then leaves nothing but the recorded pid and the copy's
    own command line to say it runs, and three supervision passes must start
    no second copy. With liveness read from the lock alone, the first pass
    starts a second copy and this case fails.
    """
    _, pid = _start(home)
    clock = FakeClock()
    adopting = fleet_supervisor.DeclaredServices(home.runtime, home.env, clock=clock)
    adopting.reload()
    assert adopting._states["demo"].pid == pid
    assert adopting._states["demo"].owned is False, "the copy was not adopted"

    fleet_supervisor.service_lock_path(home.runtime, "demo").unlink()
    fleet_supervisor.service_pid_path(home.runtime, "demo").unlink()

    # Three passes well past any backoff: a restart that was going to happen
    # has happened by the third.
    for _ in range(3):
        clock.now += fleet_supervisor.BACKOFF_CAP_SECONDS
        adopting.supervise_once(now=clock.now)
    _wait_for(
        lambda: [copy for copy in _instances(home.instances) if _alive(copy)] == [pid],
        message="a second copy was started",
        timeout=2.0,
    )

    alerts = (home.state / fleet_supervisor.ALERTS_NAME).read_text(encoding="utf-8")
    assert "demo" in alerts
    assert "vanished" in alerts


def test_a_second_copy_is_stopped_and_the_recorded_copy_survives(home):
    """A copy started outside the supervisor is stopped by pid.

    The extra copy carries the same declared argv, so the supervision pass
    finds it beside the recorded copy, logs the list, and stops it by that pid
    while the recorded copy is kept.
    """
    _, recorded = _start(home)
    extra = subprocess.Popen(
        home.argv("run"),
        env=home.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for(
        lambda: len(_instances(home.instances)) >= 2,
        message="the outside copy never started",
    )
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    manager.reload()
    assert manager._states["demo"].pid == recorded

    manager.supervise_once()
    _wait_for(
        lambda: extra.poll() is not None,
        message="the extra copy was not stopped",
        timeout=5.0,
    )
    assert _alive(recorded), "the recorded copy was stopped instead"
    assert _instances(home.instances) == [recorded, extra.pid]


def test_a_service_that_really_ended_is_restarted_after_its_backoff(home):
    """A copy whose process is gone is restarted, but only once its backoff is due.

    The process is ended and its exit is collected, so the recorded pid names
    nothing and the lock is free. The first pass schedules the restart, a pass
    before the delay starts nothing, and the pass at the delay starts a new
    copy. Reading the recorded pid as alive on the strength of the record alone
    would never restart it, and this case fails.
    """
    clock = FakeClock()
    _write_services(home, {"demo": home.argv("run")})
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env, clock=clock)
    manager.reload()
    first = _wait_for(
        lambda: _instances(home.instances) or None,
        message="the declared service never started",
    )[0]

    os.kill(first, signal.SIGTERM)
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(first, os.WNOHANG)
        except ChildProcessError:
            break
        if reaped == first:
            break
        time.sleep(POLL_SECONDS)
    else:
        pytest.fail("the service did not exit")

    manager.supervise_once(now=clock.now)
    assert manager._states["demo"].next_attempt is not None
    manager.supervise_once(now=clock.now + fleet_supervisor.FIRST_BACKOFF_SECONDS / 2)
    assert _instances(home.instances) == [first], (
        "a copy was started before its backoff"
    )

    clock.now += fleet_supervisor.FIRST_BACKOFF_SECONDS + 1
    manager.supervise_once(now=clock.now)
    second = _wait_for(
        lambda: [pid for pid in _instances(home.instances) if pid != first] or None,
        message="the ended service was never restarted",
    )[0]
    assert _alive(second)
    assert second != first


def test_a_recreated_lock_file_records_its_new_inode(home, monkeypatch):
    """A lock file remade between two starts records its new inode.

    The first copy records the inode of the lock file it held. That file is
    deleted, a new one is made in its place, and the next start must record the
    inode now on disk beside the new pid. The wrapper is replaced by the
    declared argv so the record under test is the supervisor's own, and with
    the previous inode preferred this case fails. The assertion is on the
    live file's inode rather than on the two inodes differing: a filesystem
    may hand the freed inode straight back, and a case that required a
    different number would red on a correct record.
    """
    manager, first = _start(home)
    first_inode = fleet_supervisor.recorded_service_lock_inode(home.runtime, "demo")
    assert first_inode is not None, "the first copy recorded no lock inode"

    # The copy ends without the supervisor clearing its record, as when a
    # service exits and the lock file is deleted and remade before the restart.
    os.kill(first, signal.SIGTERM)
    with suppress(ChildProcessError):
        os.waitpid(first, 0)
    assert not _alive(first), "the first copy did not exit"

    lock_path = fleet_supervisor.service_lock_path(home.runtime, "demo")
    lock_path.unlink()
    lock_path.write_text("", encoding="utf-8")
    new_inode = lock_path.stat().st_ino

    monkeypatch.setattr(
        fleet_supervisor, "service_wrapper_argv", lambda name, argv: list(argv)
    )
    second = manager.start("demo", home.argv("run"))
    assert second is not None
    assert fleet_supervisor.recorded_service_pid(home.runtime, "demo") == second
    assert (
        fleet_supervisor.recorded_service_lock_inode(home.runtime, "demo") == new_inode
    )
