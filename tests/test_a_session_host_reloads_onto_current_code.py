"""A session host replaces itself onto the checkout's current code.

A host loads its code when the session starts. Without a reload it keeps that
image for the session's whole life, so a fix landed on ``main`` reaches none of
the hosts and MCP servers started that week, and their stale stamps keep shared
state in contention with current code until each process is stopped by hand.

Each case here drives a real host process against a fake follower and reads a
file or a process, never a return value. The host is given a stale image
identity through the variable a reload itself carries, so the checkout's code
reads as moved to it exactly as a real move would; the reload it performs is a
real ``os.execve`` onto an image materialised from this checkout, so the pid,
the request descriptor and the census are observed surviving it rather than
being asserted of a stub.

The controlled case first: a host told it runs the checkout's current code never
executes, which shows the instrument that reports a reload in the other cases
reads an actual exec rather than any host at all.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import checkout_trust

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fast cadences: the host reads both from its environment, so a case does not
# spend a real owner tick or restart backoff on a wall clock.
POLL_SECONDS = "0.2"
BACKOFF_SECONDS = "0.2"

START_BOUND = 15.0
# The reload materialises this checkout's package and proves it imports in a
# throwaway interpreter: seconds of git and import work, bounded generously.
# It stays under the project's 300 s per-test timeout, so a host that never
# reloads fails on this bound and its own message rather than being killed.
RELOAD_BOUND = 240.0

STALE_IDENTITY = "0" * 40
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


def _module():
    from importlib import import_module

    return import_module("reckon.crew.session_host")


def _console_script() -> str:
    return _module()._reckon_console_script()


def _start_time(pid: int) -> str:
    return _module().process_start_time(pid) or ""


def _current_identity() -> str:
    """The identity the checkout's code holds now, read the way the host does."""
    return checkout_trust.source_code_identity(
        REPO_ROOT / "reckon", source_root=REPO_ROOT
    )


