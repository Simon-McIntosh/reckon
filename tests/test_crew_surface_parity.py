"""Parity between the CLI live view and the MCP live view.

Both surfaces project one live run to the same classification: the CLI reaches
it through ``crew view live`` and the MCP tool through ``crew(..., view="live")``.
The rows are compared over :data:`CLASSIFICATION_FIELDS`, written down here
rather than derived, so a surface that drops one of them fails.
"""

from __future__ import annotations

import json
import os
from importlib import import_module

from click.testing import CliRunner

from reckon import cli, crew, mcp

recovery_module = import_module("reckon.crew.recovery")
routed_clock_modules = (
    import_module("reckon.crew.recovery_stream"),
    import_module("reckon.crew.recovery_liveness"),
    import_module("reckon.crew.recovery_classification"),
    import_module("reckon.crew.recovery_watch"),
)
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


def test_the_cli_live_view_matches_the_mcp_classification_surface(
    tmp_path, monkeypatch
) -> None:
    log_path = tmp_path / "stream.jsonl"
    log_path.write_text("")
    os.utime(log_path, (900, 900))
    # A manifest on disk, because a field the run has no value for is left off
    # the row: this case compares every field of the shared vocabulary, so the
    # fixture gives the classification a status to carry rather than reading
    # the omission as one surface disagreeing with the other.
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text("node: worker\nstatus: in-progress\n", encoding="utf-8")
    pointer = {
        "run_id": "run-working",
        "project": "sample",
        "backend": "codex",
        "launch": "cli",
        "phase": "working",
        "process_alive": True,
        "created_at": "1970-01-01T00:11:40+00:00",
        "log_path": str(log_path),
        "manifest_path": str(manifest_path),
        "worktree": str(tmp_path / "worktree"),
        "node": {
            "id": "worker",
            "plan": "delivery",
            "time_budget": "200s",
        },
    }
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: [pointer])
    for module in routed_clock_modules:
        monkeypatch.setattr(module, "_utc_seconds", lambda: 1000.0)
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
        cli.main, ["crew", "view", "live", "--project", "sample"]
    )
    assert command.exit_code == 0, command.output

    command_row = json.loads(command.output)["runs"][0]
    live_row = mcp._crew("sample", view="live")["runs"][0]

    # The instrument check: every named field must be on both rows before
    # anything is compared, so two rows that carried none of them could not
    # agree by accident.
    for field in CLASSIFICATION_FIELDS:
        assert field in command_row, field
        assert field in live_row, field
    assert {key: command_row[key] for key in CLASSIFICATION_FIELDS} == {
        key: live_row[key] for key in CLASSIFICATION_FIELDS
    }
    # Both transports reach one projection, so the two rows are the same row:
    # a CLI plumbing defect that dropped an option would show here as a
    # differing key set rather than as a missing comparison.
    assert set(command_row) == set(live_row)
    assert command_row["classification"] == "running"
    assert command_row["process_alive"] is True
    assert command_row["backend"] == "codex"


def test_a_live_row_names_the_session_that_owns_it(tmp_path, monkeypatch) -> None:
    """A recovering orchestrator must not infer ownership from a node name.

    The dispatching session is stored on the pointer, so the live view reports
    it rather than leaving the caller to read ownership out of a node id.
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
        cli.main,
        ["crew", "view", "live", "--project", "sample", "--session", "keeper"],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["runs"]

    assert [row["run_id"] for row in rows] == ["run-mine", "run-theirs"]
    assert [row["session"] for row in rows] == ["keeper", "peer"]
    assert [row["mine"] for row in rows] == [True, False]


def test_ownership_is_unclaimed_when_no_session_is_named(tmp_path, monkeypatch) -> None:
    """Without a --session there is nobody to compare against, so no row is marked.

    The view omits the marker rather than reporting False, which would assert
    every run belongs to someone else — a claim it has no evidence for, since
    ownership is a comparison against a session nobody named.
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

    listed = CliRunner().invoke(
        cli.main, ["crew", "view", "live", "--project", "sample"]
    )
    assert listed.exit_code == 0, listed.output
    row = json.loads(listed.output)["runs"][0]
    assert row["session"] == "peer"
    assert "mine" not in row


def test_mine_without_a_session_is_refused(tmp_path, monkeypatch) -> None:
    """`--mine` with nothing to compare against would silently list everything."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(crew, "list_live", lambda project=None: [])

    listed = CliRunner().invoke(
        cli.main, ["crew", "view", "live", "--project", "sample", "--mine"]
    )
    assert listed.exit_code != 0
    assert "needs the session id" in listed.output


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
        [
            "crew",
            "view",
            "live",
            "--project",
            "sample",
            "--session",
            "keeper",
            "--mine",
        ],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["runs"]
    assert [row["run_id"] for row in rows] == ["run-mine"]
