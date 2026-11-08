"""A running session host restarts only its own session's followers.

A host that took a request for another session keeps a child for that pair, and
the supervision tick restarts it on the backoff for as long as the host lives.
The pair belongs to somebody else's pane, so the restart duplicates a follower
another session's host also runs, and the duplicate keeps printing rows the
owning session cannot see. Measured on 7 November: a fifteen-day-old host kept
restarting a follower for session 404513fc within seconds of the owning
session's own host being asked, leaving that session's pane empty.

The cases run against a temporary crew home, a stub follower command and a real
host process. The first case reproduces the defect: an unmarked child for
another session, killed, comes back at the base. At the head it is dropped
instead, and the drop is logged with its project, session and pid. The second
restarts a marked child for an earlier session of the same owner; the third
restarts an unmarked child for the owner's own current session. The fourth
leaves a running unmarked foreign child alone. The fifth asks the host for its
own session's pair while a live follower holds it, in three shapes: one started
by another owner's host, one this host started, and one armed by hand with no
host parent.

Every case reads a process or a file rather than a return value, and every
absence is preceded by the same instrument reading something present: the
marker file that stays empty for a dropped pair carries the child that was
started and killed, and the host's own child in the fifth case is read through
the same request path that starts nothing beside it.
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

from reckon.crew import runs

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fast cadences so a case does not wait on the wall clock: the host reads these
# from its own environment.
POLL_SECONDS = "0.2"
BACKOFF_SECONDS = "0.2"

START_BOUND = 10.0
STOP_BOUND = 15.0
RESTART_BOUND = 15.0

CURRENT_SESSION = "own-session"
EARLIER_SESSION = "earlier-session"
FOREIGN_SESSION = "foreign-session"

# The follower a case drives: it records the pid the host gave it and stays up,
# so a restart is a second line and an untouched pair is exactly one.
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

# A live registration this host did not start. It takes the same advisory lock
# a real follower takes and holds it; the owner it registers under comes from
# RECKON_FOLLOWER_OWNER (a host pid, or a parentless armer) in its environment.
ARMED = (
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


def _wait_for(predicate, *, timeout: float, description: str) -> None:
    """Wait until the predicate holds, or fail naming what was awaited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


