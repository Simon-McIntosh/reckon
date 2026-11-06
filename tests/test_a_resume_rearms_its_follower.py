"""A resume re-arms its session's follower.

A session's pane is fed by one follower its host runs, and ``SessionHost.tick``
does not restart a child that exited with a settled status: the pair is re-armed
by its next request. A dispatch sends that request; a resume sent none, so a
session whose last action was a resume lost the pane showing the work it had
just restarted, while the worker ran on and delivered into nothing.

Both cases run against a temporary crew home and a stub host on the session
host's FIFO. The first shows the request reaching the host and the host's own
child re-arming the pair. The second shows that attachment is not the answer:
another host's live follower for the session leaves the re-arm reporting that
nothing was re-armed, because that is not a follower this session's host runs.

Two negative controls, one per case: dropping the host request from the resume
leaves the first case with no host child; accepting any live registration makes
the second case answer success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from reckon.crew import dispatch_watch, recovery, resumption, runs, session_host
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
SESSION = "sess-resumed"
FOREIGN_HOST = "a-launcher-host-that-is-not-this-one"
RUN_ID = "r-resumed-with-a-settled-follower"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _real_crew_home() -> Path:
    """The crew home a case writes to if its isolation is not in place."""
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched.

    The proof is the absence of a path only this file's run id creates: a live
    fleet writes into the real home while the case runs, so a before-and-after
    reading of that home's own entries moves under the fleet's hand.
    """
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
def repo(tmp_path: Path) -> Path:
    """A throwaway repository carrying the fleet script a launch composes with."""
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _write_exit_record(run_id: str) -> None:
    """The supervisor's account of the worker's end, where its reader looks.

    A resume is licensed by an observed end, and this is the observation that
    stays readable on a host that never launched the worker.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worker_pid": None,
                "launched_at": "2026-11-06T10:00:00Z",
                "exited_at": "2026-11-06T10:30:00Z",
                "exit_code": 0,
                "stream_records_seen": 4,
            }
        ),
        encoding="utf-8",
    )


def _pointer(tmp_path: Path, repo: Path) -> dict:
    """A stopped run of this session, as a dispatch leaves one."""
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {RUN_ID}\nstatus: waiting\n", encoding="utf-8")
    _write_exit_record(RUN_ID)
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "pid": None,
        "pid_start_time": None,
        "process_alive": None,
        "session": SESSION,
        "session_id": "sess-on-the-pointer",
        "created_at": "2026-11-06T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "phase": "working",
        "launcher_host": FOREIGN_HOST,
        "node": {
            "id": RUN_ID,
            "plan": "plan-a",
            "section": "",
            "time_budget": "30m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(RUN_ID), record)
    return record


@pytest.fixture()
def claude_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make this process read as the calling Claude session, with its own runtime.

    The host's FIFO and its census record are both named for the Claude process
    and its kernel start tick, so moving the runtime root and the state
    directory beside a temporary tree is what keeps a case off the real host.
    """
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RECKON_SESSION_HOST_STATE_DIR", str(runtime / "session-hosts"))
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-rearm-test")
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return runtime


def _host_fifo(runtime: Path) -> Path:
    """The FIFO a session host owns for this test's Claude process."""
    owner = dispatch_watch._session_host_owner()
    assert owner is not None, "the case must read as a Claude session"
    path = dispatch_watch._session_host_fifo_path(owner)
    assert path is not None
    return path


def _write_census(children: list[dict]) -> Path:
    """Write the calling session's host census where the reader looks for it."""
    owner = dispatch_watch._session_host_owner()
    assert owner is not None, "the case must read as a Claude session"
    pid, start = owner
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
    """A reader on the session host's FIFO that answers a request with a child.

    It holds the FIFO open read-write, so the resume's non-blocking write finds
    a reader and each request is delivered at once. On a request it starts a
    long-lived stand-in for ``crew follow`` and writes the census naming that
    process as the child this host runs for the pair -- which is what the real
    host does by spawning a follower for the project and session the line names.
    A stub built without a re-arm takes the request and starts nothing, the
    shape of a host that holds no child for the pair.
    """

    def __init__(self, fifo: Path, *, rearm: bool) -> None:
        self.fifo = fifo
        self.rearm = rearm
        self.project = PROJECT
        self.session = SESSION
        self.lines: list[bytes] = []
        self.child: subprocess.Popen[bytes] | None = None
        self.opened = threading.Event()
        self.stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.opened.wait(5), "the stub host never opened its FIFO"

    def stop(self) -> None:
        self.stop_requested.set()
        self._thread.join(timeout=5)
        if self.child is not None:
            self.child.terminate()
            self.child.wait(timeout=5)

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
                if self.rearm and self.child is None:
                    self.child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(120)"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    _write_census(
                        [
                            {
                                "project": self.project,
                                "session": self.session,
                                "pid": self.child.pid,
                                "settled_at": None,
                            }
                        ]
                    )
        finally:
            os.close(handle)


