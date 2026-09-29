"""A recorded deferral withholds an obligation until its instant passes.

The deferral lives on the run's own live pointer, beside the closure
disposition, so these cases drive both ends: the CLI records it and reads it
back, and the obligations reader withholds the duty until ``--until`` and
returns it afterwards. Every crew directory is environment-resolved to a
temporary home, and the repository the fixture writes is left byte-for-byte identical
across the write.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "ack-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
RUN_ID = "run-ack"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed ack fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_pointer() -> None:
    """Record one live run that the obligations reader will owe work for."""
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "node": {
                "id": "node-ack",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _row() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "session": SESSION,
        "plan": "fixture-plan",
        "node": "node-ack",
        "classification": "blocked",
        "recovery_classification": "blocked",
        "terminal_age_seconds": 120,
        "next_action": "reckon crew resume --run run-ack --advice continue",
    }


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force one owed duty and a trivial closure drain over the live pointer."""
    _write_pointer()
    monkeypatch.setattr(
        obligations_module, "_classified_rows", lambda _project: [_row()]
    )
    monkeypatch.setattr(
        runs,
        "drain",
        lambda project, session=None: {
            "project": project,
            "session": session,
            "unreconciled_runs": 1,
        },
    )


def _until_instant(*, hours: int) -> str:
    return (OBSERVED_AT + timedelta(hours=hours)).isoformat()


def test_acknowledged_obligation_leaves_the_list_and_reads_back_its_reason(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    reason = "waiting on a peer to release the shared dispatch module"
    runs.record_run_acknowledgement(RUN_ID, reason, _until_instant(hours=1))

    result = obligations_module.obligations(PROJECT, SESSION)

    assert result["obligations"] == []
    assert [item["run_id"] for item in result["acknowledged"]] == [RUN_ID]
    deferred = result["acknowledged"][0]
    assert deferred["reason"] == reason
    assert deferred["until"] == _until_instant(hours=1)
    assert deferred["kind"] == "blocked"


def test_acknowledged_obligation_returns_once_its_until_passes(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    runs.record_run_acknowledgement(RUN_ID, "short deferral", _until_instant(hours=1))

    # The clock advances past the deferral's own instant, derived rather than
    # written down: the run returns with no second write to the store.
    monkeypatch.setattr(
        obligations_module, "_utc_now", lambda: OBSERVED_AT + timedelta(hours=2)
    )
    result = obligations_module.obligations(PROJECT, SESSION)

    assert [item["run_id"] for item in result["obligations"]] == [RUN_ID]
    assert result["acknowledged"] == []


def test_malformed_until_is_refused(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    with pytest.raises(runs.CrewError):
        runs.record_run_acknowledgement(RUN_ID, "reason", "next tuesday")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.main,
        ["crew", "ack", "--run", RUN_ID, "--reason", "reason", "--until", "soon"],
    )
    assert result.exit_code != 0
    assert "ISO-8601" in result.output


def test_ack_writes_only_under_the_temporary_config_home(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    before = {
        str(path): path.stat().st_mtime_ns
        for path in repository.rglob("*")
        if path.is_file()
    }

    runner = CliRunner()
    result = runner.invoke(
        cli_module.main,
        [
            "crew",
            "ack",
            "--run",
            RUN_ID,
            "--reason",
            "deferred",
            "--until",
            _until_instant(hours=3),
        ],
    )
    assert result.exit_code == 0, result.output

    pointer = runs.pointer_path(RUN_ID)
    assert pointer.is_file()
    assert str(pointer).startswith(str(repository.parent))
    record = json.loads(pointer.read_text())["acknowledgement"]
    assert record["reason"] == "deferred"
    assert record["until"] == _until_instant(hours=3)

    after = {
        str(path): path.stat().st_mtime_ns
        for path in repository.rglob("*")
        if path.is_file()
    }
    assert after == before
