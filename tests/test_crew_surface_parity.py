"""Parity between script-reachable and MCP live-run projections."""

from __future__ import annotations

import json
import os
from importlib import import_module

from click.testing import CliRunner

from reckon import cli, crew, mcp


recovery_module = import_module("reckon.crew.recovery")
runs_module = import_module("reckon.crew.runs")


CLASSIFICATION_FIELDS = {
    "classification",
    "process_alive",
    "elapsed_seconds",
    "log_age_seconds",
    "budget_seconds",
    "budget_overrun",
    "manifest_status",
    "next_action",
}


def test_crew_list_matches_live_view_classification(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "stream.jsonl"
    log_path.write_text("")
    os.utime(log_path, (900, 900))
    pointer = {
        "run_id": "run-working",
        "project": "sample",
        "backend": "codex",
        "launch": "cli",
        "phase": "working",
        "process_alive": True,
        "created_at": "1970-01-01T00:11:40+00:00",
        "log_path": str(log_path),
        "manifest_path": str(tmp_path / "manifest.md"),
        "worktree": str(tmp_path / "worktree"),
        "node": {
            "id": "worker",
            "plan": "delivery",
            "time_budget": "200s",
        },
    }
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: [pointer])
    monkeypatch.setattr(recovery_module, "_utc_seconds", lambda: 1000.0)
    monkeypatch.setattr(
        runs_module,
        "project_watch_visibility",
        lambda project: {"project": project, "watcher_live": False},
    )

    command = CliRunner().invoke(
        cli.main, ["crew", "list", "--project", "sample"]
    )
    assert command.exit_code == 0, command.output

    command_row = json.loads(command.output)["runs"][0]
    live_row = mcp._crew("sample", view="live")["runs"][0]

    assert CLASSIFICATION_FIELDS <= command_row.keys()
    assert {key: command_row[key] for key in CLASSIFICATION_FIELDS} == {
        key: live_row[key] for key in CLASSIFICATION_FIELDS
    }
    assert command_row["backend"] == "codex"
    assert command_row["launch"] == "cli"
