"""The batch step runs the services a user config declares.

Every case points the configuration home, the runtime directory and the state
directory at a temporary tree, so no case reads or writes the machine's own
fleet config, runtime or state. A synthetic service proves each property: it
records its pid in a directory (one file per copy, so copies ever started are
countable), writes to both output streams, and stays up unless its mode says
to exit. Time enters through an injectable clock, so no backoff case sleeps.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import fleet_supervisor
from reckon.crew.host_lease import LEASE_STALE_SECONDS, HostLease

REPO_ROOT = str(Path(fleet_supervisor.__file__).resolve().parents[2])
MODULE_ARGV = [sys.executable, "-m", "reckon.crew.fleet_supervisor"]
WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05

# The synthetic service. argv[1] is a directory it records its pid in, argv[2]
# is the mode: "run" stays up, "exit-once" ends its first copy only (so the
# restart is observable as a live second copy), "flood" writes both streams
# past a small bound and stays up. Every mode ends on SIGTERM.
SERVICE_SOURCE = (
    "import os, signal, sys, time\n"
    "instances, mode = sys.argv[1], sys.argv[2]\n"
    "os.makedirs(instances, exist_ok=True)\n"
    "open(os.path.join(instances, str(os.getpid())), 'w', encoding='utf-8')"
    ".write(mode)\n"
    "print('stdout tick', flush=True)\n"
    "sys.stderr.write('stderr tick\\n')\n"
    "sys.stderr.flush()\n"
    "if mode == 'exit-once' and len(os.listdir(instances)) == 1:\n"
    "    raise SystemExit(0)\n"
    "if mode == 'flood':\n"
    "    chunk = 'x' * 79 + '\\n'\n"
    "    for _ in range(300):\n"
    "        sys.stdout.write('out ' + chunk)\n"
    "        sys.stderr.write('err ' + chunk)\n"
    "    sys.stdout.flush()\n"
    "    sys.stderr.flush()\n"
    "    print('OUTPUT-DONE', flush=True)\n"
    "    print('ERROR-DONE', file=sys.stderr, flush=True)\n"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


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
    """The pids recorded by every service copy started so far."""
    if not directory.exists():
        return []
    pids = []
    for entry in directory.iterdir():
        with suppress(ValueError):
            pids.append(int(entry.name))
    return sorted(pids)


def _wait_for_exit(pid: int, *, timeout: float = WAIT_SECONDS) -> None:
    """Wait for a child of this process to end, and collect it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped == pid:
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(f"pid {pid} did not exit within {timeout}s")


def _write_services(home, services: dict[str, list[str]]) -> Path:
    path = home.config_home / "fleet" / "services.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(services), encoding="utf-8")
    return path


@pytest.fixture()
def home(tmp_path):
    """A fleet home in a temporary tree, with a synthetic service to declare."""
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
    yield home
    for pid in _instances(instances):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with suppress(ChildProcessError):
            os.waitpid(pid, 0)
    survivors = [pid for pid in _instances(instances) if _alive(pid)]
    if survivors:
        pytest.fail(f"service children survived the case: {survivors}")


def test_a_declared_service_starts_as_its_own_session_holding_its_lock(home):
    """A declared service leads its own session and holds the lock while up.

    The same lock probe reads closed before the start and held while the copy
    runs, so "held" is not merely "a file exists".
    """
    assert fleet_supervisor.lock_held("demo", home.runtime) is False
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    pid = manager.start("demo", home.argv("run"))
    assert pid is not None
    _wait_for(
        lambda: _instances(home.instances),
        message="the declared service never recorded its pid, so it did not run",
    )
    assert os.getsid(pid) == pid, "the service does not lead its own session"
    assert fleet_supervisor.lock_held("demo", home.runtime) is True
    assert fleet_supervisor.recorded_service_pid(home.runtime, "demo") == pid
    assert _alive(pid)
    lease_path = HostLease(home.state, "demo", "reader", 0, "").path
    assert lease_path.exists()

    manager.stop("demo")
    _wait_for(lambda: not _alive(pid), message="the service survived its stop")
    assert fleet_supervisor.lock_held("demo", home.runtime) is False
    assert not lease_path.exists()


