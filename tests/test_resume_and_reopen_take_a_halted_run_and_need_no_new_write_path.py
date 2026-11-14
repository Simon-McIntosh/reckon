"""Reopening keeps a stopped run's scope unless the caller grants more paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main
from reckon.crew import resumption, runs
from tests.test_a_reopen_launches_no_worker import CONFIG, _absent_pid, _completed_run


@pytest.fixture()
def stopped_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        "reckon.crew_run_commands._resolved_flight", lambda *args: CONFIG
    )
    run_id = "r-reopen-with-existing-scope"
    record = _completed_run(tmp_path, run_id)
    return run_id, record


@pytest.mark.parametrize("phase", ["stopped", "complete"])
def test_reopen_without_a_new_path_then_resume(stopped_run, monkeypatch, phase):
    """Reopen starts nothing; the explicit resume records the next attempt."""
    run_id, record = stopped_run
    prior_attempt = record["attempt"]
    record["phase"] = phase
    manifest = Path(record["manifest_path"])
    status = "blocked" if phase == "stopped" else "complete"
    manifest.write_text(f"node: {run_id}\nstatus: {status}\n")
    runs._write_json(runs.pointer_path(run_id), record)
    launched = []

    def launch(plan, **kwargs):
        launched.append(plan)
        return _absent_pid()

    monkeypatch.setattr(crew, "_spawn", launch)
    runner = CliRunner()
    reopened = runner.invoke(main, ["crew", "resume", "--run", run_id, "--reopen"])

    assert reopened.exit_code == 0, reopened.output
    receipt = json.loads(reopened.output)
    assert receipt["reopened"] is True
    assert receipt["launched"] is False
    assert receipt["added_paths"] == []
    assert receipt["write_paths"] == record["node"]["write_paths"]
    assert launched == []
    assert (
        json.loads((runs.run_dir(run_id) / resumption.REOPEN_RECORD_NAME).read_text())[
            "granted_paths"
        ]
        == []
    )

    resumed = runner.invoke(
        main, ["crew", "resume", "--run", run_id, "--advice", "continue the repair"]
    )

    assert resumed.exit_code == 0, resumed.output
    assert len(launched) == 1
    assert "continue the repair" in launched[0].stdin_text
    after = runs.read_pointer(run_id)
    assert after["phase"] == "working"
    assert after["attempt"] > prior_attempt
    assert after["attempt_kind"] == "resume"
    assert after["node"]["write_paths"] == record["node"]["write_paths"]


def test_reopen_a_halted_run_can_still_grant_a_path(stopped_run):
    run_id, record = stopped_run
    record["phase"] = "stopped"
    Path(record["manifest_path"]).write_text(f"node: {run_id}\nstatus: blocked\n")
    runs._write_json(runs.pointer_path(run_id), record)

    result = CliRunner().invoke(
        main,
        ["crew", "resume", "--run", run_id, "--reopen", "--write-path", "repair.py"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["added_paths"] == ["repair.py"]
    assert runs.read_pointer(run_id)["node"]["write_paths"] == [
        *record["node"]["write_paths"],
        "repair.py",
    ]


def test_reopen_without_a_new_path_refuses_a_live_worker(stopped_run):
    run_id, record = stopped_run
    record["pid"] = os.getpid()
    record["pid_start_time"] = resumption.process_start_time(os.getpid())
    runs._write_json(runs.pointer_path(run_id), record)
    before = Path(record["manifest_path"]).read_text()

    result = CliRunner().invoke(main, ["crew", "resume", "--run", run_id, "--reopen"])

    assert result.exit_code != 0
    assert "still has a live process" in result.output
    assert Path(record["manifest_path"]).read_text() == before
    assert not (runs.run_dir(run_id) / resumption.REOPEN_RECORD_NAME).exists()