def _follower_live(env: dict[str, str], project: str, session: str) -> bool:
    """Read a follower registration's liveness under the host's own config home."""
    script = (
        "import json, sys; from reckon.crew.runs import follower_state; "
        "print(json.dumps(follower_state(sys.argv[1], sys.argv[2])['live']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, project, session],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.stdout.strip() == "true"


def _real_crew_home() -> Path:
    """The crew home a case writes to if its isolation is not in place."""
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


def _real_session_host_records() -> list[str]:
    """The record names the real crew home holds, before and after a case."""
    watched = _real_crew_home() / "session-hosts"
    if not watched.is_dir():
        return []
    return sorted(path.name for path in watched.glob("*.json"))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    before = _real_session_host_records()
    yield config_home
    assert _real_session_host_records() == before, (
        "the real crew home gained a session-hosts record"
    )


class Stub:
    """A stand-in for a Claude process: an owner, or another owner's host."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self.process.pid
        deadline = time.monotonic() + START_BOUND
        self.start_time = ""
        while not self.start_time and time.monotonic() < deadline:
            time.sleep(0.01)
            self.start_time = _start_time(self.pid)
        assert self.start_time, "stub process has no readable start time"

    def end(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


class HandArmed:
    """A live follower registration this host did not start."""

    def __init__(
        self,
        project: str,
        session: str,
        env: dict[str, str],
        *,
        parent: dict[str, str],
    ) -> None:
        self.master, slave = pty.openpty()
        script = Path(env["FAKE_FOLLOWER_MARKER"]).parent / "hand_armed.py"
        script.write_text(ARMED, encoding="utf-8")
        child_env = dict(env)
        child_env["RECKON_FOLLOWER_OWNER"] = json.dumps(parent)
        self.process = subprocess.Popen(
            [sys.executable, str(script), project, session],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        os.close(slave)

    def end(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        os.close(self.master)


class Host:
    """A running session host and everything a case reads about it."""

    def __init__(self, tmp_path: Path, *, session: str = CURRENT_SESSION) -> None:
        self.tmp_path = tmp_path
        self.session = session
        self.marker = tmp_path / "children.txt"
        self.fake_follower = tmp_path / "fake_follower.py"
        self.fake_follower.write_text(FAKE, encoding="utf-8")
        self.state_dir = tmp_path / "state"
        self.log_dir = tmp_path / "logs"
        self.stderr = tmp_path / "host.stderr"
        self.owner = Stub()
        self.process: subprocess.Popen | None = None
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
        environ["XDG_RUNTIME_DIR"] = str(self.tmp_path / "run")
        # The session this host serves, read the way the host reads it.
        environ["CLAUDE_CODE_SESSION_ID"] = self.session
        environ.pop("CODEX_SESSION_ID", None)
        environ.pop("CODEX_THREAD_ID", None)
        return environ

    def start(self) -> None:
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
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_handle,
            env=self.env(),
        )
        _wait_for(
            lambda: self.record_path().exists(),
            timeout=START_BOUND,
            description="the host's census record",
        )

    def request(
        self, project: str, session: str, *, owner_checked: bool | None = None
    ) -> None:
        assert self.process is not None and self.process.stdin is not None
        payload = {"project": project, "session": session}
        if owner_checked is not None:
            payload["owner_checked"] = owner_checked
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        self.process.stdin.flush()

    def child_pids(self) -> list[int]:
        if not self.marker.exists():
            return []
        return [
            int(line)
            for line in self.marker.read_text(encoding="utf-8").split()
            if line.strip()
        ]

    def arm(self, project: str, session: str, *, parent: dict[str, str]) -> HandArmed:
        follower = HandArmed(project, session, self.env(), parent=parent)
        self._extra.append(follower)
        deadline = time.monotonic() + START_BOUND
        while not _follower_live(self.env(), project, session):
            if time.monotonic() >= deadline:
                pytest.fail("hand-armed follower never registered as live")
            time.sleep(0.05)
        return follower

    def write_host_record(self, host_pid: int, host_start: str, owner: Stub) -> Path:
        """A census record for another owner's host, as that host would write it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"{host_pid}-{host_start}.json"
        path.write_text(
            json.dumps(
                {
                    "pid": host_pid,
                    "owner": {"pid": owner.pid, "start_time": owner.start_time},
                    "children": [],
                    "waiting_for_dispatch": False,
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        return path

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
        if self._stderr_handle is not None:
            self._stderr_handle.close()
        self.owner.end()

    def record_path(self) -> Path:
        """The census record the host writes, named for the owner it was given."""
        return self.state_dir / f"{self.owner.pid}-{self.owner.start_time}.json"

    def record(self) -> dict:
        return json.loads(self.record_path().read_text(encoding="utf-8"))

    def stderr_text(self) -> str:
        return self.stderr.read_text(encoding="utf-8") if self.stderr.exists() else ""

    def drop_lines(self) -> list[str]:
        return [line for line in self.stderr_text().splitlines() if "dropped" in line]


def test_an_unmarked_foreign_child_is_not_restarted(tmp_path: Path, home: Path) -> None:
    """The defect: a child for another session comes back after it exits.

    At the base the killed child is restarted on the backoff. At the head the
    host drops the pair, logs the project, session and pid it dropped, and the
    marker keeps exactly the one pid the request started.
    """
    host = Host(tmp_path)
    host.start()
    try:
        host.request("alpha", FOREIGN_SESSION)
        _wait_for(
            lambda: len(host.child_pids()) == 1,
            timeout=START_BOUND,
            description="a child for the requested pair",
        )
        child = host.child_pids()[0]
        assert _process_alive(child), "the child never started"
        os.kill(child, signal.SIGKILL)
        _wait_for(
            lambda: len(host.child_pids()) >= 2 or host.drop_lines(),
            timeout=RESTART_BOUND,
            description="the host to decide the exited child's fate",
        )
        # Longer than the backoff plus a few ticks: the base's restart lands
        # well inside this window, so the marker is a reading of the decision.
        time.sleep(1.0)
        assert host.child_pids() == [child], (
            f"the foreign child was restarted: {host.child_pids()}"
        )
        assert host.drop_lines(), "the host logged no dropped child"
        line = host.drop_lines()[0]
        assert "alpha" in line, line
        assert FOREIGN_SESSION in line, line
        assert str(child) in line, line
        assert host.record()["children"] == [], host.record()["children"]
    finally:
        host.stop()


def test_a_marked_child_for_an_earlier_session_is_restarted(
    tmp_path: Path, home: Path
) -> None:
    """A mark earned on the writing side keeps an earlier session followed."""
    host = Host(tmp_path)
    host.start()
    try:
        host.request("beta", EARLIER_SESSION, owner_checked=True)
        _wait_for(
            lambda: len(host.child_pids()) == 1,
            timeout=START_BOUND,
            description="a child for the marked pair",
        )
        first = host.child_pids()[0]
        os.kill(first, signal.SIGKILL)
        _wait_for(
            lambda: len(host.child_pids()) >= 2,
            timeout=RESTART_BOUND,
            description="the marked pair's restarted child",
        )
        assert host.child_pids()[1] != first, host.child_pids()
    finally:
        host.stop()


def test_a_child_for_the_owners_current_session_is_restarted(
    tmp_path: Path, home: Path
) -> None:
    """The current session is the owner's whether or not a mark was earned."""
    host = Host(tmp_path)
    host.start()
    try:
        host.request("alpha", CURRENT_SESSION)
        host.request("beta", CURRENT_SESSION, owner_checked=True)
        _wait_for(
            lambda: len(host.child_pids()) == 2,
            timeout=START_BOUND,
            description="two children for the owner's current session",
        )
        for first in host.child_pids():
            os.kill(first, signal.SIGKILL)
        _wait_for(
            lambda: len(host.child_pids()) >= 4,
            timeout=RESTART_BOUND,
            description="both children restarted",
        )
        assert len(set(host.child_pids())) == 4, host.child_pids()
    finally:
        host.stop()


def test_a_running_foreign_child_is_not_signalled(tmp_path: Path, home: Path) -> None:
    """An unmarked child for another session is left to run until it exits."""
    host = Host(tmp_path)
    host.start()
    try:
        host.request("alpha", FOREIGN_SESSION)
        _wait_for(
            lambda: len(host.child_pids()) == 1,
            timeout=START_BOUND,
            description="a child for the requested pair",
        )
        child = host.child_pids()[0]
        time.sleep(1.5)
        # The same liveness reader sees the host itself alive, so a live
        # reading here is the instrument working rather than a constant.
        assert _process_alive(host.process.pid), "the host itself ended"
        assert _process_alive(child), "the running foreign child was signalled"
        assert [entry["pid"] for entry in host.record()["children"]] == [child]
    finally:
        host.stop()


def test_a_pair_held_by_another_owners_host_is_followed_here(
    tmp_path: Path, home: Path
) -> None:
    """The host runs its own child despite a foreign host's duplicate.

    Three pairs name the host's own session. One is held by a follower whose
    parent is another owner's host, and the host starts its own child for it.
    One is held by a follower whose parent is this host itself, and one by a
    follower armed with no host parent: both are left alone.
    """
    host = Host(tmp_path)
    host.start()
    other_owner = Stub()
    foreign_host = Stub()
    try:
        host_pid = host.record()["pid"]
        host_start = _start_time(host_pid)
        host.write_host_record(
            foreign_host.pid, _start_time(foreign_host.pid), other_owner
        )
        host.arm(
            "alpha",
            CURRENT_SESSION,
            parent={
                "pid": foreign_host.pid,
                "start_time": _start_time(foreign_host.pid),
            },
        )
        host.arm(
            "beta",
            CURRENT_SESSION,
            parent={"pid": host_pid, "start_time": host_start},
        )
        host.arm(
            "gamma",
            CURRENT_SESSION,
            parent={"pid": os.getpid(), "start_time": _start_time(os.getpid())},
        )
        for project in ("alpha", "beta", "gamma"):
            host.request(project, CURRENT_SESSION, owner_checked=True)
        _wait_for(
            lambda: len(host.child_pids()) == 1,
            timeout=START_BOUND,
            description="the host's own child for the foreign-held pair",
        )
        time.sleep(1.0)
        # alpha's child is the positive control for this reader: the same
        # request path that starts it starts nothing for beta or gamma.
        assert len(host.child_pids()) == 1, host.child_pids()
        children = host.record()["children"]
        assert [(entry["project"], entry["session"]) for entry in children] == [
            ("alpha", CURRENT_SESSION)
        ], children
    finally:
        host.stop()
        other_owner.end()
        foreign_host.end()