def test_stop_recovers_a_running_service_from_its_recorded_pid(home):
    """A lost in-memory state still leaves a stop path through the local lock."""
    _write_services(home, {"demo": home.argv("run")})
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    manager.reload()
    pid = _wait_for(
        lambda: _instances(home.instances) or None,
        message="the synthetic service never started",
    )[0]
    lease_path = HostLease(home.state, "demo", "reader", 0, "").path
    assert manager._states.pop("demo").pid == pid
    assert fleet_supervisor.recorded_service_pid(home.runtime, "demo") == pid
    assert fleet_supervisor.lock_held("demo", home.runtime)
    assert lease_path.exists()

    manager.stop("demo")
    _wait_for(
        lambda: not _alive(pid),
        message="the recorded service survived stop after in-memory state was lost",
        timeout=1.0,
    )
    assert not lease_path.exists()


def test_a_second_start_or_reload_starts_no_second_copy(home):
    """The lock, not bookkeeping, refuses a second copy of a running service.

    A second start is launched exactly as another supervisor would launch it,
    through the same wrapper command, and a reload is run in a fresh image; the
    running copy must be the only copy either way. With the lock check removed
    from the wrapper, this case starts a second copy and fails.
    """
    _write_services(home, {"demo": home.argv("run")})
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    manager.reload()
    first_pid = _wait_for(
        lambda: _instances(home.instances) or None,
        message="the declared service never started",
    )[0]

    # A fresh image, as after a re-exec, adopts the running copy.
    second = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    second.reload()
    assert _instances(home.instances) == [first_pid]

    # A start attempted anyway, by a supervisor that believes nothing runs, is
    # refused by the lock the running copy holds.
    log = home.state / "services" / "demo.log"
    with open(log, "ab") as sink:
        clash = subprocess.Popen(
            fleet_supervisor.service_wrapper_argv("demo", home.argv("run")),
            env=home.env,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
        )
    assert clash.wait(timeout=WAIT_SECONDS) == 1, (
        "the second start was not refused, so it began a second copy"
    )
    assert _instances(home.instances) == [first_pid], (
        "a second copy of a running service appeared"
    )
    assert _alive(first_pid)

    # A reload in the same image starts nothing either.
    manager.reload()
    time.sleep(1.0)
    assert _instances(home.instances) == [first_pid]
    text = log.read_text(encoding="utf-8")
    assert text.count("stdout tick") == 1, (
        "a second copy wrote its own start marker into the service log"
    )


def test_two_hosts_share_one_declared_service(home, capsys, monkeypatch):
    """Separate runtime locks still yield one copy through shared state."""
    other_runtime = home.tmp_path / "other-runtime"
    other_runtime.mkdir()
    _write_services(home, {"demo": home.argv("run")})
    monkeypatch.setattr(fleet_supervisor, "_short_hostname", lambda: "host-one")
    first = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    monkeypatch.setattr(fleet_supervisor, "_short_hostname", lambda: "host-two")
    other_env = {**home.env, "FLEET_RUNTIME_DIR": str(other_runtime)}
    second = fleet_supervisor.DeclaredServices(other_runtime, other_env)
    try:
        first.reload()
        first_pid = _wait_for(
            lambda: _instances(home.instances) or None,
            message="the first host did not start the synthetic service",
        )[0]
        second.reload()
        second.supervise_once()
        assert fleet_supervisor.recorded_service_pid(other_runtime, "demo") is None
        assert _instances(home.instances) == [first_pid]
        assert "host-one" in capsys.readouterr().out
    finally:
        second.stop_all()
        first.stop_all()


