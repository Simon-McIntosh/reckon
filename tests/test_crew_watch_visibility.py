"""Hermetic read-surface coverage for project watcher visibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, mcp


def _read_cli(project: str) -> dict:
    result = CliRunner().invoke(
        cli_module.main, ["crew", "list", "--project", project]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["watcher"]


def _arm_watcher(project: str, source_root: Path) -> subprocess.Popen[str]:
    script = f"""
import time
from reckon.crew import _project_watch_claim
with _project_watch_claim({project!r}, '1h') as (acquired, record):
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
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def test_watcher_visibility_tracks_registration_liveness(tmp_path, monkeypatch) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    project = "synthetic-project"
    crew._write_json(
        crew.pointer_path("live-run"),
        {
            "run_id": "live-run",
            "project": project,
            "phase": "working",
            "node": {"id": "worker", "plan": "delivery"},
        },
    )

    unwatched = mcp._crew(project, view="live")["watcher"]
    assert unwatched == _read_cli(project)
    assert unwatched["status"] == "unwatched"
    assert unwatched["seat_held"] is False
    assert unwatched["watcher_live"] is False
    assert unwatched["pointer_count"] == 1
    assert unwatched["arming_line"] == (
        f"reckon crew watch --project {project}"
    )

    process = _arm_watcher(project, Path(__file__).parents[1])
    try:
        watched = mcp._crew(project, view="live")["watcher"]
        assert watched == _read_cli(project)
        assert watched["status"] == "watched"
        assert watched["seat_held"] is True
        assert watched["watcher_live"] is True
        assert watched["pid"] == process.pid
        assert watched["armed_at"]
        assert watched["process_alive"] is True
    finally:
        process.terminate()
        process.wait(timeout=5)

    stale = mcp._crew(project, view="live")["watcher"]
    assert stale == _read_cli(project)
    assert stale["status"] == "unwatched"
    assert stale["seat_held"] is False
    assert stale["watcher_live"] is False
    assert stale["pid"] == process.pid
    assert stale["armed_at"] == watched["armed_at"]
    assert stale["process_alive"] is False
    assert stale["arming_line"] == unwatched["arming_line"]
