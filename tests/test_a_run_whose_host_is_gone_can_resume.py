"""Scheduler observations release workers stranded on an ended compute host."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch as launch
from reckon.crew import dispatch_sessions as sessions
from reckon.crew import recovery_liveness as liveness
from reckon.crew import runs
from reckon.crew.node import CrewError

RUN_ID = "r-vacated-compute-host"
COMPUTE = "compute-fixture"
LOGIN = "login-fixture"
CONFIG = {
    "backends": {
        "probe": {
            "launch": "cli",
            "command": sys.executable,
            "dialect": "codex",
            "sandbox": "worktree-full",
            "session_reuse": True,
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m"},
}


@pytest.fixture()
def stranded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = runs.crew_home()
    real_paths = [real / "live" / f"{RUN_ID}.json", real / "runs" / RUN_ID]
    before = [path.exists() for path in real_paths]
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    state = tmp_path / "scheduler.json"
    calls = tmp_path / "scheduler-calls.jsonl"
    state.write_text(json.dumps({"jobs": "", "fail": False}))
    script = (
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"state = json.loads(pathlib.Path({str(state)!r}).read_text())\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as output:\n"
        "    output.write(json.dumps(sys.argv) + '\\n')\n"
        "if state['fail']: sys.exit(1)\n"
        "if pathlib.Path(sys.argv[0]).name == 'scontrol':\n"
        f"    if {LOGIN!r} in sys.argv: sys.exit(1)\n"
        f"    print('NodeName={COMPUTE} NodeHostName={COMPUTE} State=IDLE')\n"
        "else: print(state['jobs'], end='')\n"
    )
    for name in ("scontrol", "squeue"):
        executable = bin_path / name
        executable.write_text(script)
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_path}{os.pathsep}{os.environ['PATH']}")
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    tree = tmp_path / "tree"
    tree.mkdir()
    record = {
        "run_id": RUN_ID,
        "project": "scheduler-fixture",
        "repo": str(tree),
        "worktree": str(tree),
        "launch": "cli",
        "backend": "probe",
        "role": "implement",
        "launcher_host": COMPUTE + ".example.invalid",
        "pid": os.getpid(),
        "pid_start_time": runs._process_start_time(os.getpid()),
        "process_alive": None,
        "phase": "working",
        "attempt": 1,
        "session_id": "retained-session",
        "created_at": "2026-10-01T00:00:00Z",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(directory / "manifest.md"),
        "node": {"id": "stranded-worker", "time_budget": "25m", "write_paths": []},
    }
    runs._write_json(runs.pointer_path(RUN_ID), record)
    runs._write_json(
        directory / "worker.json", {"run_id": RUN_ID, "pid": os.getpid(), "attempt": 1}
    )
    runs._write_json(
        directory / "attempt-1.reservation",
        {
            "run_id": RUN_ID,
            "attempt": 1,
            "kind": "resume",
            "pid": os.getpid(),
            "launcher_host": record["launcher_host"],
            "at": record["created_at"],
        },
    )
    yield record, state, calls, directory
    assert [path.exists() for path in real_paths] == before


def _prepare(directory: Path) -> None:
    launch._prepare_attempt_records(
        directory,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="resume",
        attempt_started_at="2026-10-01T01:00:00Z",
    )


def test_a_vacated_compute_host_allows_resume_and_records_stop(stranded, monkeypatch):
    record, _state, calls, directory = stranded
    # The foreign pid deliberately collides with this live process. No local
    # process-table answer can prove anything about the remote worker.
    assert runs.process_alive(record["pid"]) is True
    _prepare(directory)
    plan = sessions.resume_plan(RUN_ID, "continue retained work", config=CONFIG)
    assert plan.resumed_session == "retained-session"
    assert plan.cwd == record["worktree"]
    assert json.loads((directory / "attempt.json").read_text())["attempt"] == 2
    observation = json.loads((directory / "launcher-host-observation.json").read_text())
    assert observation["host"] == record["launcher_host"]
    assert observation["jobs_output"] == ""
    assert observation["process_gone"] is True
    assert observation["observed_at"]
    queries = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any(Path(query[0]).name == "scontrol" for query in queries)
    assert any(
        Path(query[0]).name == "squeue" and str(os.getuid()) in query
        for query in queries
    )

    def no_local_signal(*args, **kwargs):
        pytest.fail("a foreign pid must never be signalled on this host")

    monkeypatch.setattr(sessions, "_signal_process_group", no_local_signal)
    monkeypatch.setattr(sessions, "signal_worker", no_local_signal)
    stopped = sessions.terminate(RUN_ID)
    assert stopped["stop_exit_recorded"] is True
    assert stopped["ended_pids"] == []
    receipt = json.loads((directory / "exit.json").read_text())
    assert receipt["run_id"] == RUN_ID
    assert receipt["recorded_by"] == "crew-stop"
    assert receipt["launcher_host_observation"]["process_gone"] is True
    assert runs.read_pointer(RUN_ID)["launcher_host_observation"]["jobs_output"] == ""


@pytest.mark.parametrize("kind", ["occupied", "login", "failed"])
def test_an_unsettled_host_keeps_the_refusal(stranded, kind):
    record, state, calls, directory = stranded
    if kind == "occupied":
        state.write_text(json.dumps({"jobs": "123|RUNNING\n", "fail": False}))
    elif kind == "login":
        record["launcher_host"] = LOGIN
        runs._write_json(runs.pointer_path(RUN_ID), record)
    else:
        state.write_text(json.dumps({"jobs": "", "fail": True}))
    with pytest.raises(CrewError):
        sessions.resume_plan(RUN_ID, "continue", config=CONFIG)
    with pytest.raises(CrewError, match=record["launcher_host"]):
        _prepare(directory)
    with pytest.raises(CrewError):
        sessions.terminate(RUN_ID)
    assert not (directory / "exit.json").exists()
    assert liveness.local_liveness(record) == (None, False)
    observation = json.loads((directory / "launcher-host-observation.json").read_text())
    assert observation["process_gone"] is False
    if kind == "occupied":
        assert observation["jobs_output"] == "123|RUNNING\n"
    else:
        assert observation["jobs_output"] is None
    assert calls.is_file()