def _resume(record: dict, launched: list[int]) -> dict:
    """Resume the run through the one door both the CLI and the reflex use."""

    def launcher(*_args, **_kwargs) -> int:
        launched.append(4242)
        return 4242

    return resumption._resume(RUN_ID, record, config=CONFIG, launcher=launcher)


@pytest.fixture()
def bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the re-arm's wait to a fraction of a second, so a no-answer is quick.

    The real bound is the producer bound a dispatch waits, which is fine in a
    live session and far too long for a case whose host deliberately answers
    nothing.
    """
    monkeypatch.setattr(dispatch_watch, "WATCHER_LOAD_BOUND_SECONDS", 1.0)


def test_a_resume_asks_its_session_host_and_the_host_rearms_the_pair(
    home: Path,
    repo: Path,
    tmp_path: Path,
    claude_session: Path,
    bounded_wait: None,
) -> None:
    """The resume sends the dispatch's request and the host's own child answers it."""
    assert home.is_dir(), "the case runs against the temporary crew home"
    record = _pointer(tmp_path, repo)
    fifo = _host_fifo(claude_session)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)
    # The settled child the host still names: it runs nothing, so the pair is
    # idle until the resume asks for it again.
    _write_census(
        [
            {
                "project": PROJECT,
                "session": SESSION,
                "pid": None,
                "settled_at": 1_760_000_000.0,
            }
        ]
    )
    host = _StubSessionHost(fifo, rearm=True)
    launched: list[int] = []
    host.start()
    try:
        result = _resume(record, launched)
        child_pid = host.child.pid if host.child is not None else None
    finally:
        host.stop()

    assert launched == [4242], "the resume still launches its worker"
    assert host.lines, "the resume must send the host a follow request"
    assert json.loads(host.lines[0]) == {"project": PROJECT, "session": SESSION}
    assert len(host.lines) == 1, "one resume asks once"
    assert child_pid is not None, "the stub host must have re-armed a child"
    assert result["follower_rearmed"] is True
    assert dispatch_watch._session_host_child_pid(PROJECT, SESSION) == child_pid


def test_a_rearm_answers_failure_when_only_another_hosts_follower_is_live(
    home: Path,
    repo: Path,
    tmp_path: Path,
    claude_session: Path,
    bounded_wait: None,
) -> None:
    """Attachment is not the answer: the calling session's own host must hold a child.

    A follower registered against this session, and running, keeps
    ``session_attached`` true. It is not a child of this session's host, so a
    re-arm that took attachment as its answer would report a pane the resume
    never restored.
    """
    assert home.is_dir(), "the case runs against the temporary crew home"
    record = _pointer(tmp_path, repo)
    fifo = _host_fifo(claude_session)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)
    # This host holds no child for the pair; the census names one for another
    # pair, which is what an idle host's record looks like.
    _write_census(
        [
            {
                "project": "another-project",
                "session": "another-session",
                "pid": os.getpid(),
                "settled_at": None,
            }
        ]
    )
    host = _StubSessionHost(fifo, rearm=False)
    launched: list[int] = []
    host.start()
    try:
        with runs.follower_registration(PROJECT, SESSION, delivery="stream"):
            assert runs.follower_state(PROJECT, SESSION).get("live") is True, (
                "the other host's follower must read as attached"
            )
            result = _resume(record, launched)
    finally:
        host.stop()

    assert launched == [4242], "the resume still launches its worker"
    assert host.lines, "the resume must still send the host a follow request"
    assert result["follower_rearmed"] is False, (
        "another host's follower is not this session's re-arm"
    )
    assert dispatch_watch._session_host_child_pid(PROJECT, SESSION) is None
