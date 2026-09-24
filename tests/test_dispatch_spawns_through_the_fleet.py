"""Gate: a launch on the fleet node is spawned by the allocation's batch step.

A ``setsid``-detached child is reaped with the session step that made it, so a
supervisor forked by a dispatch running on the fleet node dies with the
dispatching session and takes its worker with it. The launch therefore leaves
through the same process tree that outlives every step: the allocation's batch
step, which reads one request per line from a FIFO in the runtime directory the
fleet record publishes.

Each case stands in for that batch step with a stub reader over a temporary
runtime directory, and measures one thing the request must do:

* a dispatch writes exactly one ``spawn`` line and records the pid the batch
  step acknowledged on the pointer;
* a dispatch with no fleet record forks as before, confirmed by intercepting
  the fork rather than by the absence of a failure;
* a FIFO that never acknowledges refuses within its bound, leaving neither
  pointer nor worktree behind;
* a ``crew watch`` producer start leaves by the same lane;
* a review dispatch and a resumption both reach the same supervisor, so
  neither is a child of whoever issued it.

The lane is opted into per case through ``RECKON_FLEET_SPAWN``: the batch step's
``spawn`` verb and this half land separately, and this workstation's fleet node
publishes a record for every session running on it, so keying on the record
alone would route an ordinary off-fleet dispatch into a FIFO whose reader may
not know the verb.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew
from reckon.crew import recovery, resumption, runs
from reckon.crew.dispatch import WATCH_ARMING_ENV
from reckon.crew.node import CrewError

# The fence's ceiling on how long an unacknowledged request may hold a dispatch.
FLEET_ACK_CEILING = 10.0

CONFIG: dict = {
    "default_backend": "alpha",
    "local_backend": "alpha",
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
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


class _StubBatchStep:
    """The allocation's batch step, stood in for by its FIFO reader.

    It holds both ends of the FIFO open as the real step does: the read end so
    each request is read as it arrives, and a synthetic write end so a read
    blocks between requests rather than seeing an end-of-file that would look
    like a reader leaving. Each parsed line is either answered with the
    acknowledgement the protocol declares or ignored, so the refusal path can be
    measured.
    """

    def __init__(self, runtime_dir: Path, *, acknowledge: bool = True) -> None:
        self.fifo = runtime_dir / "requests"
        self.acknowledge = acknowledge
        self.lines: list[str] = []
        self.supervisor_pid = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"]
        ).pid
        self._read_fd: int | None = None
        self._hold_fd: int | None = None
        self._opened = threading.Event()
        self._stop = threading.Event()

    def start(self) -> _StubBatchStep:
        os.mkfifo(self.fifo)
        threading.Thread(target=self._run, daemon=True).start()
        assert self._opened.wait(10.0), "the stub reader never opened the FIFO"

    def stop(self) -> None:
        self._stop.set()
        self._opened.wait(10.0)
        for descriptor in (self._read_fd, self._hold_fd):
            if descriptor is not None:
                os.close(descriptor)
        with contextlib.suppress(OSError):
            os.kill(self.supervisor_pid, 9)

    def _run(self) -> None:
        self._read_fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self._hold_fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        self._opened.set()
        carried = b""
        while not self._stop.is_set():
            readable, _, _ = select.select([self._read_fd], [], [], 0.05)
            if not readable:
                continue
            chunk = os.read(self._read_fd, 4096)
            if not chunk:
                continue
            carried += chunk
            while b"\n" in carried:
                line, carried = carried.split(b"\n", 1)
                self._handle(line.decode())

    def _handle(self, line: str) -> None:
        self.lines.append(line)
        if not self.acknowledge:
            return
        fields = line.split(" ")
        assert len(fields) == 3, line
        assert fields[0] == "spawn", line
        spec_path = Path(fields[2])
        assert spec_path.is_file(), spec_path
        # The acknowledgement lands beside the spec, which is the run directory
        # for a worker and the watch directory for a producer.
        (spec_path.parent / "spawned.json").write_text(
            json.dumps(
                {"pid": self.supervisor_pid, "started_at": "2026-09-24T00:00:00Z"}
            ),
            encoding="utf-8",
        )


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s10">A worker launches from the batch step</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        # A real worktree, because the refusal case asserts one is gone when the
        # dispatch refuses and only a registered worktree can be removed.
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", "--force", str(path), base_sha],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    # The watch is waived for every case but the producer one: the lane under
    # measurement is the launch, and arming a producer would add a second thing
    # to explain. Waiving is the recorded no-watch path, not a bypass.
    monkeypatch.setenv(WATCH_ARMING_ENV, "off")
    return config_home, repo


def _publish_fleet_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Publish a record naming this process's allocation; return its runtime dir."""
    runtime_dir = tmp_path / "fleet-runtime"
    runtime_dir.mkdir()
    record = tmp_path / "fleet-record.json"
    record.write_text(
        json.dumps(
            {
                "job_id": "4242",
                "node": "fixture",
                "runtime_dir": str(runtime_dir),
                "started_at": "2026-09-24T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(record))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("SLURM_JOB_ID", "4242")
    return runtime_dir


class _ForkRecorder:
    """A stand-in for ``subprocess`` that records only the ``Popen`` calls.

    Patching the real module's ``Popen`` would patch every subprocess call in
    the process, including the ``git`` invocations the repository work needs.
    Replacing only dispatch's own name for the module keeps the interception on
    the fork under measurement.
    """

    def __init__(self) -> None:
        self.forked: list[list[str]] = []

    def Popen(self, argv, *arguments, **keywords):  # noqa: N802 - mirrors stdlib
        self.forked.append(list(argv))
        return SimpleNamespace(pid=os.getpid(), poll=lambda: None)

    def __getattr__(self, name: str):
        return getattr(subprocess, name)


def _opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_FLEET_SPAWN", "on")


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="launch one worker through the fleet's batch step",
        plan="fixture",
        section="s10",
        spec_level="guided",
        done_when="pytest reports one spawn line and the acknowledged pid on the pointer",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _dispatch(config_home: Path, repo: Path, name: str, **kwargs) -> dict:
    session = kwargs.pop("session", f"session-{name}")
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=None,
        watch_required=False,
        check_budget=False,
        **kwargs,
    )


def test_a_fleet_dispatch_writes_one_spawn_line_and_records_the_acked_pid(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home, repo = host
    runtime_dir = _publish_fleet_record(tmp_path, monkeypatch)
    _opt_in(monkeypatch)
    stub = _StubBatchStep(runtime_dir)
    stub.start()
    try:
        record = _dispatch(config_home, repo, "fleet")

        assert len(stub.lines) == 1, stub.lines
        verb, request_id, spec_path = stub.lines[0].split(" ")
        assert verb == "spawn"
        assert request_id == record["run_id"]
        spec_file = Path(spec_path)
        assert spec_file.name == "supervisor.json"
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        assert spec["run_id"] == record["run_id"]
        assert spec["argv"][-1] == str(spec_file)
        assert "__supervise__" in spec["argv"]
        assert spec["plan"]["backend"] == "alpha"

        assert record["pid"] == stub.supervisor_pid
        assert runs.read_pointer(record["run_id"])["pid"] == stub.supervisor_pid
    finally:
        stub.stop()


def test_a_dispatch_with_no_fleet_record_forks_as_before(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fork is confirmed by the call it makes, not by an absent failure."""
    config_home, repo = host
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(tmp_path / "absent-record.json"))
    _opt_in(monkeypatch)
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    recorder = _ForkRecorder()
    monkeypatch.setattr(dispatch_module, "subprocess", recorder)
    record = _dispatch(config_home, repo, "forked")

    assert recorder.forked, "no process was started at all"
    assert any("__supervise__" in argv for argv in recorder.forked), recorder.forked
    assert record["pid"] == os.getpid()


def test_the_fleet_lane_stays_off_until_the_batch_step_opts_in(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published record alone must not redirect a session's launch.

    The batch step's spawn verb and this half land separately, so a record that
    is present without the opt-in has to keep forking — otherwise every session
    on the node sends a request its batch step cannot answer.
    """
    config_home, repo = host
    runtime_dir = _publish_fleet_record(tmp_path, monkeypatch)
    monkeypatch.delenv("RECKON_FLEET_SPAWN", raising=False)
    stub = _StubBatchStep(runtime_dir)
    stub.start()
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    recorder = _ForkRecorder()
    monkeypatch.setattr(dispatch_module, "subprocess", recorder)
    try:
        _dispatch(config_home, repo, "optin")
        assert stub.lines == [], "the lane ran without the opt-in"
        assert any("__supervise__" in argv for argv in recorder.forked), recorder.forked
    finally:
        stub.stop()


def test_an_unacknowledged_fifo_refuses_leaving_no_pointer_or_worktree(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home, repo = host
    runtime_dir = _publish_fleet_record(tmp_path, monkeypatch)
    _opt_in(monkeypatch)
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch_module, "FLEET_SPAWN_ACK_BOUND_SECONDS", 1.0)
    stub = _StubBatchStep(runtime_dir, acknowledge=False)
    stub.start()
    try:
        started = time.monotonic()
        with pytest.raises(CrewError) as refusal:
            _dispatch(config_home, repo, "silent")
        elapsed = time.monotonic() - started

        assert len(stub.lines) == 1, "the request was never written"
        assert elapsed < FLEET_ACK_CEILING, elapsed
        assert elapsed >= 1.0, "the request was not waited on at all"
        assert "did not acknowledge" in str(refusal.value)
        assert runs.list_live(project="sample") == [], "a refusal left a pointer"
        worktree = tmp_path / "worktrees" / "session-silent-node-silent"
        assert not worktree.exists(), "a refusal left a worktree"
    finally:
        stub.stop()


def test_a_watch_producer_start_leaves_by_the_same_lane(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_home, _repo = host
    runtime_dir = _publish_fleet_record(tmp_path, monkeypatch)
    _opt_in(monkeypatch)
    # Arming is refused under a throwaway configuration home unless the caller
    # states it will reap the producer, which this case does.
    monkeypatch.setenv(WATCH_ARMING_ENV, "on")
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    stub = _StubBatchStep(runtime_dir)
    stub.start()
    try:
        handle = dispatch_module._start_watch_producer("sample")

        assert len(stub.lines) == 1, stub.lines
        verb, request_id, spec_path = stub.lines[0].split(" ")
        assert verb == "spawn"
        assert request_id == "watch-sample"
        assert Path(spec_path).name == "producer.json"
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        assert spec["project"] == "sample"
        assert "crew watch" in " ".join(spec["argv"])
        assert handle.pid == stub.supervisor_pid
    finally:
        stub.stop()


def test_a_review_dispatch_and_a_resumption_reach_the_supervisor(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither is spawned in-process, so neither is a child of whoever issued it.

    A review dispatch and a resumption both reach the supervisor with no
    launcher supplied, which is the path a sweep takes when it is nobody's
    parent: a launch made inline there would die with the sweeping session.
    """
    config_home, repo = host
    runtime_dir = _publish_fleet_record(tmp_path, monkeypatch)
    _opt_in(monkeypatch)
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    stub = _StubBatchStep(runtime_dir)
    stub.start()
    try:
        manifest = config_home / "manifests" / "r-scoring.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("status: complete\ncommits: r-scoring\n", encoding="utf-8")
        scoring = {
            "run_id": "r-scoring",
            "project": "sample",
            "repo": str(repo),
            "node": {"id": "r-scoring", "plan": "fixture", "section": "s10"},
            "backend": "alpha",
            "launch": "cli",
            "argv": ["codex"],
            "phase": "starting",
            "process_alive": False,
            "session": "session-orchestrating",
            "manifest_path": str(manifest),
            "worktree": str(repo),
        }
        crew._write_json(runs.pointer_path("r-scoring"), scoring)
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            report = recovery.dispatch_review_for_run(
                scoring, config=CONFIG, launcher=None
            )
        assert report["dispatched"] is True, report
        review_run_id = str(report["review_run_id"])
        assert runs.read_pointer(review_run_id)["pid"] == stub.supervisor_pid

        resume_id = "r-parked"
        runs.run_dir(resume_id).mkdir(parents=True, exist_ok=True)
        parked = {
            "run_id": resume_id,
            "project": "sample",
            "repo": str(repo),
            "worktree": str(repo),
            "backend": "alpha",
            "launch": "cli",
            "argv": ["codex"],
            "phase": "blocked",
            "process_alive": False,
            "session": "session-orchestrating",
        }
        crew._write_json(runs.pointer_path(resume_id), parked)

        plan = dispatch_module._backends.LaunchPlan(
            backend="alpha",
            dialect="codex",
            argv=["codex", "resume"],
            cwd=str(repo),
            stdin_text="",
            environment={},
            final_message_path=None,
            resumed_session="sess-1",
        )
        monkeypatch.setattr(resumption, "resume_plan", lambda *a, **k: plan)
        launched = resumption._resume(resume_id, parked, config=CONFIG, launcher=None)

        assert launched["pid"] == stub.supervisor_pid
        assert len(stub.lines) == 2, stub.lines
        spec_paths = [Path(line.split(" ")[2]) for line in stub.lines]
        assert all(path.name == "supervisor.json" for path in spec_paths)
        assert {path.parent.name for path in spec_paths} == {
            review_run_id,
            resume_id,
        }
        assert all(
            "__supervise__" in json.loads(path.read_text(encoding="utf-8"))["argv"]
            for path in spec_paths
        )
    finally:
        stub.stop()
