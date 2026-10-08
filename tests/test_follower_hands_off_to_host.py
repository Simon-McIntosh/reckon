"""A hand-armed follower hands its session to a session host that is waiting.

A session with the crew-host plugin linked runs one session host, waiting on a
node-local FIFO named for the Claude process and its kernel start tick. A
coordinator that arms ``crew follow`` by hand beside that host duplicates the
pane and re-arms every time the arming ends, while the host waits unused. These
cases drive the real command and a real dispatch against a temporary crew home
and a temporary runtime root, and observe a process, a FIFO line, or a dispatch
payload rather than a return value.

Every absence is preceded by showing the same instrument reading a known-present
value: the follower streams with no reader on the FIFO, and hands over -- one
request line, one handoff line, a clean exit -- when a reader holds it.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.dispatch_launch import (
    _LAUNCHED_WORKER_REAPER,
    _LAUNCHED_WORKERS,
    _LAUNCHED_WORKERS_HANDOVER_ENV,
    _LAUNCHED_WORKERS_LOCK,
)
from reckon.crew_follow_commands import crew_follow

# A session host reads a hand-armed follower's pair and adopts it, so the
# dispatch case needs a real watcher seat rather than the suite's suppressed
# arming. The follower cases start no producer, so only the dispatch case is
# marked. The ``isolated_project`` fixture is requested by name from the
# sibling module, so the import is a registration rather than a direct use.
from tests.test_dispatch_session_host import (  # noqa: F401
    _claude_env,
    _dispatch,
    _register_watcher,
    _session_host_fifo,
    _spawn_runner,
    isolated_project,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT = "sample"
SESSION = "session-handoff"

START_BOUND = 30.0
STREAM_SECONDS = 2.5
END_BOUND = 30.0

HANDOFF_MARKER = "session host now delivers"


class OwnerStub:
    """A stand-in for the Claude process a session host belongs to."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self.process.pid
        deadline = time.monotonic() + START_BOUND
        self.start_time = runs._process_start_time(self.pid) or ""
        while not self.start_time and time.monotonic() < deadline:
            time.sleep(0.01)
            self.start_time = runs._process_start_time(self.pid) or ""
        assert self.start_time, "owner stub has no readable start time"

    def end(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "run"
    runtime.mkdir()
    return runtime


def _follower_env(
    home: Path, runtime: Path, owner_pid: int, *, child: bool = False
) -> dict[str, str]:
    """The follower's environment with a Claude identity and a temporary root.

    The ambient crew and harness variables are dropped: a worker whose own
    session runs under a follower exports ``RECKON_FOLLOWER_OWNER`` and its own
    Claude identity, and inherited unchanged they would make this follower
    believe it belongs to another session. Only what the case sets is kept.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RECKON_", "CLAUDE_", "CODEX_"))
    }
    env["RECKON_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["CLAUDE_PID"] = str(owner_pid)
    env["CLAUDE_CODE_SESSION_ID"] = "claude-session-host-handoff"
    if child:
        from reckon.crew.session_host import CHILD_ENV

        env[CHILD_ENV] = "1"
    return env


def _arm(
    env: dict[str, str], *args: str, session: str | None = SESSION
) -> subprocess.Popen:
    """Start the real follower command against the temporary config home."""
    argv = [
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "follow",
        "--project",
        PROJECT,
        "--no-color",
    ]
    if session is not None:
        argv += ["--session", session]
    argv += list(args)
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _fifo(runtime: Path, owner: OwnerStub) -> Path:
    return runtime / "reckon-session-host" / f"{owner.pid}-{owner.start_time}.fifo"


def _hold_fifo(fifo: Path) -> int:
    """Create the FIFO and hold a reader on it, as a waiting host does."""
    fifo.parent.mkdir(parents=True, exist_ok=True)
    if not fifo.exists():
        os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    os.set_blocking(descriptor, False)
    return descriptor


def _read_one_line(descriptor: int, timeout: float) -> str:
    data = b""
    deadline = time.monotonic() + timeout
    while b"\n" not in data and time.monotonic() < deadline:
        ready, _w, _x = select.select([descriptor], [], [], 0.1)
        if ready:
            chunk = os.read(descriptor, 4096)
            if chunk:
                data += chunk
    return data.decode("utf-8", errors="replace")


def _kill(process: subprocess.Popen) -> tuple[str, str]:
    if process.poll() is None:
        process.kill()
    return process.communicate(timeout=START_BOUND)


def _wait_registered(
    process: subprocess.Popen, *, timeout: float = START_BOUND
) -> None:
    """Wait until the follower holds its registration, so start-up is behind it.

    The registration is acquired after the follower has decided not to hand off
    at start-up, so observing it is how a case knows the host may now start
    waiting without being read as waiting at start-up -- the timing the end-row
    case exists to exercise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=START_BOUND)
            pytest.fail(
                f"the follower ended before it armed; stdout={stdout!r} "
                f"stderr={stderr!r}"
            )
        if runs.follower_state(PROJECT, SESSION)["registered"]:
            return
        time.sleep(0.05)
    pytest.fail("the follower never armed its registration")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def test_a_follower_hands_off_to_a_waiting_host_and_exits(home, tmp_path) -> None:
    """A hand-armed follower beside a waiting host writes one request and exits.

    The host is the session's own delivery, so the follower asks it for the pair
    and leaves rather than streaming beside it: exactly one request line reaches
    the FIFO, the follower prints the handoff line, and it exits zero so a shell
    that armed it is not left holding a second pane.
    """
    runtime = _runtime(tmp_path)
    owner = OwnerStub()
    fifo = _fifo(runtime, owner)
    descriptor = _hold_fifo(fifo)
    process = _arm(_follower_env(home, runtime, owner.pid))
    try:
        stdout, stderr = process.communicate(timeout=START_BOUND)
        assert process.returncode == 0, (
            f"a handoff exit is the follower ending by itself; stderr={stderr!r}"
        )
        assert HANDOFF_MARKER in stdout, (
            f"the follower must print the handoff line; stdout={stdout!r}"
        )
        written = _read_one_line(descriptor, 2.0)
        lines = [line for line in written.splitlines() if line.strip()]
        assert len(lines) == 1, f"exactly one request line, got {written!r}"
        assert json.loads(lines[0]) == {"project": PROJECT, "session": SESSION}
    finally:
        _kill(process)
        os.close(descriptor)
        owner.end()


