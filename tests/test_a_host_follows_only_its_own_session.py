"""A host is asked to follow only its own session.

A follow request names the pair it wants followed and travels to the FIFO of
the Claude process issuing it, so a request composed for any session reaches
the calling process's host and, if honoured, makes that host run a follower for
a pair it does not own. A recovery review dispatch takes this path: it dispatches
the review for the reviewed run's session, which need not be the session issuing
it. The ask then reports success from the session-wide attachment, so the
caller is told delivery is the calling host's when another host holds the pair.

The cases run against a temporary crew home, a stub host on the session-host
FIFO, and a census the caller's host writes. The first and fourth show a request
for another session is not written at all. The second and fifth show a request
for the calling session is written and carries ``owner_checked: true``. The
third shows the ask reports this host's own child, and that a dispatch then
records the delivery as the host's. The sixth shows another host's live follower
for the pair does not answer for this host's census.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import dispatch_watch, runs, session_host

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
SESSION = "calling-session"
FOREIGN_SESSION = "another-sessions-run"
RUN_ID = "r-follow-request-for-another-session"


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
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return runtime


def _owner() -> tuple[int, str]:
    owner = dispatch_watch._session_host_owner()
    assert owner is not None, "the case must read as a Claude session"
    return owner


def _host_fifo(runtime: Path) -> Path:
    path = dispatch_watch._session_host_fifo_path(_owner())
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    return path


def _write_census(children: list[dict]) -> Path:
    """Write the calling session's host census where the reader looks for it."""
    pid, start = _owner()
    directory = session_host._state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}-{start}{session_host.RECORD_SUFFIX}"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "owner": {"pid": pid, "start_time": start},
                "children": children,
            }
        ),
        encoding="utf-8",
    )
    return path


class _StubSessionHost:
    """A reader on the session-host FIFO that records each request line.

    It holds the FIFO open read-write so the non-blocking write finds a reader
    and every request is delivered at once. Built with ``answers`` it writes a
    census naming a child for the pair once it takes a request -- what the real
    host does by spawning a follower for the pair the line names.
    """

    def __init__(self, fifo: Path, *, answers: bool = True) -> None:
        self.fifo = fifo
        self.answers = answers
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
        """Wait for ``count`` request lines, so a write is seen before it is read."""
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
                if self.answers:
                    line = json.loads(self.lines[-1])
                    _write_census(
                        [
                            {
                                "project": PROJECT,
                                "session": line["session"],
                                "pid": os.getpid(),
                                "settled_at": None,
                            }
                        ]
                    )
        finally:
            os.close(handle)


@pytest.fixture()
def host_fifo(claude_session: Path) -> Path:
    return _host_fifo(claude_session)


@pytest.fixture()
def stub_host(host_fifo: Path):
    stub = _StubSessionHost(host_fifo)
    stub.start()
    yield stub
    stub.stop()


@pytest.fixture()
def bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the ask's wait to a fraction of a second, so a no-answer is quick."""
    monkeypatch.setattr(dispatch_watch, "WATCHER_LOAD_BOUND_SECONDS", 0.3)


def test_an_ask_for_another_session_writes_no_request(
    home: Path, claude_session: Path, stub_host: _StubSessionHost, bounded_wait: None
) -> None:
    _write_census([])

    answered = dispatch_watch._ask_session_host_for_follower(PROJECT, FOREIGN_SESSION)

    assert answered is False
    assert stub_host.lines == [], "a request for another session reached this host"


def test_an_ask_for_the_calling_session_marks_its_request(
    home: Path, claude_session: Path, stub_host: _StubSessionHost, bounded_wait: None
) -> None:
    _write_census([])

    answered = dispatch_watch._ask_session_host_for_follower(PROJECT, SESSION)

    assert answered is True
    assert len(stub_host.lines) == 1
    request = json.loads(stub_host.lines[0])
    assert request["project"] == PROJECT
    assert request["session"] == SESSION
    assert request["owner_checked"] is True


def test_an_ask_reports_its_own_hosts_child_and_dispatch_names_the_host(
    home: Path,
    claude_session: Path,
    stub_host: _StubSessionHost,
    monkeypatch: pytest.MonkeyPatch,
    bounded_wait: None,
) -> None:
    _write_census([])
    monkeypatch.setattr(
        dispatch_module,
        "_ensure_watch_producer",
        lambda project, session=None: {"session_attached": False},
    )
    monkeypatch.setattr(
        dispatch_module,
        "watch_state",
        lambda project, session=None: {"session_attached": False},
    )
    monkeypatch.setattr(dispatch_module, "_session_host_waiting", lambda: False)
    monkeypatch.setattr(
        dispatch_module, "watch_arming_suppressed", lambda *_a, **_k: False
    )

    assert dispatch_watch._ask_session_host_for_follower(PROJECT, SESSION) is True

    _watch, session_delivery, _override = dispatch_module._arrange_dispatch_follower(
        PROJECT, SESSION, "cli", watch_required=True, watch_override=False
    )

    assert session_delivery == "host"


def test_a_hand_off_for_another_session_writes_no_request(
    home: Path,
    claude_session: Path,
    stub_host: _StubSessionHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_watch, "_session_host_waiting", lambda: True)

    handed = dispatch_watch._hand_off_to_waiting_host(PROJECT, FOREIGN_SESSION)

    assert handed is False
    assert stub_host.lines == [], "a hand-off for another session reached this host"


def test_a_hand_off_for_the_calling_session_marks_its_request(
    home: Path,
    claude_session: Path,
    stub_host: _StubSessionHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_watch, "_session_host_waiting", lambda: True)

    handed = dispatch_watch._hand_off_to_waiting_host(PROJECT, SESSION)

    assert handed is True
    stub_host.wait_for(1)
    assert len(stub_host.lines) == 1
    request = json.loads(stub_host.lines[0])
    assert request["project"] == PROJECT
    assert request["session"] == SESSION
    assert request["owner_checked"] is True


def test_another_hosts_follower_does_not_answer_for_this_host(
    home: Path,
    claude_session: Path,
    stub_host: _StubSessionHost,
    monkeypatch: pytest.MonkeyPatch,
    bounded_wait: None,
) -> None:
    _write_census([])

    def attached_elsewhere(project: str, session: str | None = None) -> dict:
        return {"session_attached": True, "follower": {"pid": os.getpid()}}

    stub_host.answers = False
    monkeypatch.setattr(dispatch_watch, "watch_state", attached_elsewhere)

    answered = dispatch_watch._ask_session_host_for_follower(PROJECT, SESSION)

    stub_host.wait_for(1)
    assert answered is False
    assert len(stub_host.lines) == 1, "the ask must still write its own request"
