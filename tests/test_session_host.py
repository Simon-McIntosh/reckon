"""The session host runs one follower per project and session for one session.

The host is driven as a real process against a fake follower command, and every
case observes a process or a file rather than a return value: a child exists, a
killed child comes back, a hand-armed follower is not contended with, the pane
carries the follower's lines and nothing the host wrote, and no child outlives
the host's SIGTERM or its owner's exit.

Every absence here is preceded by showing the same instrument reading a
known-present value: a project with no hand-armed follower starts a child, and
the stdout check shows the follower's own line is visible through the same
predecessor that must find no host line beside it.

The fake follower is a script the host runs through ``--follower-command``; it
appends its pid to a marker file and prints one line, so a restart is a second
line with a new pid, and the host's stdout can be read as the follower's line
and nothing else. Owner identity is supplied as ``--owner-pid`` and
``--owner-start`` so a case can end the owner without ending the test.
"""

from __future__ import annotations

import json
import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fast cadences so a case does not spend the restart backoff or the owner tick
# waiting on a wall clock: the host reads these from its own environment.
POLL_SECONDS = "0.2"
BACKOFF_SECONDS = "0.2"

START_BOUND = 10.0
STOP_BOUND = 15.0
RESTART_BOUND = 15.0

FOLLOWER_LINE = "FAKE-FOLLOWER-LINE"

FAKE = (
    "import os, sys, time\n"
    "marker = os.environ.get('FAKE_FOLLOWER_MARKER')\n"
    "if marker:\n"
    "    with open(marker, 'a', encoding='utf-8') as handle:\n"
    "        handle.write(f'{os.getpid()}\\n')\n"
    "print('FAKE-FOLLOWER-LINE', flush=True)\n"
    "while True:\n"
    "    time.sleep(0.1)\n"
)

# A live registration the host did not start: it takes the same advisory lock a
# real follower takes and holds it, with a terminal on stdout so its lines read
# as delivering.
HAND_ARMED = (
    "import sys, time\n"
    "from reckon.crew.runs import _FollowerRegistration\n"
    "reg = _FollowerRegistration(sys.argv[1], sys.argv[2], delivery='stream')\n"
    "sys.exit(3) if not reg.acquire() else None\n"
    "print('ARMED', flush=True)\n"
    "while True:\n"
    "    time.sleep(0.1)\n"
)


def _module():
    """The session host module as this gate imported it."""
    from importlib import import_module

    return import_module("reckon.crew.session_host")


def _console_script() -> str:
    """The reckon console script the host is driven through."""
    return _module()._reckon_console_script()


def _start_time(pid: int) -> str:
    """The kernel start tick for a pid, read the way the host reads it."""
    return _module().process_start_time(pid) or ""


def _process_alive(pid: int) -> bool:
    """Whether a pid still runs; a zombie reads as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    state = fields[fields.rfind(")") + 1 :].split()
    return bool(state) and state[0] != "Z"


def _follower_live(env: dict[str, str], project: str, session: str) -> bool:
    """Read a follower registration's liveness under the host's own config home.

    The helper process and the host resolve their state directory from the
    environment, so the liveness of the registration they hold is read the same
    way, in a process carrying that environment rather than this test's.
    """
    script = (
        "import json, sys; from reckon.crew.runs import follower_state; "
        "print(json.dumps(follower_state(sys.argv[1], sys.argv[2])['live']))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            project,
            session,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.stdout.strip() == "true"


def _wait_for(predicate, *, timeout: float, description: str) -> None:
    """Wait until the predicate holds, or fail naming what was awaited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


