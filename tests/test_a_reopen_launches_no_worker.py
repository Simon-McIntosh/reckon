"""A reopen widens a run's scope and starts no worker.

``crew resume --reopen --write-path P`` answers a caller asking for a wider
boundary, not one asking for work. It used to fall through into the resume it
was combined with, so the attempt that started carried advice composed for the
state the run had left — and regularly consumed the advice a refused resume
had been holding for the next one.

Measured 2026-11-12: ``crew resume --reopen --write-path
reckon/crew/admission/launching.py`` on a completed run launched attempt 4 at
once, on instructions written for an earlier state, and emptied
``resume-refused-advice.txt`` into that attempt.

A reopen now makes the grant, records it and reports what a refused resume left
waiting. The resume that delivers that advice is the caller's own next call,
which carries it ahead of whatever it brings, exactly as before.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import resumption, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "fixture-project"
HOST = socket.gethostname()
MANIFEST_NS = int(datetime(2026, 9, 29, 10, 0, tzinfo=UTC).timestamp()) * 1_000_000_000

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "claude",
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

REFUSED = "widen the fence to the admission module before you continue"


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


@contextmanager
def _live_launcher():
    """A real running process standing in for a launch already under way."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    return config_home


def _completed_run(tmp_path: Path, run_id: str) -> dict:
    """A delivered run: pointer at phase complete, manifest reading complete."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text(f"node: {run_id}\nstatus: complete\n", encoding="utf-8")
    os.utime(manifest, ns=(MANIFEST_NS, MANIFEST_NS))
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["claude", "-p"],
        "role": "implement",
        "session_id": "fixture-session",
        "phase": "complete",
        "attempt": 3,
        "created_at": "2026-09-29T09:59:00+00:00",
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / f"{run_id}.jsonl"),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "worktree": str(worktree),
        "repo": str(worktree),
        "process_alive": None,
        "pid": _absent_pid(),
        "pid_start_time": None,
        "launcher_host": HOST,
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": ["reckon/crew/runs.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _refuse_a_resume_in_flight(run_id: str, launcher_pid: int) -> None:
    """Refuse one resume while a launch is under way, keeping its advice.

    This is the door's own plan builder, so the advice is kept by the refusal
    path exactly as a hand-typed resume keeps it, rather than by the test.
    """
    record = runs.read_pointer(run_id)
    record["resume_launch_in_flight"] = {
        "pid": launcher_pid,
        "at": "2026-11-12T10:00:05Z",
    }
    _write_json(pointer_path(run_id), record)
    with pytest.raises(CrewError) as raised:
        resume_plan(run_id, REFUSED, config=CONFIG)
    assert "already has a resume launching" in str(raised.value)


def _end_the_attempt(run_id: str) -> None:
    """The launch that held the run has ended, so the mark comes off."""
    record = runs.read_pointer(run_id)
    record.pop("resume_launch_in_flight", None)
    _write_json(pointer_path(run_id), record)


def _reopen(run_id: str, path: str, monkeypatch: pytest.MonkeyPatch):
    """Run the reopen verb, failing loudly if it starts an attempt."""
    started: list[tuple] = []

    def launcher(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("a reopen must launch nothing")

    monkeypatch.setattr("reckon.crew.resumption._resume", launcher)
    result = CliRunner().invoke(
        main,
        ["crew", "resume", "--run", run_id, "--reopen", "--write-path", path],
    )
    return result, started


def test_a_reopen_with_advice_waiting_starts_no_attempt(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reopen grants the path, launches nothing, and names the kept advice."""
    run_id = "r-reopen-launches-nothing"
    _completed_run(tmp_path, run_id)
    with _live_launcher() as launcher_pid:
        _refuse_a_resume_in_flight(run_id, launcher_pid)
        assert resumption.refused_advice_path(run_id).exists()
    _end_the_attempt(run_id)
    before = runs.read_pointer(run_id)

    result, started = _reopen(run_id, "reckon/crew/admission/launching.py", monkeypatch)

    assert result.exit_code == 0, result.output
    assert started == [], started
    after = runs.read_pointer(run_id)
    assert after["attempt"] == before["attempt"]
    assert after["pid"] == before["pid"]
    assert after["phase"] == before["phase"]
    assert "resume_launch_in_flight" not in after
    assert sorted(p.name for p in runs.run_dir(run_id).glob("resume-*.jsonl")) == []
    # The widening is the whole effect.
    assert after["node"]["write_paths"] == [
        "reckon/crew/runs.py",
        "reckon/crew/admission/launching.py",
    ]
    # The output names the advice still waiting for the next resume.
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["reopened"] is True
    assert payload["launched"] is False
    assert payload["refused_advice"] == REFUSED
    assert payload["refused_advice_path"] == str(resumption.refused_advice_path(run_id))
    assert REFUSED in result.output
    # Named, not consumed: the resume that follows still carries it.
    assert resumption.refused_advice_path(run_id).read_text().strip() == REFUSED


def test_the_resume_after_a_reopen_carries_the_refused_advice_first(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kept advice reaches the next attempt ahead of the caller's own."""
    run_id = "r-reopen-then-resume"
    _completed_run(tmp_path, run_id)
    with _live_launcher() as launcher_pid:
        _refuse_a_resume_in_flight(run_id, launcher_pid)
    _end_the_attempt(run_id)

    result, started = _reopen(run_id, "reckon/crew/claims.py", monkeypatch)
    assert result.exit_code == 0, result.output
    assert started == [], started

    plan = resume_plan(run_id, "carry on with the widened fence", config=CONFIG)

    prompt = plan.stdin_text
    assert REFUSED in prompt, prompt
    assert prompt.index(REFUSED) < prompt.index("carry on with the widened fence")
    assert not resumption.refused_advice_path(run_id).exists()


def test_a_reopen_with_nothing_waiting_names_no_advice(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no refused advice reports an empty slot, not a stale one."""
    run_id = "r-reopen-nothing-waiting"
    _completed_run(tmp_path, run_id)

    result, started = _reopen(run_id, "reckon/crew/claims.py", monkeypatch)

    assert result.exit_code == 0, result.output
    assert started == [], started
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["refused_advice"] == ""
    assert payload["refused_advice_path"] == ""
