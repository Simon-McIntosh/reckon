"""A session mismatch never skips the host silently.

A dispatch issued with a ``--session`` name that is not the calling Claude
session used to be refused by the host hand-off and the refusal was silent: no
host was asked for the run's follower, so the caller's pane showed nothing while
the run read as unattached everywhere else. A hand-off that silently does
nothing is a delivery that never arrives.

The cases run against a temporary crew home and a stub host on the session-host
FIFO. The first shows a mismatched dispatch writes its request to the calling
host, prints one line naming both sessions, and reports that the host took the
run. The second shows a mismatched dispatch with no waiting host says so and
writes nothing. The third shows a matching dispatch is left to the ordinary ask
path, which still writes its request.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import dispatch_watch, runs

PROJECT = "proj"
CALLING_SESSION = "calling-session"
OTHER_SESSION = "s21-nova"
RUN_ID = "r-a-session-mismatch-never-skips-the-host"


def _real_crew_home() -> Path:
    """The crew home a case writes to if its isolation is not in place."""
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [
        path
        for path in (
            _real_crew_home() / "runs" / RUN_ID,
            _real_crew_home() / "live" / f"{RUN_ID}.json",
        )
        if path.exists()
    ]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


@pytest.fixture()
def claude_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make this process read as the calling Claude session, with its own runtime."""
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RECKON_SESSION_HOST_STATE_DIR", str(runtime / "session-hosts"))
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLING_SESSION)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return runtime


def _host_fifo(runtime: Path) -> Path:
    owner = dispatch_watch._session_host_owner()
    assert owner is not None, "the case must read as a Claude session"
    path = dispatch_watch._session_host_fifo_path(owner)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    return path


class _StubSessionHost:
    """A reader on the session-host FIFO that records each request line."""

    def __init__(self, fifo: Path) -> None:
        self.fifo = fifo
        self.lines: list[bytes] = []
        self.opened = threading.Event()
        self.stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.opened.wait(5), "the stub host never opened its FIFO"

    def stop(self) -> None:
        self.stop_requested.set()
        self._thread.join(timeout=5)

    def wait_for(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.lines) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    def _run(self) -> None:
        handle = os.open(self.fifo, os.O_RDWR)
        self.opened.set()
        try:
            while not self.stop_requested.is_set():
                data = b""
                while b"\n" not in data:
                    chunk = os.read(handle, 4096)
                    if not chunk:
                        return
                    data += chunk
                self.lines.append(data.split(b"\n", 1)[0])
        finally:
            os.close(handle)


@pytest.fixture()
def stub_host(claude_session: Path):
    fifo = _host_fifo(claude_session)
    stub = _StubSessionHost(fifo)
    stub.start()
    yield stub
    stub.stop()


def test_a_mismatched_dispatch_hands_the_run_to_the_calling_host(
    home: Path,
    claude_session: Path,
    stub_host: _StubSessionHost,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handoff = dispatch_watch.hand_off_mismatched_session(PROJECT, OTHER_SESSION)

    stub_host.wait_for(1)
    assert handoff == {
        "requested_session": OTHER_SESSION,
        "calling_session": CALLING_SESSION,
        "handed_to_host": True,
    }
    assert len(stub_host.lines) == 1, "the run's request never reached the host"
    request = json.loads(stub_host.lines[0])
    assert request["project"] == PROJECT
    assert request["session"] == OTHER_SESSION
    assert request["owner_checked"] is True
    line = capsys.readouterr().out
    assert OTHER_SESSION in line and CALLING_SESSION in line
    assert "took the run" in line


def test_a_mismatched_dispatch_with_no_waiting_host_says_so(
    home: Path, claude_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handoff = dispatch_watch.hand_off_mismatched_session(PROJECT, OTHER_SESSION)

    assert handoff == {
        "requested_session": OTHER_SESSION,
        "calling_session": CALLING_SESSION,
        "handed_to_host": False,
    }
    line = capsys.readouterr().out
    assert OTHER_SESSION in line and CALLING_SESSION in line
    assert "no waiting host received the run" in line


def test_a_matching_dispatch_writes_as_before(
    home: Path, claude_session: Path, stub_host: _StubSessionHost
) -> None:
    assert dispatch_watch.hand_off_mismatched_session(PROJECT, CALLING_SESSION) is None

    assert dispatch_watch._request_host_follower(PROJECT, CALLING_SESSION) is True
    stub_host.wait_for(1)
    assert len(stub_host.lines) == 1
    request = json.loads(stub_host.lines[0])
    assert request["session"] == CALLING_SESSION


def test_the_calling_session_is_none_while_a_foreign_session_name_is_handed_off(
    home: Path, claude_session: Path
) -> None:
    """A foreign name is the mismatch; the caller's own name is not."""
    assert dispatch_watch._calling_session() == CALLING_SESSION
    assert dispatch_watch.hand_off_mismatched_session(PROJECT, CALLING_SESSION) is None
    assert dispatch_watch.hand_off_mismatched_session(PROJECT, "") is None