def test_a_follower_with_no_reader_streams_as_before(home, tmp_path) -> None:
    """With no reader on the FIFO the follower streams rather than handing off.

    The positive control is the case above: the same reader sees an exit and a
    request when a host holds the FIFO, so a follower still running here is the
    absence of a host and not a request that never ran.
    """
    runtime = _runtime(tmp_path)
    owner = OwnerStub()
    fifo = _fifo(runtime, owner)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)  # present, but with no reader: no host is waiting
    process = _arm(_follower_env(home, runtime, owner.pid))
    try:
        time.sleep(STREAM_SECONDS)
        assert process.poll() is None, (
            "a follower with no waiting host must keep streaming, not exit"
        )
    finally:
        stdout, _stderr = _kill(process)
        owner.end()
    assert HANDOFF_MARKER not in stdout, (
        f"a follower with no host must not print the handoff line; {stdout!r}"
    )


def test_a_host_child_never_hands_off(home, tmp_path) -> None:
    """A follower carrying the host-child marker never hands its session over.

    The host already consumes its own child; a handoff from it would ask the
    host for a follower it is already running. The same waiting host that drew a
    handoff from a hand-armed follower leaves this one streaming and its FIFO
    quiet.
    """
    runtime = _runtime(tmp_path)
    owner = OwnerStub()
    fifo = _fifo(runtime, owner)
    descriptor = _hold_fifo(fifo)
    process = _arm(_follower_env(home, runtime, owner.pid, child=True))
    try:
        time.sleep(STREAM_SECONDS)
        assert process.poll() is None, "the host's own child must keep streaming"
        assert _read_one_line(descriptor, 0.2) == "", (
            "the host's own child wrote a request"
        )
    finally:
        stdout, _stderr = _kill(process)
        os.close(descriptor)
        owner.end()
    assert HANDOFF_MARKER not in stdout, (
        f"the host's own child must not print the handoff line; {stdout!r}"
    )


def test_the_lifetime_end_row_hands_over_without_a_re_arm_command(
    home, tmp_path
) -> None:
    """A hand-armed follower that reaches its lifetime hands over at its end.

    The host appears while the follower streams, so the follower reaches its own
    deadline rather than handing over at start-up. Its last line says the host
    now delivers and carries no ``re-arm with``, because a re-arm beside a host
    that consumes the session would arm a second follower.
    """
    runtime = _runtime(tmp_path)
    owner = OwnerStub()
    fifo = _fifo(runtime, owner)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)  # no reader yet: the follower streams past its start-up check
    process = _arm(_follower_env(home, runtime, owner.pid), "--lifetime", "8s")
    descriptor = None
    try:
        _wait_registered(process)
        descriptor = _hold_fifo(fifo)  # the host starts waiting mid-run
        stdout, stderr = process.communicate(timeout=END_BOUND)
        assert process.returncode == 0, stderr
        lines = [line for line in stdout.splitlines() if line.strip()]
        assert lines, f"the follower ended without printing anything; {stderr!r}"
        final = lines[-1]
        assert final.startswith("follower end:"), final
        assert "re-arm with" not in final, (
            f"a handed-over end row must carry no re-arm command; {final!r}"
        )
        assert HANDOFF_MARKER in final, (
            f"the end row must say the host now delivers; {final!r}"
        )
        written = _read_one_line(descriptor, 2.0)
        assert HANDOFF_MARKER not in written  # the request is JSON, not the line
        assert json.loads(written.strip()) == {"project": PROJECT, "session": SESSION}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _kill(process)
        owner.end()


