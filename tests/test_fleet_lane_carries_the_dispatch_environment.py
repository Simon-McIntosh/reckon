"""Gate: the fleet spawn lane carries the dispatcher's crew environment.

The allocation's batch step starts a per-run supervisor with its own
environment. A supervisor resolves the run it was asked to hold through
reckon's crew home, so one started under the batch step's home rather than the
dispatcher's finds no run and exits at once -- after the spawn has been
acknowledged. Dispatch then reports a live pid for a worker that never started.

The cases here run the REAL spawn verb against a real per-run supervisor, over
a temporary crew home the batch step deliberately does not share:

* the spec carries the dispatcher's crew-state environment, and the supervisor
  started by the real verb reaches the run and spawns its worker;
* a spec environment that is not a mapping, or whose values are not strings, is
  refused rather than passed on to the child.

The batch step is stood in for by its FIFO reader, and each case observes the
process it starts and the files those processes write rather than a return
value alone.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.crew import fleet_supervisor, runs
from reckon.crew.dispatch import (
    CREW_STATE_ENVIRONMENT,
    WATCH_ARMING_ENV,
    _carried_crew_environment,
)

# The variables that relocate reckon's crew state, written out so the carried
# list is checked against a statement rather than against itself.
CARRIED_NAMES = {
    "RECKON_HOME",
    "RECKON_STATE_ROOT",
    "RECKON_MOUNTS_PATH",
    "RECKON_FLIGHT_CONFIG",
    "RECKON_RUN_STORE",
}

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# The backend the fixture declares. Its command is a stub so the real supervisor
# can start it without a real harness on the machine.
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

WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05


def _wait_for(predicate, *, message: str, timeout: float = WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_SECONDS)
    pytest.fail(message)


class _RealSpawnBatchStep:
    """The allocation's batch step, running the REAL spawn verb.

    Unlike the stub in the fleet-lane test, this reader fabricates no
    acknowledgement: it calls ``fleet_supervisor.spawn_supervisor`` with an
    environment that deliberately differs from the dispatcher's crew home, so
    the case measures what the real supervisor does when it starts under the
    wrong home unless the spec carries the right one.
    """

    def __init__(self, runtime_dir: Path, batch_environ: dict[str, str]) -> None:
        self.fifo = runtime_dir / "requests"
        self.environ = batch_environ
        self.lines: list[str] = []
        self.spawned: list[dict] = []
        self.errors: list[BaseException] = []
        self._read_fd: int | None = None
        self._hold_fd: int | None = None
        self._opened = threading.Event()
        self._stop = threading.Event()

    def start(self) -> _RealSpawnBatchStep:
        os.mkfifo(self.fifo)
        threading.Thread(target=self._run, daemon=True).start()
        assert self._opened.wait(10.0), "the stub reader never opened the FIFO"
        return self

    def stop(self) -> None:
        self._stop.set()
        for descriptor in (self._read_fd, self._hold_fd):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

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
                try:
                    self._handle(line.decode())
                except BaseException as exc:  # noqa: BLE001 - reported to the case
                    self.errors.append(exc)

    def _handle(self, line: str) -> None:
        self.lines.append(line)
        fields = line.split(" ")
        assert len(fields) == 3 and fields[0] == "spawn", line
        run_id, spec_path = fields[1], fields[2]
        # The real verb, under this batch step's own environment.
        self.spawned.append(
            fleet_supervisor.spawn_supervisor(run_id, spec_path, self.environ)
        )


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """A temporary crew home, a mounted repository, and a stub ``codex`` marker."""
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

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", "--force", str(path), base_sha],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return {"path": str(path), "base": base, "base_sha": base_sha}

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    # The fence is off so the stub harness is the process the supervisor starts,
    # rather than a fence wrapper whose only job here would be to re-exec it.
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", False)
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", False)
    monkeypatch.setenv(WATCH_ARMING_ENV, "off")

    # A stub ``codex`` the supervisor can start: it records its pid and stays
    # alive so the supervisor holds a live worker while dispatch confirms it.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    marker = tmp_path / "worker-started"
    codex = stub_bin / "codex"
    codex.write_text(
        f'#!/bin/sh\necho "$$" > "{marker}"\nsleep 60\n',
        encoding="utf-8",
    )
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    return config_home, repo, marker


def _publish_fleet_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="launch one worker through the fleet's batch step",
        plan="fixture",
        section="s10",
        spec_level="guided",
        done_when=(
            "pytest reports one spawn line, the acknowledged pid on the pointer, "
            "and the stub worker's marker file"
        ),
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _dispatch(config_home: Path, repo: Path, name: str) -> dict:
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=f"session-{name}",
        launcher=None,
        watch_required=False,
        check_budget=False,
    )


def _kill_group(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def test_a_dispatch_confirms_its_supervisor_reached_the_run(
    host: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carried environment is what lets the real supervisor find the run.

    The batch step runs under a crew home with no run in it; the spec carries
    the dispatcher's. With the carry applied the supervisor reaches the run and
    starts the stub worker, and dispatch reports the acknowledged pid only after
    seeing the supervisor survive its first seconds.
    """
    config_home, repo, marker = host
    dispatcher_home = os.environ["RECKON_HOME"]
    # A second crew-state variable, so the spec is shown to carry the whole
    # set rather than the crew home alone.
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "runs.sqlite"))
    other_home = tmp_path / "batch-home"
    other_home.mkdir()
    batch_environ = {
        **os.environ,
        "RECKON_HOME": str(other_home),
        "PYTHONPATH": REPO_ROOT,
    }
    runtime_dir = _publish_fleet_record(tmp_path, monkeypatch)
    monkeypatch.setenv("RECKON_FLEET_SPAWN", "on")
    stub = _RealSpawnBatchStep(runtime_dir, batch_environ).start()
    supervisor_pid: int | None = None
    try:
        record = _dispatch(config_home, repo, "carried")
        supervisor_pid = int(record["pid"])
        assert not stub.errors, stub.errors

        assert len(stub.lines) == 1, stub.lines
        run_directory = runs.run_dir(record["run_id"])
        spec = json.loads(
            (run_directory / "supervisor.json").read_text(encoding="utf-8")
        )
        # The spec carries the dispatcher's crew home, not the batch step's,
        # and exactly the crew-state variables the dispatcher had set.
        assert isinstance(spec["environment"], dict), spec["environment"]
        assert spec["environment"]["RECKON_HOME"] == dispatcher_home, spec[
            "environment"
        ]
        assert spec["environment"] == {
            name: os.environ[name] for name in CARRIED_NAMES if os.environ.get(name)
        }, spec["environment"]
        assert spec["environment"]["RECKON_RUN_STORE"] == str(tmp_path / "runs.sqlite")

        # The real supervisor acknowledged, and dispatch reported its pid.
        assert stub.spawned and stub.spawned[0]["pid"] == supervisor_pid

        # The supervisor found the run and spawned the stub worker.
        _wait_for(
            marker.exists,
            message="the stub worker never started, so the supervisor exited first",
        )
        # The marker is written by the stub worker the moment it starts, while
        # the supervisor writes worker.json on its own schedule after the spawn
        # returns, so the marker alone does not mean the record exists yet.
        worker_record_path = run_directory / "worker.json"
        _wait_for(
            worker_record_path.exists,
            message="the supervisor never wrote the worker record for the run",
        )
        worker_record = json.loads(worker_record_path.read_text(encoding="utf-8"))
        assert worker_record["run_id"] == record["run_id"]
        # No exit record exists while dispatch reported the run as live.
        assert not (run_directory / "exit.json").exists()
    finally:
        stub.stop()
        _kill_group(supervisor_pid)