class OwnerStub:
    """A stand-in for the Claude process a session host lives for."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self.process.pid
        deadline = time.monotonic() + START_BOUND
        self.start_time = _start_time(self.pid)
        while not self.start_time and time.monotonic() < deadline:
            time.sleep(0.01)
            self.start_time = _start_time(self.pid)
        assert self.start_time, "owner stub has no readable start time"

    def end(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


class HandArmedFollower:
    """A live follower registration this host did not start."""

    def __init__(self, project: str, session: str, env: dict[str, str]) -> None:
        self.master, slave = pty.openpty()
        self.project = project
        self.session = session
        script = Path(env["FAKE_FOLLOWER_MARKER"]).parent / "hand_armed.py"
        script.write_text(HAND_ARMED, encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(script), project, session],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        os.close(slave)
        self._ended = False

    def end(self) -> None:
        """Stop the follower and release its pty; safe to call more than once."""
        if self._ended:
            return
        self._ended = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        os.close(self.master)


class HostHarness:
    """A running session host and everything a case reads about it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.marker = tmp_path / "children.txt"
        self.fake_follower = tmp_path / "fake_follower.py"
        self.fake_follower.write_text(FAKE, encoding="utf-8")
        self.state_dir = tmp_path / "state"
        self.log_dir = tmp_path / "logs"
        self.stdout = tmp_path / "host.stdout"
        self.stderr = tmp_path / "host.stderr"
        self.owner = OwnerStub()
        self.process: subprocess.Popen | None = None
        self._stdout_handle = None
        self._stderr_handle = None
        self._extra: list = []

    def env(self) -> dict[str, str]:
        environ = dict(os.environ)
        environ["PYTHONPATH"] = str(REPO_ROOT)
        environ["RECKON_HOME"] = str(self.tmp_path / "home")
        environ["RECKON_SESSION_HOST_STATE_DIR"] = str(self.state_dir)
        environ["RECKON_SESSION_HOST_LOG_DIR"] = str(self.log_dir)
        environ["RECKON_SESSION_HOST_POLL_SECONDS"] = POLL_SECONDS
        environ["RECKON_SESSION_HOST_BACKOFF"] = BACKOFF_SECONDS
        environ["FAKE_FOLLOWER_MARKER"] = str(self.marker)
        return environ

    def start(self) -> None:
        self._stdout_handle = self.stdout.open("wb")
        self._stderr_handle = self.stderr.open("wb")
        argv = [
            _console_script(),
            "crew",
            "host",
            "--owner-pid",
            str(self.owner.pid),
            "--owner-start",
            self.owner.start_time,
            "--follower-command",
            json.dumps([sys.executable, str(self.fake_follower)]),
        ]
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            env=self.env(),
        )

    def request(self, project: str, session: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        line = json.dumps({"project": project, "session": session}) + "\n"
        self.process.stdin.write(line.encode())
        self.process.stdin.flush()

    def child_pids(self) -> list[int]:
        if not self.marker.exists():
            return []
        return [
            int(line)
            for line in self.marker.read_text(encoding="utf-8").split()
            if line.strip()
        ]

    def arm(self, project: str, session: str) -> HandArmedFollower:
        env = self.env()
        follower = HandArmedFollower(project, session, env)
        self._extra.append(follower)
        deadline = time.monotonic() + START_BOUND
        while not _follower_live(env, project, session):
            if time.monotonic() >= deadline:
                pytest.fail("hand-armed follower never registered as live")
            time.sleep(0.05)
        return follower

    def stop(self) -> None:
        for extra in self._extra:
            extra.end()
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=STOP_BOUND)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                handle.close()
        self.owner.end()

    def stdout_text(self) -> str:
        return self.stdout.read_text(encoding="utf-8") if self.stdout.exists() else ""


@pytest.fixture()
def host(tmp_path: Path):
    harness = HostHarness(tmp_path)
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


def test_one_child_per_project_and_session(host: HostHarness) -> None:
    host.request("alpha", "sess")
    _wait_for(
        lambda: len(host.child_pids()) == 1, timeout=START_BOUND, description="a child"
    )
    host.request("alpha", "sess")
    host.request("alpha", "sess")
    time.sleep(0.5)
    assert len(host.child_pids()) == 1, host.child_pids()


