"""Hermetic lifecycle contracts for project watcher registration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move watcher registration into an isolated configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def watcher_processes():
    """Terminate only watcher subprocesses started by the current test."""
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _arm_watcher(
    project: str, processes: list[subprocess.Popen[str]]
) -> subprocess.Popen[str]:
    source_root = Path(__file__).parents[1]
    script = f"""
import time
from reckon.crew import _project_watch_claim
with _project_watch_claim({project!r}, '1h') as (acquired, _record):
    print('ready' if acquired else 'refused', flush=True)
    if acquired:
        time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=source_root,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    processes.append(process)
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def _invoke_unwatch(project: str):
    return CliRunner().invoke(
        cli_module.main,
        ["crew", "unwatch", "--project", project],
    )


def test_unwatch_stops_clears_and_allows_rearming(home, watcher_processes) -> None:
    first = _arm_watcher("proj", watcher_processes)
    registered = crew.watch_state("proj")
    assert registered["watcher_live"] is True
    assert registered["watcher"]["pid"] == first.pid

    stopped = _invoke_unwatch("proj")

    assert stopped.exit_code == 0, stopped.output
    payload = json.loads(stopped.output)
    assert payload["ok"] is True
    assert payload["stopped"] is True
    assert payload["registration_released"] is True
    assert payload["watcher"]["pid"] == first.pid
    first.wait(timeout=5)
    assert crew.watch_state("proj")["watcher_live"] is False
    assert json.loads(crew.watch_lock_path("proj").read_text()) == {}

    again = _invoke_unwatch("proj")
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["reason"] == "nothing-to-stop"

    second = _arm_watcher("proj", watcher_processes)
    assert crew.watch_state("proj")["watcher"]["pid"] == second.pid
    rearmed_stop = _invoke_unwatch("proj")
    assert rearmed_stop.exit_code == 0, rearmed_stop.output
    assert json.loads(rearmed_stop.output)["stopped"] is True
    second.wait(timeout=5)


def test_unwatch_refuses_a_changed_process_identity(home, watcher_processes) -> None:
    watcher = _arm_watcher("proj", watcher_processes)
    path = crew.watch_lock_path("proj")
    with path.open("r+b") as handle:
        record = crew._read_watch_record(handle)
        record["pid_start_time"] = "not-the-running-process"
        crew._write_watch_record(handle, record)

    result = _invoke_unwatch("proj")

    assert result.exit_code == 1
    assert "process identity changed" in result.output
    assert watcher.poll() is None
    assert crew.watch_state("proj")["watcher_live"] is True
    assert json.loads(path.read_text())["pid_start_time"] == "not-the-running-process"


def test_unwatch_never_reaches_another_project(home, watcher_processes) -> None:
    other = _arm_watcher("other-project", watcher_processes)

    result = _invoke_unwatch("named-project")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stopped"] is False
    assert payload["reason"] == "nothing-to-stop"
    assert other.poll() is None
    assert crew.watch_state("other-project")["watcher"]["pid"] == other.pid

    cleanup = _invoke_unwatch("other-project")
    assert cleanup.exit_code == 0, cleanup.output
    other.wait(timeout=5)
