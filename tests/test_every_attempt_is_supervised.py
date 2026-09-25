"""Every spawned execution attempt retains a supervisor and current evidence."""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import _backends, crew
from reckon import cli as cli_module
from reckon.crew import recovery, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "fixture-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written within {timeout:g}s")


def _wait_until(predicate, description: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{description} was not observed within {timeout:g}s")


def _record_names_attempt(path: Path, attempt: int) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("attempt") == attempt
    except (OSError, ValueError):
        return False


def _cmdline(pid: int) -> list[str]:
    return [
        part.decode(errors="replace")
        for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if part
    ]


def _stop_group(pid: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)


def _sleeping_plan(worktree: Path, dialect: str) -> _backends.LaunchPlan:
    return _backends.LaunchPlan(
        backend="alpha",
        dialect=dialect,
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(worktree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session="session-fixture",
    )


def _resumable_run(tmp_path: Path, dialect: str) -> tuple[str, Path, Path]:
    run_id = f"r-resume-{dialect}"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / f"worktree-{dialect}"
    worktree.mkdir()
    manifest = tmp_path / f"manifest-{dialect}.md"
    manifest.write_text(
        "status: complete\ncommits: [old-head]\nblockers: none\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "alpha",
        "launch": "cli",
        "dialect": dialect,
        "phase": "blocked",
        "pid": None,
        "session": "coordinator-fixture",
        "session_id": "session-fixture",
        "manifest_path": str(manifest),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-09-25T12:00:00Z",
        "created_at": "2026-09-25T12:00:00Z",
        "manifest_baseline_mtime_ns": 0,
        "node": {
            "id": f"resume-{dialect}",
            "goal": "exercise one resumed worker",
            "plan": "fixture",
            "section": "runtime",
            "role": "implement",
            "spec_level": "exact",
            "done_when": "the current attempt is supervised",
            "write_paths": ["target.py"],
            "time_budget": "20m",
            "manifest_path": str(manifest),
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    (directory / "worker.json").write_text(
        json.dumps({"run_id": run_id, "pid": 999_991, "backend": "old"}),
        encoding="utf-8",
    )
    (directory / "exit.json").write_text(
        json.dumps({"run_id": run_id, "worker_pid": 999_991, "exit_code": 0}),
        encoding="utf-8",
    )
    return run_id, directory, manifest


def _assert_current_attempt(
    run_id: str, directory: Path, supervisor_pid: int
) -> dict[str, object]:
    assert "__supervise__" in _cmdline(supervisor_pid)
    _wait_for(directory / "worker.json")
    worker = json.loads((directory / "worker.json").read_text(encoding="utf-8"))
    assert int(worker["pid"]) != supervisor_pid
    assert worker["attempt"] == 2
    assert (
        json.loads((directory / "attempt-1-worker.json").read_text(encoding="utf-8"))[
            "attempt"
        ]
        == 1
    )
    assert (
        json.loads((directory / "attempt-1-exit.json").read_text(encoding="utf-8"))[
            "attempt"
        ]
        == 1
    )
    assert (
        json.loads((directory / "attempt-2-worker.json").read_text(encoding="utf-8"))
        == worker
    )
    assert runs.read_pointer(run_id)["pid"] == supervisor_pid
    return worker


def _kill_worker_and_read_exit(directory: Path, worker_pid: int) -> dict[str, object]:
    os.kill(worker_pid, signal.SIGKILL)
    _wait_for(directory / "exit.json")
    _wait_until(
        lambda: _record_names_attempt(directory / "exit.json", 2),
        "the current attempt exit record",
    )
    exit_record = json.loads((directory / "exit.json").read_text(encoding="utf-8"))
    assert exit_record["attempt"] == 2
    assert exit_record["worker_pid"] == worker_pid
    assert (
        json.loads((directory / "attempt-2-exit.json").read_text(encoding="utf-8"))
        == exit_record
    )
    return exit_record


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_crew_resume_keeps_the_harness_behind_a_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialect: str
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id, directory, _manifest = _resumable_run(tmp_path, dialect)
    plan = _sleeping_plan(Path(runs.read_pointer(run_id)["worktree"]), dialect)
    monkeypatch.setattr(crew, "resume_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        cli_module,
        "_resolved_flight",
        lambda flight_module, project, checkout_path, overrides: CONFIG,
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "resume", "--run", run_id, "--advice", "continue"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    supervisor_pid = int(payload["pid"])
    try:
        worker = _assert_current_attempt(run_id, directory, supervisor_pid)
        Path(runs.read_pointer(run_id)["worktree"], "repair.txt").write_text(
            "uncommitted repair\n", encoding="utf-8"
        )
        row = recovery.classify_pointer(runs.read_pointer(run_id))
        assert row["classification"] == "running"
        assert row["classification"] not in {"scoring", "promotable"}
        reflex = recovery.dispatch_review_for_run(
            runs.read_pointer(run_id),
            launcher=lambda *args, **kwargs: pytest.fail(
                "a live attempt must not dispatch its review"
            ),
        )
        assert reflex["dispatched"] is False
        assert "running" in reflex["reason"]
        exit_record = _kill_worker_and_read_exit(directory, int(worker["pid"]))
        assert exit_record["signal_name"] == "SIGKILL"
    finally:
        _stop_group(supervisor_pid)


def test_a_killed_resumed_worker_leaves_its_attempt_exit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id, directory, _manifest = _resumable_run(tmp_path, "codex")
    plan = _sleeping_plan(Path(runs.read_pointer(run_id)["worktree"]), "codex")
    monkeypatch.setattr(crew, "resume_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        cli_module,
        "_resolved_flight",
        lambda flight_module, project, checkout_path, overrides: CONFIG,
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "resume", "--run", run_id, "--advice", "continue"],
    )
    assert result.exit_code == 0, result.output
    launched_pid = int(json.loads(result.output)["pid"])
    try:
        if "__supervise__" in _cmdline(launched_pid):
            _wait_for(directory / "worker.json")
            worker_pid = int(
                json.loads((directory / "worker.json").read_text(encoding="utf-8"))[
                    "pid"
                ]
            )
        else:
            worker_pid = launched_pid
        os.kill(worker_pid, signal.SIGKILL)
        _wait_until(
            lambda: _record_names_attempt(directory / "exit.json", 2),
            "the killed resume's current-attempt exit record",
            timeout=3.0,
        )
    finally:
        _stop_group(launched_pid)


def _lane_resolution(dialect: str) -> SimpleNamespace:
    command = "claude" if dialect == "claude" else "codex"
    backend = {
        "launch": "cli",
        "command": command,
        "model": "fixture-model",
        "effort": "medium",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "20m",
    }
    return SimpleNamespace(
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        backend_settings=backend,
        backend="beta",
        launch="cli",
        authority={},
        sandbox_write_roots=None,
    )


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_crew_redispatch_keeps_the_harness_behind_a_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialect: str
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id, directory, _manifest = _resumable_run(tmp_path, dialect)
    pointer = runs.read_pointer(run_id)
    pointer["session_harness"] = dialect
    pointer["session_model"] = "fixture-model"
    runs._write_json(runs.pointer_path(run_id), pointer)
    plan = _sleeping_plan(Path(pointer["worktree"]), dialect)
    resolution = _lane_resolution(dialect)
    monkeypatch.setattr(dispatch_module, "plan_dispatch", lambda **kwargs: resolution)
    monkeypatch.setattr(
        dispatch_module, "resolve_dispatch_ledger_root", lambda authority: tmp_path
    )
    monkeypatch.setattr(
        dispatch_module,
        "_budget_verdict",
        lambda **kwargs: {"held": False, "backend": "beta"},
    )
    monkeypatch.setattr(dispatch_module._backends, "launch_plan", lambda **kwargs: plan)

    moved = dispatch_module.change_lane(
        run_id,
        "beta",
        "the source lane cannot continue",
        config={"backends": {"alpha": resolution.backend_settings}},
        advice="continue",
    )
    supervisor_pid = int(moved["pid"])
    try:
        worker = _assert_current_attempt(run_id, directory, supervisor_pid)
        assert moved["attempt"] == 2
        assert moved["attempt_kind"] == "lane-change"
        assert moved["lane_change"]["to_backend"] == "beta"
        _kill_worker_and_read_exit(directory, int(worker["pid"]))
    finally:
        _stop_group(supervisor_pid)
