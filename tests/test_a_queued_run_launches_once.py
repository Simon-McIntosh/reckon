"""A run whose worker is live refuses a second launch, naming the worker it found.

A queued run admitted twice was launched into one worktree: attempt 1 recorded
its worker pid while attempt 2 recorded its own, both ran, and the two wrote
the same manifest in turn from their own partial views.

A refusal is only checkable if it names the process a reader can look up, so
every case here asserts the whole refusal text. A worker found by its session
has no pid to name — the recorded one is dead — and a placed run's recorded pid
belongs to the scheduler client, so those arms name the session and the job.

Every case works against a temporary crew home and a temporary run directory,
so none reads or writes the operator's configuration home.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import process_liveness, runs
from reckon.crew.node import CrewError
from reckon.crew.recovery_liveness import _reading_host

RUN_ID = "r-queued-launch-once"
SESSION_ID = "6f1c2d34-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
JOB_ID = "1291586"

SPAWN_TAIL = (
    "a launch that started now would run a second worker beside it; "
    "observe or stop it before launching again"
)
PREPARE_TAIL = (
    "a launch that started now would retire its worker record, so the run "
    "would read as halted while its worker kept working; observe or stop it "
    "before launching again"
)


def _spawn_refusal(holder: str) -> str:
    """The whole text a refusal at the supervisor's spawn must carry."""
    return f"run {RUN_ID!r} still has a live worker ({holder}): {SPAWN_TAIL}"


def _prepare_refusal(holder: str) -> str:
    """The whole text a refusal at the attempt-record preparation must carry."""
    return f"run {RUN_ID!r} still has a live worker ({holder}): {PREPARE_TAIL}"


def _listing(directory: Path) -> list[str] | None:
    """The names directly under a directory, or None when it does not exist."""
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs.crew_home()
    listing_of_the_real_home_before = _listing(real)
    yield
    assert _listing(real) == listing_of_the_real_home_before, (
        "the case wrote into the real crew home"
    )


@pytest.fixture()
def queued_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run whose second attempt is starting."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    runs._write_json(runs.pointer_path(RUN_ID), _pointer(_dead_pid()))
    # The launcher publishes this attempt's identity before it starts the
    # supervisor, so the spawn seam reads the same marker a real launch does.
    runs._write_json(
        directory / "attempt.json",
        {
            "run_id": RUN_ID,
            "attempt": 2,
            "attempt_kind": "dispatch",
            "attempt_started_at": "2026-11-07T00:00:00Z",
        },
    )
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(recorded_pid: int, **extra: object) -> dict:
    """The run's pointer naming an attempt, with the fields a launch reads."""
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "launcher_host": _reading_host(),
        "attempt": 2,
        "attempt_kind": "dispatch",
        "session": "session-1",
        "session_id": SESSION_ID,
        "pid": recorded_pid,
        "worktree": str(runs.run_dir(RUN_ID)),
        "manifest_path": str(runs.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs.run_dir(RUN_ID) / "stream.jsonl"),
        "stderr_path": str(runs.run_dir(RUN_ID) / "stderr.log"),
        "node": {"id": "node-queued-launch-once", "plan": "fixture"},
        **extra,
    }


def _rewrite_pointer(**fields: object) -> None:
    """Replace fields of the pointer this case's launch reads."""
    pointer = json.loads(runs.pointer_path(RUN_ID).read_text(encoding="utf-8"))
    pointer.update(fields)
    runs._write_json(runs.pointer_path(RUN_ID), pointer)