def test_stale_service_lease_moves_to_second_host(home, monkeypatch):
    """A dead holder can be replaced without losing the successor's lease."""
    other_runtime = home.tmp_path / "other-runtime"
    other_runtime.mkdir()
    _write_services(home, {"demo": home.argv("run")})
    monkeypatch.setattr(fleet_supervisor, "_short_hostname", lambda: "host-one")
    first = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    monkeypatch.setattr(fleet_supervisor, "_short_hostname", lambda: "host-two")
    other_env = {**home.env, "FLEET_RUNTIME_DIR": str(other_runtime)}
    second = fleet_supervisor.DeclaredServices(other_runtime, other_env)
    try:
        first.reload()
        first_pid = _wait_for(
            lambda: _instances(home.instances) or None,
            message="the first host did not start the synthetic service",
        )[0]
        path = HostLease(home.state, "demo", "reader", 0, "").path
        stale = time.time() - LEASE_STALE_SECONDS - 1
        os.utime(path, (stale, stale))
        second.reload()
        second_pid = fleet_supervisor.recorded_service_pid(other_runtime, "demo")
        assert second_pid is not None and second_pid != first_pid
        _wait_for(
            lambda: second_pid in _instances(home.instances),
            message="the second host did not take over the stale lease",
        )
        first.supervise_once(
            now=time.monotonic() + fleet_supervisor.LEASE_RENEW_SECONDS
        )
        _wait_for(
            lambda: not _alive(first_pid), message="the displaced service stayed up"
        )
        assert json.loads(path.read_text(encoding="utf-8"))["host"] == "host-two"
        assert _alive(second_pid)
        second.stop_all()
        assert not path.exists()
    finally:
        second.stop_all()
        first.stop_all()


def test_a_service_that_exits_restarts_after_the_backoff_and_not_before(home):
    """An exited service restarts on the backoff, and not before it.

    The clock is driven by hand, so the case never waits five real seconds:
    the exit is observed at t=1, no copy exists at t=5.9, and the restarted
    copy appears once the tick at t=6.1 passes the five-second wait.
    """
    clock = FakeClock(0.0)
    _write_services(home, {"demo": home.argv("exit-once")})
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env, clock=clock)
    manager.reload()
    first_pid = _wait_for(
        lambda: _instances(home.instances) or None,
        message="the declared service never started",
    )[0]
    _wait_for_exit(first_pid)

    clock.now = 1.0
    manager.supervise_once()
    clock.now = 5.9
    manager.supervise_once()
    assert _instances(home.instances) == [first_pid], (
        "a second copy started before the backoff elapsed"
    )

    clock.now = 6.1
    manager.supervise_once()

    def two_copies():
        pids = _instances(home.instances)
        return pids if len(pids) == 2 else None

    pids = _wait_for(
        two_copies, message="the service was not restarted once the backoff elapsed"
    )
    second_pid = next(pid for pid in pids if pid != first_pid)
    _wait_for(
        partial(_alive, second_pid),
        message="the restarted copy did not stay up",
    )
    assert not _alive(first_pid)

    log = home.state / "services" / "demo.log"
    _wait_for(
        lambda: (
            log.exists() and log.read_text(encoding="utf-8").count("stdout tick") >= 2
        ),
        message="the restarted copy's output was not appended to the same log",
    )


def test_a_reload_starts_a_newly_declared_service_and_stops_the_removed_one(home):
    """A reload applies the config: the new name starts, the old one is stopped.

    The removed service is stopped through the pid the supervisor recorded for
    it -- the pid file the start wrote -- while the service the reload newly
    declared keeps running, so a stop that took everything, or one aimed by a
    pattern, would not read like this.
    """
    manager = fleet_supervisor.DeclaredServices(home.runtime, home.env)
    _write_services(home, {"first": home.argv("run")})
    manager.reload()
    first_pid = _wait_for(
        lambda: _instances(home.instances) or None,
        message="the declared service never started",
    )[0]
    assert fleet_supervisor.recorded_service_pid(home.runtime, "first") == first_pid

    _write_services(home, {"second": home.argv("run")})
    manager.reload()

    def two_copies():
        pids = _instances(home.instances)
        return pids if len(pids) == 2 else None

    pids = _wait_for(two_copies, message="the newly declared service did not start")
    second_pid = next(pid for pid in pids if pid != first_pid)
    _wait_for(
        lambda: not _alive(first_pid), message="the removed service was not stopped"
    )
    assert _alive(second_pid), "the newly declared service did not survive the reload"
    assert not fleet_supervisor.service_pid_path(home.runtime, "first").exists()
    assert fleet_supervisor.lock_held("first", home.runtime) is False
    assert fleet_supervisor.lock_held("second", home.runtime) is True