@pytest.mark.arms_watch_producer
def test_dispatch_asks_a_waiting_host_for_a_foreignly_attached_session(
    request, tmp_path, monkeypatch
) -> None:
    """A waiting host is asked even when a follower the host does not run holds
    the session, and the dispatch reports delivery ``host``.

    The session reads as attached by a hand-armed follower, so the old path
    never asked the host and reported ``monitor``. A host waiting on its FIFO
    can take the pair over once that follower ends, so the dispatch writes the
    request and reports ``host`` instead.
    """
    config_home, repo = request.getfixturevalue("isolated_project")
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    _claude_env(monkeypatch, runtime)
    fifo = _session_host_fifo(runtime)
    descriptor = _hold_fifo(fifo)
    runner = _spawn_runner()
    try:
        _register_watcher(PROJECT, runner.pid)
        with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
            record = _dispatch(config_home, repo, SESSION)
        written = _read_one_line(descriptor, 2.0)
    finally:
        os.close(descriptor)
        runner.terminate()
        runner.wait(timeout=5)

    assert written, "the dispatch must write the request to the waiting host"
    assert json.loads(written.strip()) == {"project": PROJECT, "session": SESSION}
    assert record["watch"]["session_attached"] is True
    assert record["watch"]["delivery"] == "host"


def test_a_reloaded_follower_adopts_launched_workers_before_it_hands_off(
    home, tmp_path, monkeypatch, capsys
) -> None:
    """A follower reloaded in place hands off and still adopts its children.

    The replacement image runs the start-up handoff, but the adoption of the
    pids carried across the in-place reload runs first: registering them is what
    keeps the children the previous image launched collectable by the same
    process, which remains their parent. A handoff taken before the adoption
    would leave the registry empty and no reaper started, so the carried child
    would stay an uncollected child of a process that has exited.
    """
    runtime = _runtime(tmp_path)
    owner = OwnerStub()
    fifo = _fifo(runtime, owner)
    descriptor = _hold_fifo(fifo)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CLAUDE_PID", str(owner.pid))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session-host-handoff")
    from reckon.crew.session_host import CHILD_ENV

    monkeypatch.delenv(CHILD_ENV, raising=False)

    # A live child of THIS process, as a worker the previous follower image
    # launched and carried across its reload in the handover carrier.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.clear()
    _LAUNCHED_WORKER_REAPER["thread"] = None
    os.environ[_LAUNCHED_WORKERS_HANDOVER_ENV] = json.dumps(
        {"pids": [child.pid], "runs": {}}
    )
    try:
        crew_follow.callback(
            project=PROJECT,
            session=SESSION,
            observe_sessions=(),
            run_ids=(),
            attention=False,
            json_output=False,
            pretty=False,
            lifetime=None,
            width=None,
            theme=None,
            no_color=True,
        )
        out = capsys.readouterr().out
        assert HANDOFF_MARKER in out, (
            f"the reloaded follower must print the handoff line; {out!r}"
        )
        written = _read_one_line(descriptor, 2.0)
        assert json.loads(written.strip()) == {"project": PROJECT, "session": SESSION}
        # The adoption ran before the handoff returned: the carried pid is
        # registered and a reaper owns it, so the child is not abandoned.
        with _LAUNCHED_WORKERS_LOCK:
            adopted = child.pid in _LAUNCHED_WORKERS
        assert adopted, "the handoff returned before the launched pids were adopted"
        assert _LAUNCHED_WORKER_REAPER["thread"] is not None, (
            "an adopted worker must start the reaper that collects it"
        )
    finally:
        os.close(descriptor)
        owner.end()
        if child.poll() is None:
            child.kill()
        child.wait()
        with _LAUNCHED_WORKERS_LOCK:
            _LAUNCHED_WORKERS.discard(child.pid)
        os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, None)