def _spec(directory: Path) -> dict:
    """The supervisor's spec for a second attempt of the fixture run."""
    return {
        "run_id": RUN_ID,
        "run_directory": str(directory),
        "attempt": 2,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-11-07T00:00:00Z",
        "prompt_path": str(directory / "prompt.txt"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "plan": {
            "argv": [sys.executable, "-c", "pass"],
            "cwd": str(directory),
            "environment": {},
            "dialect": "",
        },
    }


def _write_worker_record(directory: Path, pid: int) -> dict:
    """File the incumbent attempt's worker record, as its supervisor wrote it."""
    record = {
        "run_id": RUN_ID,
        "attempt": 1,
        "pid": pid,
        "launched_at": "2026-11-07T00:00:00Z",
        "backend": "probe",
        "argv": [],
    }
    runs._write_json(directory / "worker.json", record)
    return record


def _live_child(*extra: str) -> subprocess.Popen:
    """A process that stays alive until the case stops it."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", *extra]
    )


def _dead_pid() -> int:
    """A pid whose process has exited and been collected."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _records(directory: Path) -> dict[str, str]:
    """Everything the run directory says about its attempts, as text, to compare."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.json"))
    }


def test_a_second_launch_beside_a_live_recorded_pid_is_refused(
    queued_run: Path,
) -> None:
    worker = _live_child()
    try:
        _rewrite_pointer(pid=worker.pid)
        _write_worker_record(queued_run, _dead_pid())
        before = _records(queued_run)
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert str(refused.value) == _spawn_refusal(f"pid {worker.pid}")
        # A removal of the liveness read lets the launch through here, so this
        # assertion is what reddens on that regression.
        assert _records(queued_run) == before, (
            "the refused launch rewrote the run's attempt records"
        )
    finally:
        _stop(worker)


def test_a_worker_found_by_its_session_is_named_by_its_session(
    queued_run: Path,
) -> None:
    """The recorded pid is dead, so naming it points at no process at all."""
    recorded = _dead_pid()
    _rewrite_pointer(pid=recorded)
    _write_worker_record(queued_run, _dead_pid())
    stand_in = _live_child(SESSION_ID)
    try:
        before = _records(queued_run)
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert str(refused.value) == _spawn_refusal(f"session {SESSION_ID}")
        assert f"pid {recorded}" not in str(refused.value), refused.value
        assert _records(queued_run) == before, (
            "the refused launch rewrote the run's attempt records"
        )
    finally:
        _stop(stand_in)


def test_a_placed_runs_scheduler_client_pid_is_not_named_as_the_worker(
    queued_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placed run's recorded pid is the client's, so the refusal names the job."""
    client = _live_child()
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: True)
    try:
        _rewrite_pointer(
            pid=client.pid,
            job_id=JOB_ID,
            placement={"partition": "betelgeuse", "nodes": 1},
        )
        _write_worker_record(queued_run, _dead_pid())
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
        assert str(refused.value) == _spawn_refusal(f"job {JOB_ID}")
        assert f"pid {client.pid}" not in str(refused.value), refused.value
    finally:
        _stop(client)


def test_a_launch_after_the_worker_exited_proceeds(queued_run: Path) -> None:
    _write_worker_record(queued_run, _dead_pid())
    pid = dispatch_launch_module._supervisor_spawn_worker(_spec(queued_run))
    assert pid > 0
    record = json.loads((queued_run / "worker.json").read_text(encoding="utf-8"))
    assert record["attempt"] == 2, "the launched attempt did not record its worker"


def test_a_fresh_dispatch_is_refused_through_the_session_walk(
    queued_run: Path,
) -> None:
    """The launcher's own refusal names the session too, not the dead pid.

    A dispatch that reaches the attempt-record preparation is a launch that
    never got as far as a supervisor, and it is refused from the same reading.
    """
    recorded = _dead_pid()
    _rewrite_pointer(pid=recorded)
    _write_worker_record(queued_run, _dead_pid())
    stand_in = _live_child(SESSION_ID)
    try:
        before = _records(queued_run)
        with pytest.raises(CrewError) as refused:
            dispatch_launch_module._prepare_attempt_records(
                queued_run,
                run_id=RUN_ID,
                attempt=2,
                attempt_kind="dispatch",
                attempt_started_at="2026-11-07T00:00:00Z",
            )
        assert str(refused.value) == _prepare_refusal(f"session {SESSION_ID}")
        assert f"pid {recorded}" not in str(refused.value), refused.value
        assert _records(queued_run) == before, (
            "the refused dispatch rewrote the run's attempt records"
        )
    finally:
        _stop(stand_in)
