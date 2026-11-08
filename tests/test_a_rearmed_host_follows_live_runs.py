"""A re-armed session host follows its session's live runs at once.

Claude Code ends a monitor at thirty minutes, so the plugin entry point runs
again on every re-arm. It used to wait on its FIFO for a first request before
exec'ing the host, and a request arrives only with a dispatch: a session with
live runs and no new dispatch showed an empty pane indefinitely, and the
workaround was to write the request to the FIFO by hand after every re-arm.

The host now starts at once, reads its owner's session from the same environment
``_calling_session`` reads, and follows every live run that session still owns.
The cases here drive the real ``reckon crew host`` against a temporary crew home
and a stub follower command, because the hand-off, the census and the host's own
stop path are what a re-arm has to get right.

Every absence below is preceded by the same reader finding a known-present
value: the terminal-and-foreign case ends by writing a working run for the
owner's own session and watching the follower appear.
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

from reckon.crew import dispatch_watch, runs, session_host

REPO_ROOT = Path(__file__).resolve().parents[1]

OWNER_SESSION = "session-owner"
EARLIER_SESSION = "session-earlier"
FOREIGN_SESSION = "session-foreign"

PROJECT = "proj"
OTHER_PROJECT = "proj-two"

START_BOUND = 20.0
POLL_SECONDS = "0.2"

WORKING_RUN = "r-working"
OTHER_PROJECT_RUN = "r-working-elsewhere"
COMPLETE_RUN = "r-complete"
INTERRUPTED_RUN = "r-interrupted"
LAUNCH_FAILED_RUN = "r-launch-failed"
FOREIGN_RUN = "r-foreign"
EARLIER_RUN = "r-earlier"

# Each start of the stub follower appends its argv, so a case reads which pair
# the host started rather than inferring it from a pid.
FAKE_FOLLOWER = (
    "import os, sys, time\n"
    "record = os.environ['FOLLOWER_RECORD']\n"
    "with open(record, 'a', encoding='utf-8') as handle:\n"
    "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
    "while True:\n"
    "    time.sleep(0.1)\n"
)


def _wait_for(predicate, *, timeout: float, description: str) -> None:
    """Wait until the predicate holds, or fail naming what was awaited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