def test_the_carried_variables_are_the_ones_that_relocate_crew_state() -> None:
    """The carried list is exactly the crew-state variables, named here.

    The expected set is written out rather than read back from the constant,
    so a name dropped from the constant fails this case instead of passing it.
    """
    assert set(CREW_STATE_ENVIRONMENT) == CARRIED_NAMES


def test_every_set_crew_variable_is_carried_and_an_unset_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each set variable arrives with its value; an unset one stays absent.

    Absent and empty resolve differently in every reader that falls back on a
    default, so an unset variable must not arrive as an empty string.
    """
    for name in CARRIED_NAMES:
        monkeypatch.setenv(name, f"/carried/{name.lower()}")
    assert _carried_crew_environment() == {
        name: f"/carried/{name.lower()}" for name in CARRIED_NAMES
    }
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG")
    monkeypatch.setenv("RECKON_STATE_ROOT", "")
    carried = _carried_crew_environment()
    assert "RECKON_FLIGHT_CONFIG" not in carried
    assert "RECKON_STATE_ROOT" not in carried
    assert set(carried) == CARRIED_NAMES - {"RECKON_FLIGHT_CONFIG", "RECKON_STATE_ROOT"}


def _write_spec(run_directory: Path, environment: object) -> Path:
    run_directory.mkdir(parents=True, exist_ok=True)
    spec_path = run_directory / "supervisor.json"
    spec_path.write_text(
        json.dumps(
            {
                "run_id": "r-malformed",
                "run_directory": str(run_directory),
                "argv": [sys.executable, "-c", "pass"],
                "environment": environment,
            }
        ),
        encoding="utf-8",
    )
    return spec_path


def test_a_spec_environment_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    """A carried environment that is not a mapping is refused, never passed on."""
    spec_path = _write_spec(tmp_path / "run-list", ["RECKON_HOME", "/nonexistent-home"])
    with pytest.raises(TypeError, match="not a mapping"):
        fleet_supervisor.spawn_supervisor("r-malformed", str(spec_path), {})


def test_a_spec_environment_entry_that_is_not_a_string_is_refused(
    tmp_path: Path,
) -> None:
    """A non-string name or value is refused rather than handed to the child."""
    spec_path = _write_spec(tmp_path / "run-int", {"RECKON_HOME": 7})
    with pytest.raises(TypeError, match="not a string pair"):
        fleet_supervisor.spawn_supervisor("r-malformed", str(spec_path), {})