def test_the_health_sampler_still_starts_beside_a_declared_service(home):
    """The sampler starts as before, and the supervisor runs declared services.

    Both children are observed in one run of the real batch step: the sampler
    stub records the runtime it was handed, and the declared service records
    its own pid, so neither observation stands in for the other.
    """
    marker = home.tmp_path / "sampler-ran"
    stub = home.tmp_path / "sampler"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "runtime %s\\n" "$XDG_RUNTIME_DIR" > "$RECKON_SAMPLER_MARKER"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    _write_services(home, {"demo": home.argv("run")})
    log = home.tmp_path / "reader.log"
    with open(log, "ab") as sink:
        reader = subprocess.Popen(
            MODULE_ARGV,
            cwd=REPO_ROOT,
            env={
                **home.env,
                "FLEET_HEALTH_SAMPLER": str(stub),
                "RECKON_SAMPLER_MARKER": str(marker),
            },
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
        )
        try:
            _wait_for(marker.exists, message="the health sampler did not start")
            _wait_for(
                lambda: _instances(home.instances),
                message="the supervisor did not start the declared service",
            )
            assert reader.poll() is None, "the supervisor died while starting services"
            assert (home.runtime / "requests").exists()
            assert f"runtime {home.runtime}" in marker.read_text(encoding="utf-8")
        finally:
            service_pid = fleet_supervisor.recorded_service_pid(home.runtime, "demo")
            if reader.poll() is None:
                reader.kill()
                reader.wait(timeout=10)
            if service_pid is not None:
                with suppress(ProcessLookupError):
                    os.kill(service_pid, signal.SIGKILL)
                _wait_for(
                    lambda: not _alive(service_pid),
                    message="the declared service survived the supervisor's end",
                )


def test_each_services_stdout_and_stderr_append_to_its_size_bounded_log(home):
    """Both output streams land in one log, and the log stays bounded.

    The flood copy writes past a small bound first, so the instrument is shown
    reading a log above the bound before the tick trims it; the newest markers
    must survive the trim, which is the part a later reader opens the log for.
    """
    limit, keep = 4096, 2048
    manager = fleet_supervisor.DeclaredServices(
        home.runtime, home.env, log_limit=limit, log_keep=keep
    )
    _write_services(home, {"demo": home.argv("flood")})
    manager.reload()
    log_path = fleet_supervisor.service_log_directory(home.env) / "demo.log"

    def both_streams_done():
        if not log_path.exists():
            return None
        text = log_path.read_text(encoding="utf-8")
        return text if "OUTPUT-DONE" in text and "ERROR-DONE" in text else None

    _wait_for(both_streams_done, message="both output streams never reached the log")
    grown = log_path.stat().st_size
    assert grown > limit, (
        f"the flood left the log at {grown} bytes, not above the {limit}-byte "
        "bound, so the bound was never exercised"
    )

    manager.supervise_once()
    trimmed = log_path.stat().st_size
    assert trimmed <= limit, (
        f"the log stayed at {trimmed} bytes against a {limit}-byte bound"
    )
    tail = log_path.read_text(encoding="utf-8")
    assert "OUTPUT-DONE" in tail and "ERROR-DONE" in tail, (
        "the trim dropped the newest output instead of the oldest"
    )


def test_the_backoff_doubles_to_its_cap_and_resets_after_steady_running():
    delay = fleet_supervisor.FIRST_BACKOFF_SECONDS
    waits = []
    for _ in range(8):
        wait, delay = fleet_supervisor.next_backoff(delay, 0.0)
        waits.append(wait)
    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]
    assert fleet_supervisor.next_backoff(
        300.0, fleet_supervisor.STEADY_RUN_SECONDS
    ) == (5.0, 10.0)
    assert fleet_supervisor.next_backoff(
        80.0, fleet_supervisor.STEADY_RUN_SECONDS - 1.0
    ) == (80.0, 160.0)
