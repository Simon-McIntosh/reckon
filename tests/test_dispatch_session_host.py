"""Dispatch asks the calling session's host for a follower before it refuses.

A Claude Code session with the crew-host plugin linked runs one session host,
waiting on a node-local FIFO named for the Claude process and its kernel start
tick. A dispatch from a session that is not attached writes one request line to
that FIFO and waits, briefly, for the host to attach it. A session with no host
-- no FIFO, or a FIFO with no reader -- falls back to the Monitor path
unchanged: the same refusal text as before, and a delivery of ``monitor``. The
delivery verdict rides the payload and the refusal so a caller can tell which
path it took.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs, session_host

# The suite suppresses watcher arming by default; these cases decide on the
# watcher, so they run with arming allowed and register their own watcher
# process rather than starting a producer.
pytestmark = pytest.mark.arms_watch_producer

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A mountable repository plus a configuration home, as dispatch needs."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        (
            "<!doctype html>\n<html><head>\n"
            '<meta name="docs-project" content="sample">\n'
            '<meta name="reckon-type" content="plan">\n'
            '<meta name="plan-slug" content="fixture">\n'
            '</head><body><h2 id="guard">Dispatch guard</h2></body></html>\n'
        ),
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repo,
            encoding="utf-8",
            check=True,
            capture_output=True,
        )
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record dispatch admission for the calling session's host",
        plan="fixture",
        section="guard",
        spec_level="exact",
        done_when="pytest reports one passing session-host dispatch case",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _spawn_runner() -> subprocess.Popen[str]:
    """A long-lived process used as a genuinely running registered watcher."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _register_watcher(project: str, pid: int) -> None:
    """Register a running process as the project's watcher seat."""
    path = crew.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        crew._write_watch_record(
            handle,
            {"pid": pid, "pid_start_time": crew._process_start_time(pid)},
        )


def _claude_env(monkeypatch, runtime: Path) -> None:
    """Make this process read as the calling Claude session with a runtime dir.

    The host's census record directory is moved beside the runtime dir, so a
    case never reads or writes the real config home and both the writer here and
    the reader under test resolve the same temp tree.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RECKON_SESSION_HOST_STATE_DIR", str(runtime / "session-hosts"))
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session-host-test")
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


def _session_host_fifo(runtime: Path) -> Path:
    """The FIFO path a session host owns for this test's Claude process."""
    pid = os.getpid()
    start = crew._process_start_time(pid)
    assert start, "the running node id must have a kernel start tick"
    return runtime / "reckon-session-host" / f"{pid}-{start}.fifo"


def _write_host_record(children: list[dict]) -> Path:
    """Write the calling session's host census into the host module's state dir.

    The directory and the filename suffix are the ones the host itself resolves,
    so this fixture writes where the reader under test looks rather than at a
    second hardcoded spelling of the same path.
    """
    pid = os.getpid()
    start = crew._process_start_time(pid)
    assert start, "the running node id must have a kernel start tick"
    directory = session_host._state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}-{start}{session_host.RECORD_SUFFIX}"
    path.write_text(json.dumps({"pid": pid, "children": children}), encoding="utf-8")
    return path


class _StubHost:
    """A reader on the session host's FIFO that attaches the session on request.

    It holds the FIFO open read-write, so a dispatch's non-blocking write finds a
    reader and the request is delivered at once. On reading one line it takes a
    follower registration and keeps it until stopped, which is what the real
    host does by running ``crew follow`` for the project and session.
    """

    def __init__(self, fifo: Path, project: str, session: str) -> None:
        self.fifo = fifo
        self.project = project
        self.session = session
        self.line: bytes | None = None
        self.opened = threading.Event()
        self.stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.opened.wait(5), "the stub host never opened its FIFO"

    def stop(self) -> None:
        self.stop_requested.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        handle = os.open(self.fifo, os.O_RDWR)
        self.opened.set()
        try:
            data = b""
            while b"\n" not in data:
                chunk = os.read(handle, 4096)
                if not chunk:
                    return
                data += chunk
            self.line = data.split(b"\n", 1)[0]
            with runs.follower_registration(
                self.project, self.session, delivery="stream"
            ):
                self.stop_requested.wait(30)
        finally:
            os.close(handle)