def _proc_start(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat, the start time in clock ticks."""
    raw = Path(f"/proc/{pid}/stat").read_bytes()
    return raw[raw.rindex(b")") + 2 :].split()[19].decode()


def _write_pointer(
    run_id: str,
    *,
    project: str = PROJECT,
    session: str = OWNER_SESSION,
    phase: str = "working",
) -> None:
    """Publish one live pointer into the temporary crew home."""
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "session": session,
            "phase": phase,
        },
    )


class OwnerStub:
    """A stand-in for the Claude process a session host lives for."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self.process.pid
        self.start_time = _proc_start(self.pid)

    def end(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, and proof the real crew home is untouched after."""
    config_home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    real = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"
    landed = [
        path
        for run_id in (
            WORKING_RUN,
            OTHER_PROJECT_RUN,
            COMPLETE_RUN,
            INTERRUPTED_RUN,
            LAUNCH_FAILED_RUN,
            FOREIGN_RUN,
            EARLIER_RUN,
        )
        for path in (real / "live" / f"{run_id}.json", real / "runs" / run_id)
        if path.exists()
    ]
    assert not landed, f"a case wrote into the real crew home: {landed}"


class HostHarness:
    """A running session host and everything a case reads about it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.state_dir = tmp_path / "state"
        self.log_dir = tmp_path / "logs"
        self.runtime = tmp_path / "run"
        self.runtime.mkdir(exist_ok=True)
        self.followers = tmp_path / "followers.txt"
        self.stdout = tmp_path / "host.stdout"
        self.stderr = tmp_path / "host.stderr"
        self.follower_script = tmp_path / "fake_follower.py"
        self.follower_script.write_text(FAKE_FOLLOWER, encoding="utf-8")
        self.owner = OwnerStub()
        self.process: subprocess.Popen | None = None
        self._stdout_handle = None
        self._stderr_handle = None
        self._fifo_fd: int | None = None

    # -- environment --------------------------------------------------------

    def env(self) -> dict[str, str]:
        environ = dict(os.environ)
        environ["PYTHONPATH"] = str(REPO_ROOT)
        environ["RECKON_HOME"] = str(self.tmp_path / "home")
        environ["RECKON_SESSION_HOST_STATE_DIR"] = str(self.state_dir)
        environ["RECKON_SESSION_HOST_LOG_DIR"] = str(self.log_dir)
        environ["RECKON_SESSION_HOST_POLL_SECONDS"] = POLL_SECONDS
        environ["XDG_RUNTIME_DIR"] = str(self.runtime)
        environ["FOLLOWER_RECORD"] = str(self.followers)
        # The host reads its owner's session from the same environment
        # ``_calling_session`` reads. The pid is this test process, so the
        # ancestry check resolves the harness to claude-code.
        environ["CLAUDE_CODE_SESSION_ID"] = OWNER_SESSION
        environ["CLAUDE_PID"] = str(os.getpid())
        return environ

    # -- the record ---------------------------------------------------------

    def record_path(self) -> Path:
        return self.state_dir / (
            f"{self.owner.pid}-{self.owner.start_time}{session_host.RECORD_SUFFIX}"
        )

    def record(self) -> dict:
        return json.loads(self.record_path().read_text(encoding="utf-8"))

    def write_previous_record(self, children: list[dict]) -> None:
        """Write the census a predecessor host left, before this host starts."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.record_path().write_text(
            json.dumps(
                {
                    "pid": self.owner.pid,
                    "owner": {
                        "pid": self.owner.pid,
                        "start_time": self.owner.start_time,
                    },
                    "children": children,
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )

    # -- the followers the host started -------------------------------------

    def follower_lines(self) -> list[str]:
        if not self.followers.exists():
            return []
        return [
            line
            for line in self.followers.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def followed(self) -> list[tuple[str, str]]:
        pairs = []
        for line in self.follower_lines():
            args = line.split()
            pairs.append(
                (
                    args[args.index("--project") + 1],
                    args[args.index("--session") + 1],
                )
            )
        return pairs

    # -- the process --------------------------------------------------------

    def fifo_path(self) -> Path:
        """The FIFO the host resolves from its owner pair and runtime root."""
        root = dispatch_watch.session_host_runtime_root(
            environ={"XDG_RUNTIME_DIR": str(self.runtime)}
        )
        assert root is not None, "the case must resolve a runtime root"
        return (
            root
            / dispatch_watch.SESSION_HOST_DIRECTORY
            / f"{self.owner.pid}-{self.owner.start_time}.fifo"
        )

    def start(self, *, on_a_fifo: bool = False) -> None:
        self._stdout_handle = self.stdout.open("wb")
        self._stderr_handle = self.stderr.open("wb")
        argv = [
            session_host._reckon_console_script(),
            "crew",
            "host",
            "--owner-pid",
            str(self.owner.pid),
            "--owner-start",
            self.owner.start_time,
            "--follower-command",
            json.dumps([sys.executable, str(self.follower_script)]),
        ]
        stdin: object = subprocess.PIPE
        pass_fds: tuple[int, ...] = ()
        if on_a_fifo:
            fifo = self.fifo_path()
            fifo.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(fifo)
            self._fifo_fd = os.open(fifo, os.O_RDWR)
            argv += ["--fd", str(self._fifo_fd)]
            stdin = subprocess.DEVNULL
            pass_fds = (self._fifo_fd,)
        self.process = subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            env=self.env(),
            pass_fds=pass_fds,
        )

    def request(self, line: dict) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write((json.dumps(line) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=START_BOUND)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                handle.close()
        if self._fifo_fd is not None:
            os.close(self._fifo_fd)
        self.owner.end()


@pytest.fixture()
def harness(tmp_path: Path, home: Path) -> HostHarness:
    made = HostHarness(tmp_path)
    yield made
    made.stop()


def _await_record(harness: HostHarness) -> dict:
    """The census the host writes at start, once it is on disk."""
    _wait_for(
        lambda: harness.record_path().exists(),
        timeout=START_BOUND,
        description="the host's census record",
    )
    return harness.record()


def test_a_live_run_of_its_owner_starts_a_follower(harness: HostHarness) -> None:
    """No request is written: the host finds the run on its own start."""
    _write_pointer(WORKING_RUN)

    harness.start()

    _wait_for(
        lambda: harness.followed() == [(PROJECT, OWNER_SESSION)],
        timeout=START_BOUND,
        description="a follower for the owner's live run",
    )
    record = harness.record()
    assert [(c["project"], c["session"]) for c in record["children"]] == [
        (PROJECT, OWNER_SESSION)
    ]
    assert record["waiting_for_dispatch"] is False


def test_live_runs_in_two_projects_start_a_follower_each(
    harness: HostHarness,
) -> None:
    _write_pointer(WORKING_RUN)
    _write_pointer(OTHER_PROJECT_RUN, project=OTHER_PROJECT)

    harness.start()

    _wait_for(
        lambda: len(harness.followed()) == 2,
        timeout=START_BOUND,
        description="a follower in each of the owner's live projects",
    )
    assert sorted(harness.followed()) == sorted(
        [(PROJECT, OWNER_SESSION), (OTHER_PROJECT, OWNER_SESSION)]
    )


def test_terminal_and_foreign_pointers_start_no_follower(
    harness: HostHarness,
) -> None:
    """Only work the pane would still show is followed.

    The control is the live-work judgement itself: before the host starts it
    reads the launch-failed pointer as this session's live work, so the host
    starting nothing for it is a statement about the scan's own rule rather
    than about an instrument that found no pointers at all.
    """
    _write_pointer(COMPLETE_RUN, phase="complete")
    _write_pointer(INTERRUPTED_RUN, phase="interrupted")
    _write_pointer(LAUNCH_FAILED_RUN, phase="launch-failed")
    _write_pointer(FOREIGN_RUN, session=FOREIGN_SESSION)

    assert session_host.live_work_run(PROJECT, OWNER_SESSION) == LAUNCH_FAILED_RUN, (
        "the control must read the launch-failed pointer as live work"
    )
    assert session_host.live_work_run(PROJECT, FOREIGN_SESSION) == FOREIGN_RUN, (
        "the control must read the foreign pointer as its own session's work"
    )

    harness.start()
    record = _await_record(harness)

    time.sleep(0.5)
    assert harness.followed() == [], "a terminal or foreign run must start nothing"
    assert record["children"] == []
    assert record["waiting_for_dispatch"] is True


def test_a_host_with_no_live_run_waits_for_dispatch(harness: HostHarness) -> None:
    harness.start()

    record = _await_record(harness)

    assert record["children"] == []
    assert record["waiting_for_dispatch"] is True
    time.sleep(0.5)
    assert harness.followed() == []
    assert harness.stdout.read_text(encoding="utf-8") == "", (
        "the host's stdout is the pane, and only its children speak there"
    )


def test_a_marked_request_reaches_the_census_as_a_marked_child(
    harness: HostHarness,
) -> None:
    harness.start()
    _await_record(harness)

    harness.request(
        {"project": PROJECT, "session": OWNER_SESSION, "owner_checked": True}
    )

    def marked_child() -> bool:
        children = harness.record().get("children") or []
        return any(child.get("owner_checked") is True for child in children)

    _wait_for(
        marked_child,
        timeout=START_BOUND,
        description="a child entry carrying the ownership mark",
    )
    children = harness.record()["children"]
    assert [(c["project"], c["session"]) for c in children] == [
        (PROJECT, OWNER_SESSION)
    ]
    assert children[0]["owner_checked"] is True


def test_an_earlier_marked_session_is_followed_and_an_unmarked_one_is_not(
    harness: HostHarness,
) -> None:
    """A pre-fix census can hold a foreign pair, and the mark tells them apart."""
    harness.write_previous_record(
        [
            {
                "project": PROJECT,
                "session": EARLIER_SESSION,
                "pid": None,
                "settled_at": None,
                "rearmed_for": None,
                "owner_checked": True,
            },
            {
                "project": PROJECT,
                "session": FOREIGN_SESSION,
                "pid": None,
                "settled_at": None,
                "rearmed_for": None,
            },
        ]
    )
    _write_pointer(EARLIER_RUN, session=EARLIER_SESSION)
    _write_pointer(FOREIGN_RUN, session=FOREIGN_SESSION)

    harness.start()

    _wait_for(
        lambda: harness.followed() == [(PROJECT, EARLIER_SESSION)],
        timeout=START_BOUND,
        description="a follower for the earlier session its owner kept marked",
    )
    time.sleep(0.5)
    assert harness.followed() == [(PROJECT, EARLIER_SESSION)], (
        "the unmarked foreign session must start no follower"
    )
    children = harness.record()["children"]
    assert [child["session"] for child in children] == [EARLIER_SESSION]
    assert children[0]["owner_checked"] is True


def test_a_signalled_host_removes_its_fifo(harness: HostHarness) -> None:
    harness.start(on_a_fifo=True)
    _await_record(harness)
    fifo = harness.fifo_path()
    assert fifo.is_fifo(), "the case must have created the host's FIFO"

    assert harness.process is not None
    harness.process.send_signal(signal.SIGTERM)
    harness.process.wait(timeout=START_BOUND)

    assert not fifo.exists(), "the host left its FIFO behind on TERM"


def test_a_host_whose_owner_exits_removes_its_fifo(harness: HostHarness) -> None:
    harness.start(on_a_fifo=True)
    _await_record(harness)
    fifo = harness.fifo_path()
    assert fifo.is_fifo(), "the case must have created the host's FIFO"

    harness.owner.end()

    assert harness.process is not None
    harness.process.wait(timeout=START_BOUND)

    assert not fifo.exists(), "the host left its FIFO behind when its owner ended"