def test_a_second_project_or_session_gets_its_own_child(host: HostHarness) -> None:
    # The positive control for the count above: the same reader that found one
    # child for one pair finds three for three distinct pairs.
    host.request("alpha", "sess")
    host.request("alpha", "other")
    host.request("beta", "sess")
    _wait_for(
        lambda: len(host.child_pids()) == 3,
        timeout=START_BOUND,
        description="three children",
    )
    assert len(set(host.child_pids())) == 3, host.child_pids()


def test_a_killed_child_is_restarted(host: HostHarness) -> None:
    host.request("alpha", "sess")
    _wait_for(
        lambda: len(host.child_pids()) >= 1, timeout=START_BOUND, description="a child"
    )
    first = host.child_pids()[0]
    os.kill(first, signal.SIGKILL)
    _wait_for(
        lambda: len(host.child_pids()) >= 2,
        timeout=RESTART_BOUND,
        description="a restarted child",
    )
    assert host.child_pids()[1] != first, host.child_pids()


def test_a_hand_armed_live_follower_is_left_alone(host: HostHarness) -> None:
    follower = host.arm("alpha", "sess")
    host.request("alpha", "sess")
    # The positive control: a pair with no hand-armed follower does start one
    # child through the same request path, so a quiet marker is a skip and not
    # a request that never arrived.
    host.request("beta", "sess")
    _wait_for(
        lambda: len(host.child_pids()) >= 1,
        timeout=START_BOUND,
        description="the control child",
    )
    time.sleep(0.5)
    assert len(host.child_pids()) == 1, host.child_pids()
    assert _process_alive(follower.process.pid), "the hand-armed follower was killed"


def test_the_host_writes_nothing_to_stdout(host: HostHarness) -> None:
    host.request("alpha", "sess")
    _wait_for(
        lambda: host.stdout_text().count(FOLLOWER_LINE) >= 1,
        timeout=START_BOUND,
        description="the follower line",
    )
    time.sleep(0.3)
    # The follower's line is visible through this reader, so an empty reading
    # would be the host adding nothing beside a child that did speak.
    assert host.stdout_text().strip() == FOLLOWER_LINE, repr(host.stdout_text())


def test_no_child_survives_the_hosts_sigterm(host: HostHarness) -> None:
    host.request("alpha", "sess")
    _wait_for(
        lambda: len(host.child_pids()) >= 1, timeout=START_BOUND, description="a child"
    )
    child = host.child_pids()[0]
    assert _process_alive(child), "the child never started"
    assert host.process is not None
    host.process.send_signal(signal.SIGTERM)
    host.process.wait(timeout=STOP_BOUND)
    _wait_for(
        lambda: not _process_alive(child),
        timeout=STOP_BOUND,
        description="the child to end",
    )


def test_no_child_survives_its_owners_exit(host: HostHarness) -> None:
    host.request("alpha", "sess")
    _wait_for(
        lambda: len(host.child_pids()) >= 1, timeout=START_BOUND, description="a child"
    )
    child = host.child_pids()[0]
    assert _process_alive(child), "the child never started"
    host.owner.end()
    assert host.process is not None
    host.process.wait(timeout=STOP_BOUND)
    _wait_for(
        lambda: not _process_alive(child),
        timeout=STOP_BOUND,
        description="the child to end",
    )


def test_the_host_takes_over_a_pair_once_a_foreign_follower_exits(
    host: HostHarness,
) -> None:
    """A pair deferred to a live hand-armed follower is picked up when it ends.

    A Monitor-armed follower holds the pair; the request starts no child, so the
    host is not contending with it. When that follower ends, the next tick must
    start the host's own child -- without a second request, and without waiting
    for the next dispatch to re-request the pair.
    """
    follower = host.arm("alpha", "sess")
    host.request("alpha", "sess")
    time.sleep(0.5)
    assert host.child_pids() == [], host.child_pids()
    follower.end()
    _wait_for(
        lambda: len(host.child_pids()) >= 1,
        timeout=START_BOUND,
        description="the host's own child once the hand-armed follower ended",
    )


