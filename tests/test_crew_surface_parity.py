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
        lambda project, session=None: {
            "project": project,
            "watcher_live": False,
            "session_attached": None if session is None else False,
        },
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


def test_a_listed_run_names_the_session_that_owns_it(tmp_path, monkeypatch) -> None:
    """A recovering orchestrator must not infer ownership from a node name.

    The dispatching session is stored on the pointer, so the listing reports it
    rather than leaving the caller to read ownership out of a node id.
    """
    pointers = [
        {
            "run_id": "run-mine",
            "project": "sample",
            "session": "keeper",
            "member": "m-1",
            "agent": {"model": "gpt-5.6-sol", "effort": "medium"},
            "phase": "working",
            "node": {"id": "mine", "plan": "delivery"},
        },
        {
            "run_id": "run-theirs",
            "project": "sample",
            "session": "peer",
            "phase": "working",
            "node": {"id": "theirs", "plan": "delivery"},
        },
    ]
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: pointers)
    monkeypatch.setattr(
        runs_module,
        "project_watch_visibility",
        lambda project, session=None: {"project": project, "session": session},
    )

    listed = CliRunner().invoke(
        cli.main, ["crew", "list", "--project", "sample", "--session", "keeper"]
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["runs"]

    assert [row["session"] for row in rows] == ["keeper", "peer"]
    assert [row["mine"] for row in rows] == [True, False]
    assert rows[0]["member"] == "m-1"
    assert rows[0]["agent"] == "gpt-5.6-sol/medium"
    assert rows[1]["member"] is None
    assert rows[1]["agent"] is None


def test_ownership_is_unclaimed_when_no_session_is_named(tmp_path, monkeypatch) -> None:
    """Without a --session there is nobody to compare against, so `mine` is null.

    Reporting False would assert every run belongs to someone else, which is a
    claim the command has no evidence for.
    """
    pointer = {
        "run_id": "run-any",
        "project": "sample",
        "session": "peer",
        "phase": "working",
        "node": {"id": "any", "plan": "delivery"},
    }
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: [pointer])
    monkeypatch.setattr(
        runs_module,
        "project_watch_visibility",
        lambda project, session=None: {"project": project, "session": session},
    )

    listed = CliRunner().invoke(cli.main, ["crew", "list", "--project", "sample"])
    assert listed.exit_code == 0, listed.output
    row = json.loads(listed.output)["runs"][0]
    assert row["session"] == "peer"
    assert row["mine"] is None


def test_mine_without_a_session_is_refused(tmp_path, monkeypatch) -> None:
    """`--mine` with nothing to compare against would silently list everything."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: [])

    listed = CliRunner().invoke(cli.main, ["crew", "list", "--mine"])
    assert listed.exit_code != 0
    assert "--mine needs --session" in listed.output


def test_mine_keeps_only_the_named_sessions_runs(tmp_path, monkeypatch) -> None:
    """The filter drops peers rather than reordering them."""
    pointers = [
        {
            "run_id": "run-peer",
            "project": "sample",
            "session": "peer",
            "phase": "working",
            "node": {"id": "theirs", "plan": "delivery"},
        },
        {
            "run_id": "run-mine",
            "project": "sample",
            "session": "keeper",
            "phase": "working",
            "node": {"id": "mine", "plan": "delivery"},
        },
        {
            "run_id": "run-unowned",
            "project": "sample",
            "phase": "working",
            "node": {"id": "unowned", "plan": "delivery"},
        },
    ]
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: pointers)
    monkeypatch.setattr(
        runs_module,
        "project_watch_visibility",
        lambda project, session=None: {"project": project, "session": session},
    )

    listed = CliRunner().invoke(
        cli.main,
        ["crew", "list", "--project", "sample", "--session", "keeper", "--mine"],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["runs"]
    assert [row["run_id"] for row in rows] == ["run-mine"]
