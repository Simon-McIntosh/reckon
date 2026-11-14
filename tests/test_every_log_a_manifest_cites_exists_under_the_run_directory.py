"""The worker's manifest check refuses logs that will not survive its scratch."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import runs

COMMAND = "python -m pytest tests/test_example.py -q"
LOG_FIELDS = ("test_logs", "baseline_suite", "after_suite", "negative_control_log")


@pytest.fixture()
def run(tmp_path, monkeypatch, isolated_reckon_home):
    run_directory = tmp_path / "durable" / "run"
    run_directory.mkdir(parents=True)
    manifest_path = run_directory / "manifest.md"
    record = {
        "run_id": "log-location-check",
        "project": "sample",
        "repo": str(tmp_path / "repo"),
        "worktree": str(tmp_path / "repo"),
        "role": "investigate",
        "node": {"id": "log-location-check", "write_paths": []},
        "manifest_path": str(manifest_path),
    }
    monkeypatch.setattr(runs, "read_pointer", lambda run_id: record)
    return manifest_path


def _log(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# revision=abc1234 tree={path.parent} command: {COMMAND}\n1 passed\nEXIT=0\n",
        encoding="utf-8",
    )
    return path


def _manifest(path, fields):
    payload = {"status": "blocked", "messages_received": "none"}
    for field, log in fields.items():
        if field == "test_logs":
            payload[field] = [str(log)]
        elif field in ("baseline_suite", "after_suite"):
            payload[field] = {
                "revision": "abc1234",
                "command": COMMAND,
                "exit_status": 0,
                "log_path": str(log),
                "completed": True,
                "failure_ids": [],
            }
        else:
            payload[field] = str(log)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _check():
    result = CliRunner().invoke(
        main, ["crew", "check", "--manifest", "--run", "log-location-check"]
    )
    assert result.exception is None or result.exit_code == 1, result.output
    return result, json.loads(result.output)


@pytest.mark.parametrize("field", LOG_FIELDS)
@pytest.mark.parametrize("location", ["scratch", "missing", "directory", "symlink"])
def test_check_refuses_each_unavailable_or_external_log(run, tmp_path, field, location):
    scratch = _log(tmp_path / "node-local-scratch" / "gate.log")
    if location == "scratch":
        cited = scratch
    elif location == "missing":
        cited = run.parent / "missing.log"
    elif location == "directory":
        cited = run.parent / "logs"
        cited.mkdir()
    else:
        cited = run.parent / "linked.log"
        cited.symlink_to(scratch)
    _manifest(run, {field: cited})

    result, receipt = _check()

    assert result.exit_code == 1, receipt
    assert receipt["ok"] is False
    assert any(
        field in finding and str(cited) in finding for finding in receipt["findings"]
    )
    print(f"REFUSED {field} {location}: {receipt['findings']}")


@pytest.mark.parametrize("relative", [False, True])
def test_check_accepts_all_logs_under_its_run_directory(run, relative):
    fields = {}
    for field in LOG_FIELDS:
        path = _log(run.parent / "logs" / f"{field}.log")
        fields[field] = path.relative_to(run.parent) if relative else path
    _manifest(run, fields)

    result, receipt = _check()

    assert result.exit_code == 0, receipt
    assert receipt == {
        "ok": True,
        "run_id": "log-location-check",
        "node": "log-location-check",
        "manifest_path": str(run),
        "findings": [],
    }
    print(f"ACCEPTED all {len(LOG_FIELDS)} log fields; relative={relative}")


def test_check_names_every_missing_path_and_relative_escape(run, tmp_path):
    fields = {field: run.parent / f"absent-{field}.log" for field in LOG_FIELDS}
    _manifest(run, fields)
    result, receipt = _check()
    assert result.exit_code == 1, receipt
    for field, path in fields.items():
        assert any(
            field in finding and str(path) in finding for finding in receipt["findings"]
        )

    scratch = _log(tmp_path / "node-local-scratch" / "gate.log")
    relative = Path("../../node-local-scratch/gate.log")
    assert (run.parent / relative).resolve() == scratch
    _manifest(run, {"test_logs": relative})
    result, receipt = _check()
    assert result.exit_code == 1, receipt
    assert any(str(relative) in finding for finding in receipt["findings"])