def test_the_defaults_read_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that passes no environment gets the process's own.

    The helpers and the host are both called with no ``environ``; each must
    resolve against the live process environment rather than a None that has no
    ``get``. The environment is set to a temporary state and log directory so a
    default that ignored it would be visible in the returned path.
    """
    module = _module()
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("RECKON_SESSION_HOST_STATE_DIR", str(state_dir))
    monkeypatch.setenv("RECKON_SESSION_HOST_LOG_DIR", str(log_dir))
    monkeypatch.setenv("RECKON_SESSION_HOST_POLL_SECONDS", "3.5")
    monkeypatch.setenv("RECKON_SESSION_HOST_BACKOFF", "1.25")

    assert module._state_dir() == state_dir
    assert module._log_dir() == log_dir
    assert module._poll_seconds() == 3.5
    assert module._initial_backoff() == 1.25

    host = module.SessionHost()
    stem = f"{host.owner['pid']}-{host.owner['start_time'] or '0'}"
    assert host.record_path == state_dir / f"{stem}.json"
    assert host._log_path == log_dir / f"{stem}.log"


def test_the_crew_host_verb_reads_a_fifo_descriptor_and_a_pre_read_request(
    tmp_path: Path,
) -> None:
    """The CLI verb handles a pre-read line and a descriptor line alike.

    The plugin reads one request line off the FIFO before it execs ``reckon
    crew host`` and passes that line in with the still-open descriptor. Here the
    verb is run through click with ``--fd`` on an inherited FIFO descriptor and
    ``--first-request``; the line supplied that way must start its follower, and
    a second request written to the descriptor afterwards must start the next.
    The first request is the only one not written to the descriptor, so a host
    that ignored it would start one child rather than two.
    """
    owner = OwnerStub()
    marker = tmp_path / "children.txt"
    fake_follower = tmp_path / "fake_follower.py"
    fake_follower.write_text(FAKE, encoding="utf-8")
    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(REPO_ROOT)
    environ["RECKON_HOME"] = str(tmp_path / "home")
    environ["RECKON_SESSION_HOST_STATE_DIR"] = str(tmp_path / "state")
    environ["RECKON_SESSION_HOST_LOG_DIR"] = str(tmp_path / "logs")
    environ["RECKON_SESSION_HOST_POLL_SECONDS"] = POLL_SECONDS
    environ["RECKON_SESSION_HOST_BACKOFF"] = BACKOFF_SECONDS
    environ["FAKE_FOLLOWER_MARKER"] = str(marker)
    fifo = tmp_path / "requests.fifo"
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDWR)
    first = json.dumps({"project": "alpha", "session": "sess"})
    argv = [
        _console_script(),
        "crew",
        "host",
        "--owner-pid",
        str(owner.pid),
        "--owner-start",
        owner.start_time,
        "--follower-command",
        json.dumps([sys.executable, str(fake_follower)]),
        "--fd",
        str(descriptor),
        "--first-request",
        first,
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environ,
            pass_fds=(descriptor,),
        )
        try:
            _wait_for(
                lambda: marker.exists() and len(marker.read_text().split()) >= 1,
                timeout=START_BOUND,
                description="the first request's child from --first-request",
            )
            os.write(
                descriptor,
                json.dumps({"project": "beta", "session": "sess"}).encode() + b"\n",
            )
            _wait_for(
                lambda: marker.exists() and len(marker.read_text().split()) >= 2,
                timeout=START_BOUND,
                description="the second request's child read off the descriptor",
            )
            assert len(set(marker.read_text().split())) == 2, marker.read_text()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=STOP_BOUND)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            owner.end()
    finally:
        os.close(descriptor)