def _dispatch(config_home: Path, repo: Path, session: str):
    return crew.dispatch(
        node=_node(config_home, session),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=lambda *args, **kwargs: 4242,
        watch_required=True,
    )


def test_dispatch_asks_the_session_host_and_is_admitted_by_it(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """A host on the FIFO attaches the session and the run is admitted."""
    config_home, repo = isolated_project
    project = "sample"
    session = "session-hosted"
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    _claude_env(monkeypatch, runtime)
    fifo = _session_host_fifo(runtime)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)
    stub = _StubHost(fifo, project, session)
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        stub.start()
        record = _dispatch(config_home, repo, session)
    finally:
        stub.stop()
        runner.terminate()
        runner.wait(timeout=5)

    assert stub.line is not None, "the request must reach the host's FIFO"
    assert json.loads(stub.line) == {"project": project, "session": session}
    assert record["watch"]["session_attached"] is True
    assert record["watch"]["delivery"] == "host"
    assert record["watch"]["arming_line"] == "", "a host-delivered session arms nothing"


def test_dispatch_without_a_host_reader_falls_back_to_the_monitor_refusal(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """No reader on the FIFO leaves the refusal text unchanged, delivery monitor."""
    config_home, repo = isolated_project
    project = "sample"
    session = "session-unhosted"
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    _claude_env(monkeypatch, runtime)
    fifo = _session_host_fifo(runtime)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        # The control: no FIFO at all, so there is nothing to ask.
        with pytest.raises(crew.WatcherRequired) as before:
            _dispatch(config_home, repo, session)
        # Now the FIFO exists but holds no reader: the write falls through.
        os.mkfifo(fifo)
        with pytest.raises(crew.WatcherRequired) as after:
            _dispatch(config_home, repo, session)
    finally:
        runner.terminate()
        runner.wait(timeout=5)

    assert str(after.value) == str(before.value), "the refusal text is unchanged"
    assert getattr(after.value, "delivery", None) == "monitor"


def test_dispatch_reads_host_for_an_already_attached_host_follower(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """A session already attached by the host's own follower reads delivery host.

    No request is written and no arming line is offered: the host already
    consumes the follower, so the payload must say so rather than hand the
    caller a Monitor watch that would double-deliver.
    """
    config_home, repo = isolated_project
    project = "sample"
    session = "session-prehosted"
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    _claude_env(monkeypatch, runtime)
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        # The host's census names the follower it started for this pair. The
        # live registration below is held by the same process, which is the pid
        # the census carries, so the two describe one follower.
        _write_host_record(
            [{"project": project, "session": session, "pid": os.getpid()}],
        )
        with runs.follower_registration(project, session, delivery="stream"):
            record = _dispatch(config_home, repo, session)
    finally:
        runner.terminate()
        runner.wait(timeout=5)

    assert record["watch"]["session_attached"] is True
    assert record["watch"]["delivery"] == "host"
    assert record["watch"]["arming_line"] == "", "a host-delivered session arms nothing"


def test_dispatch_reads_monitor_for_a_follower_the_host_did_not_start(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """A hand-armed follower the host does not run leaves delivery at monitor.

    The host's census names this pair, but with a pid that is not the live
    registration's, so the follower attached to the session is not the host's
    and the caller still needs the Monitor path.
    """
    config_home, repo = isolated_project
    project = "sample"
    session = "session-foreign"
    runtime = tmp_path / "run-runtime"
    runtime.mkdir()
    _claude_env(monkeypatch, runtime)
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        # A child for this pair, but a different process than the reader.
        _write_host_record(
            [{"project": project, "session": session, "pid": runner.pid}],
        )
        with runs.follower_registration(project, session, delivery="stream"):
            record = _dispatch(config_home, repo, session)
    finally:
        runner.terminate()
        runner.wait(timeout=5)

    assert record["watch"]["session_attached"] is True
    assert record["watch"]["delivery"] == "monitor"
    assert record["watch"]["arming_line"] != "", (
        "a monitor session keeps its arming line"
    )