def _wait_for(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


class OwnerStub:
    """A stand-in for the Claude process a session host lives for."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
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


class HostHarness:
    """A running session host and everything a case reads about it.

    The request descriptor is a pipe this harness holds, preloaded before the
    host starts when a case needs bytes already waiting. That removes the race
    between a host's first read and a line written just after it is launched,
    which is what makes the deferral case measure the host's rule rather than
    the test's timing.
    """

    def __init__(self, tmp_path: Path, *, identity: str | None) -> None:
        self.tmp_path = tmp_path
        self.marker = tmp_path / "children.txt"
        self.fake_follower = tmp_path / "fake_follower.py"
        self.fake_follower.write_text(FAKE, encoding="utf-8")
        self.state_dir = tmp_path / "state"
        self.log_dir = tmp_path / "logs"
        self.stdout = tmp_path / "host.stdout"
        self.stderr = tmp_path / "host.stderr"
        self.owner = OwnerStub()
        self.identity = identity
        self.read_fd, self.write_fd = os.pipe()
        self.process: subprocess.Popen | None = None
        self._stdout_handle = None
        self._stderr_handle = None

    def env(self) -> dict[str, str]:
        environ = dict(os.environ)
        environ["PYTHONPATH"] = str(REPO_ROOT)
        environ["RECKON_HOME"] = str(self.tmp_path / "home")
        environ["RECKON_SESSION_HOST_STATE_DIR"] = str(self.state_dir)
        environ["RECKON_SESSION_HOST_LOG_DIR"] = str(self.log_dir)
        environ["RECKON_SESSION_HOST_POLL_SECONDS"] = POLL_SECONDS
        environ["RECKON_SESSION_HOST_BACKOFF"] = BACKOFF_SECONDS
        environ["FAKE_FOLLOWER_MARKER"] = str(self.marker)
        # Reload images live under the node-local runtime root; point it at the
        # temporary tree so a case never touches the operator's runtime state.
        environ["XDG_RUNTIME_DIR"] = str(self.tmp_path / "run")
        # A moved identity is what a reload onto the checkout's current code
        # looks like from inside a host, and this is the variable the reload
        # itself carries. A case that asserts no reload simply omits it.
        environ.pop("RECKON_FOLLOWER_IMAGE_COMMIT", None)
        if self.identity is not None:
            environ["RECKON_FOLLOWER_IMAGE_COMMIT"] = self.identity
        return environ

    def start(self, *, preload: bytes | None = None) -> None:
        if preload is not None:
            os.write(self.write_fd, preload)
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
            stdin=self.read_fd,
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            env=self.env(),
            pass_fds=(self.read_fd,),
        )
        # The child holds the read end now; this side owns only the write end.
        os.close(self.read_fd)
        self.read_fd = -1

    def send(self, line: str | bytes) -> None:
        payload = line.encode() if isinstance(line, str) else line
        os.write(self.write_fd, payload)

    def child_pids(self) -> list[int]:
        if not self.marker.exists():
            return []
        return [
            int(line)
            for line in self.marker.read_text(encoding="utf-8").split()
            if line.strip()
        ]

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=START_BOUND)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                handle.close()
        self.owner.end()

    def record_path(self) -> Path:
        return self.state_dir / f"{self.owner.pid}-{self.owner.start_time}.json"

    def record(self) -> dict:
        return json.loads(self.record_path().read_text(encoding="utf-8"))

    def reload_count(self) -> int:
        try:
            return int(self.record().get("reload_count", 0))
        except (OSError, ValueError):
            return 0

    def stderr_text(self) -> str:
        return self.stderr.read_text(encoding="utf-8", errors="replace")

    def follower_alive(self, pid: int) -> bool:
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


@pytest.fixture()
def make_host(tmp_path: Path):
    harnesses: list[HostHarness] = []

    def build(*, identity: str | None) -> HostHarness:
        harness = HostHarness(tmp_path, identity=identity)
        harnesses.append(harness)
        return harness

    yield build
    for harness in harnesses:
        harness.stop()


def test_a_stale_host_reexecutes_once_keeping_its_pid_and_its_follower(
    make_host,
) -> None:
    """The whole point: a moved checkout reaches a running host, once.

    The request is written before the host starts, so it is already waiting when
    the host first reads. The host spawns that follower, defers its own reload
    past the child's first tick, and then re-executes onto an image materialised
    from this checkout. Afterwards the same pid is alive, the census names the
    same follower it was running, and the descriptor still carries a request --
    each observed rather than inferred from the reload having been attempted.
    """
    current = _current_identity()
    host = make_host(identity=STALE_IDENTITY)
    host.start(preload=b'{"project": "alpha", "session": "sess"}\n')
    original_pid = host.process.pid

    _wait_for(
        lambda: len(host.child_pids()) == 1,
        timeout=START_BOUND,
        description="the follower the host runs",
    )
    follower = host.child_pids()[0]

    _wait_for(
        lambda: host.reload_count() >= 1,
        timeout=RELOAD_BOUND,
        description="the host to reload onto the current code",
    )
    # Give a second exec every chance to show itself before the count is read:
    # a reload that looped would climb here.
    time.sleep(1.5)

    assert host.process.poll() is None, host.stderr_text()
    assert host.process.pid == original_pid, "the reload replaced its pid"
    record = host.record()
    assert record["reload_count"] == 1, record["reload_count"]
    assert record["identity"] == current, (record.get("identity"), current)
    # The follower it was running is still the follower it runs: one child, the
    # same pid, alive. A host that had not adopted it would have started a
    # second one.
    assert host.child_pids() == [follower], host.child_pids()
    assert [entry["pid"] for entry in record["children"]] == [follower]
    assert host.follower_alive(follower)

    # The request descriptor crossed the exec: a request written now, after the
    # replacement image is running.
    host.send(json.dumps({"project": "beta", "session": "sess"}) + "\n")
    _wait_for(
        lambda: len(host.child_pids()) == 2,
        timeout=START_BOUND,
        description="a second follower requested after the reload",
    )


def test_a_host_on_current_code_never_reexecutes(make_host) -> None:
    """The controlled case: the same reader reports no reload for a current host.

    Without this, the case above would pass on any host that happened to write a
    reload count, and an exec read from nothing at all would look like a reload.
    """
    host = make_host(identity=None)
    host.start(preload=b'{"project": "alpha", "session": "sess"}\n')
    original_pid = host.process.pid

    _wait_for(
        lambda: len(host.child_pids()) == 1,
        timeout=START_BOUND,
        description="the follower the host runs",
    )
    # Several idle ticks pass; a host that reloaded on any of them would show it.
    time.sleep(2.0)

    assert host.process.poll() is None, host.stderr_text()
    assert host.process.pid == original_pid
    record = host.record()
    assert record.get("reload_count", 0) == 0
    assert record["identity"] == _current_identity()
    assert len(host.child_pids()) == 1


def test_unread_request_bytes_defer_the_reload_until_they_are_handled(
    make_host,
) -> None:
    """Bytes already read into this image's buffer would be lost by an exec.

    A host told its code is stale must not replace itself while a partial
    request sits in its buffer. The partial line is preloaded before the host
    starts, so the deferral is measured from its first tick; completing the line
    is what releases the reload, and the follower the completed line asks for is
    the proof the bytes were handled rather than dropped.
    """
    host = make_host(identity=STALE_IDENTITY)
    host.start(preload=b'{"project": "alpha"')
    original_pid = host.process.pid

    # Several idle ticks with a parked partial line: no reload and no child.
    time.sleep(2.0)
    assert host.process.poll() is None, host.stderr_text()
    assert host.reload_count() == 0, host.record()
    assert host.child_pids() == []

    host.send(b', "session": "sess"}\n')
    _wait_for(
        lambda: len(host.child_pids()) == 1,
        timeout=START_BOUND,
        description="the follower the completed request asks for",
    )
    _wait_for(
        lambda: host.reload_count() >= 1,
        timeout=RELOAD_BOUND,
        description="the reload the unread bytes deferred",
    )
    assert host.process.pid == original_pid
    assert host.reload_count() == 1, host.record()


def test_a_pending_handover_is_never_read_as_a_census_record(
    tmp_path: Path,
) -> None:
    """The handover is written beside the census and is not part of it.

    A host writes the handover while its own census still describes the image
    it is leaving, so a reader that took the handover for a record would report
    a host with no pid and no children for the whole exec window. The census is
    the ``*.json`` set in the state directory, which the handover's name is
    therefore outside.
    """
    module = _module()
    state = tmp_path / "state"
    environ = {"RECKON_SESSION_HOST_STATE_DIR": str(state)}
    owner = {
        "pid": os.getpid(),
        "start_time": module.process_start_time(os.getpid()),
    }
    host = module.SessionHost(owner=owner, environ=environ)
    payload = {
        "owner": owner,
        "identity": "f" * 40,
        "reload_count": 1,
        "children": [],
    }
    host._handover_path.parent.mkdir(parents=True, exist_ok=True)
    host._handover_path.write_text(json.dumps(payload), encoding="utf-8")

    assert host._handover_path.is_file()
    assert host._handover_path.parent == state
    assert list(state.glob("*.json")) == []
    assert module.live_census(environ) == []
