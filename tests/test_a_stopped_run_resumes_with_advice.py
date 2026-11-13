"""A stopped process tree leaves an exit receipt that licenses advice."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew_run_commands
from reckon.crew import (
    dispatch_launch,
    dispatch_sessions,
    recovery_liveness,
    resumption,
    runs,
)
from reckon.crew_run_commands import crew_resume, crew_stop
from tests.helpers.git import git

ADVICE = "Continue the session and submit the pending job copy."
# The scheduler client has no job identifier, so only the run's local process
# tree or an exit receipt can settle its end.
PLACEMENT = {"scheduler": "srun", "job_id_status": "no-probe-declared"}
SUPERVISOR = r"""
import json, os, subprocess, sys, time
from pathlib import Path
worker_code = ("import signal, sys, time\nfrom pathlib import Path\n"
    "if sys.argv[2] == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "Path(sys.argv[1]).write_text('ready')\ntime.sleep(120)\n")
worker = subprocess.Popen([sys.executable, '-c', worker_code, sys.argv[3], sys.argv[4]])
Path(sys.argv[1]).write_text(json.dumps({'run_id': sys.argv[2], 'attempt': 1,
    'pid': worker.pid}))
worker.wait()
"""


def _wait(predicate, detail):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(detail)


@pytest.fixture()
def fixture(tmp_path, monkeypatch):
    operator_home = runs.crew_home()
    run_id = f"r-stop-advice-{os.getpid()}-{os.urandom(6).hex()}"
    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "mounts.json"))
    (tmp_path / "mounts.json").write_text("{}")
    monkeypatch.setenv("RECKON_FLEET_SPAWN", "")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    monkeypatch.setattr(resumption, "_require_fleet_gate_open", dict)
    monkeypatch.setattr(dispatch_launch, "_require_fleet_gate_open", dict)
    from reckon.crew import dispatch_watch

    monkeypatch.setattr(
        dispatch_watch, "_rearm_session_follower", lambda *a: (False, "fixture")
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "source.txt").write_text("committed work\n")
    git(repository, "add", "source.txt")
    git(
        repository,
        "commit",
        "-qm",
        "test: create fixture\n\nKeep the stopped worktree readable.",
    )
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    received = tmp_path / "received.json"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(received)!r}).write_text(json.dumps({{'argv': sys.argv, 'stdin': sys.stdin.read()}}))\n"
        'print(\'{"type":"thread.started","thread_id":"fixture-session"}\', flush=True)\n'
    )
    binary.chmod(0o755)
    config = {
        "default_backend": "stub",
        "backends": {
            "stub": {
                "launch": "cli",
                "command": str(binary),
                "time_budget": "20m",
                "session_reuse": True,
            }
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }
    monkeypatch.setattr(crew_run_commands, "_resolved_flight", lambda *a, **k: config)
    now = runs._utc_now()
    record = {
        "run_id": run_id,
        "project": "fixture",
        "repo": str(repository),
        "worktree": str(repository),
        "backend": "stub",
        "launch": "cli",
        "dialect": "codex",
        "phase": "working",
        "session_id": "fixture-session",
        "launcher_host": socket.gethostname(),
        "pid": None,
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": now,
        "created_at": now,
        "placement": PLACEMENT,
        "job_id": None,
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "node": {
            "id": "stopped-advice",
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    (directory / "stream.jsonl").write_text(
        '{"type":"thread.started","thread_id":"fixture-session"}\n'
    )
    runs._write_json(directory / "attempt.json", {"run_id": run_id, "attempt": 1})
    runs._write_json(runs.pointer_path(run_id), record)
    assert runs.crew_home().is_relative_to(tmp_path)
    yield {
        "run_id": run_id,
        "directory": directory,
        "record": record,
        "received": received,
        "tmp_path": tmp_path,
        "config": config,
    }
    # These unique fixture paths must never appear in the operator's crew home.
    assert not (operator_home / "runs" / run_id).exists()
    assert not (operator_home / "live" / f"{run_id}.json").exists()


@contextmanager
def _process_tree(fixture, *, ignores_stop=False):
    directory = fixture["directory"]
    ready = fixture["tmp_path"] / "ready"
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            SUPERVISOR,
            str(directory / "worker.json"),
            fixture["run_id"],
            str(ready),
            "ignore" if ignores_stop else "end",
        ],
        start_new_session=True,
        env={**os.environ, "RECKON_RUN_ID": fixture["run_id"]},
    )
    worker_pid = None
    try:
        _wait(
            lambda: ready.exists() and (directory / "worker.json").exists(),
            "the supervisor did not register its worker",
        )
        worker_pid = json.loads((directory / "worker.json").read_text())["pid"]
        record = fixture["record"]
        record["pid"] = supervisor.pid
        record["pid_start_time"] = runs._process_start_time(supervisor.pid)
        runs._write_json(runs.pointer_path(fixture["run_id"]), record)
        assert runs.process_alive(supervisor.pid) is True
        assert runs.process_alive(worker_pid) is True
        yield supervisor, worker_pid
    finally:
        survivors = [
            pid
            for pid in (worker_pid, supervisor.pid)
            if pid and runs.process_alive(pid) is True
        ]
        print("fixture cleanup pids:", survivors)
        for pid in survivors:
            os.kill(pid, signal.SIGKILL)
        supervisor.wait(timeout=15)


def _resume(fixture):
    result = CliRunner().invoke(
        crew_resume, ["--run", fixture["run_id"], "--advice", ADVICE]
    )
    assert result.exit_code == 0, result.output
    _wait(fixture["received"].exists, "the resumed stub did not receive advice")
    received = json.loads(fixture["received"].read_text())
    assert ADVICE in received["stdin"] or any(ADVICE in arg for arg in received["argv"])
    assert "fixture-session" in received["argv"]
    pointer = runs.read_pointer(fixture["run_id"])
    assert pointer["attempt"] == 2
    assert pointer["attempt_kind"] == "resume"
    _wait(
        lambda: runs.process_alive(pointer["pid"]) is False,
        "the resumed supervisor did not finish",
    )
    return result


def test_a_stopped_tree_records_its_exit_and_resumes_with_advice(fixture, monkeypatch):
    if os.environ.get("RECKON_STOP_ADVICE_NEGATIVE_CONTROL"):
        monkeypatch.setattr(
            dispatch_sessions, "_record_stopped_attempt_exit", lambda *a: False
        )
    with _process_tree(fixture) as (supervisor, worker_pid):
        result = CliRunner().invoke(crew_stop, ["--run", fixture["run_id"]])
        assert result.exit_code == 0, result.output
        supervisor.wait(timeout=15)
        assert runs.process_alive(supervisor.pid) is False
        assert runs.process_alive(worker_pid) is False
        record = runs.read_pointer(fixture["run_id"])
        # Ask the resume door before inspecting the receipt, so the mutation
        # proves the missing receipt refuses the advice itself.
        reading = resumption._observed_process_reading(record)
        _resume(fixture)
        receipt = json.loads((fixture["directory"] / "attempt-1-exit.json").read_text())
        assert receipt["run_id"] == fixture["run_id"]
        assert receipt["attempt"] == 1
        assert receipt["reason"] == "run-stop"
        assert "crew stop" in receipt["detail"]
        assert reading == "process gone"


def test_a_surviving_worker_has_no_exit_and_refuses_the_advice(fixture, monkeypatch):
    with _process_tree(fixture, ignores_stop=True) as (supervisor, worker_pid):
        # The worker ignores the actual SIGTERM; a refused escalation leaves
        # it alive, so the receipt and resume guards must still see it.
        monkeypatch.setattr(dispatch_sessions, "_escalate_run_stop", lambda *a, **k: [])
        monkeypatch.setattr(dispatch_sessions, "_STOP_SETTLE_SECONDS", 0.05)
        result = CliRunner().invoke(crew_stop, ["--run", fixture["run_id"]])
        assert result.exit_code == 0, result.output
        stopped = json.loads(result.output)
        assert worker_pid in stopped["alive_after"]
        assert str(worker_pid) in stopped["detail"]
        assert not (fixture["directory"] / "exit.json").exists()
        assert not (fixture["directory"] / "attempt-1-exit.json").exists()
        supervisor.wait(timeout=15)
        record = runs.read_pointer(fixture["run_id"])
        assert recovery_liveness.local_liveness(record) == (True, True)
        result = CliRunner().invoke(
            crew_resume, ["--run", fixture["run_id"], "--advice", ADVICE]
        )
        assert result.exit_code != 0
        assert str(worker_pid) in result.output
        assert "alive" in result.output
        assert not fixture["received"].exists()


def test_a_legacy_stopped_run_with_no_exit_resumes_on_its_launch_host(fixture):
    with _process_tree(fixture) as (supervisor, worker_pid):
        for pid in (worker_pid, supervisor.pid):
            os.kill(pid, signal.SIGTERM)
        supervisor.wait(timeout=15)
        _wait(
            lambda: runs.process_alive(worker_pid) is False,
            "the legacy worker is alive",
        )
        record = fixture["record"]
        record.update(phase="stopped", stopped_at=runs._utc_now(), alive_after=[])
        runs._write_json(runs.pointer_path(fixture["run_id"]), record)
        assert not (fixture["directory"] / "exit.json").exists()
        assert recovery_liveness.local_liveness(record) == (False, True)
        _resume(fixture)


def test_a_legacy_stop_does_not_prove_death_on_another_host(fixture):
    record = fixture["record"]
    record.update(
        phase="stopped",
        stopped_at=runs._utc_now(),
        alive_after=[],
        pid=999999999,
        launcher_host="another-host",
        process_alive=None,
    )
    assert recovery_liveness.local_liveness(record) == (None, False)
    assert resumption._observed_end_refusal(record) is not None


def test_a_legacy_stop_with_a_scheduler_job_keeps_its_unknown_reading(fixture):
    record = fixture["record"]
    record.update(
        phase="stopped",
        stopped_at=runs._utc_now(),
        alive_after=[],
        pid=999999999,
        job_id="unqueryable-fixture-job",
    )
    assert recovery_liveness.local_liveness(record) == (None, True)
    assert resumption._observed_end_refusal(record) is not None


def test_a_legacy_stop_still_finds_its_surviving_worker(fixture):
    with _process_tree(fixture) as (supervisor, worker_pid):
        supervisor.terminate()
        supervisor.wait(timeout=15)
        record = fixture["record"]
        record.update(phase="stopped", stopped_at=runs._utc_now(), alive_after=[])
        assert recovery_liveness.local_liveness(record) == (True, True)
        refusal = resumption._observed_end_refusal(record)
        assert refusal is not None
        assert str(worker_pid) in str(refusal)


def test_a_stop_keeps_the_supervisors_immutable_exit_at_its_attempt(fixture):
    with _process_tree(fixture) as (supervisor, worker_pid):
        receipt = {
            "run_id": fixture["run_id"],
            "attempt": 1,
            "recorded_by": "supervisor",
            "worker_pid": worker_pid,
            "signal": 15,
            "signal_name": "SIGTERM",
        }
        directory = fixture["directory"]
        numbered = directory / "attempt-1-exit.json"
        runs._write_json(numbered, receipt)
        runs._write_json(directory / "exit.json", receipt)
        before = numbered.read_bytes()
        result = CliRunner().invoke(crew_stop, ["--run", fixture["run_id"]])
        assert result.exit_code == 0, result.output
        supervisor.wait(timeout=15)
        assert numbered.read_bytes() == before
        assert not (directory / "attempt-2-exit.json").exists()
        canonical = json.loads((directory / "exit.json").read_text())
        assert canonical["attempt"] == 1
        assert canonical["recorded_by"] == "supervisor"
        assert canonical["stop"]["reason"] == "run-stop"
