"""A closure drain is owned by the coordinator session asking for it.

The project-wide form remains available for compatibility. Supplying a session
aims the closure fence at that session's runs while keeping peer rows visible,
so a coordinator neither blocks on nor loses sight of another coordinator's
work.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_dispatch_commands, mcp

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {
        "time_budget": "25m",
        "needs_help_after_failures": 2,
        "unreconciled_run_grace": "5m",
    },
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep every live pointer and manifest inside the temporary tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, home):
    """Create a committed plan and the fleet script dispatch validates."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (root / "docs" / "plans" / "session-closure.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="session-closure">
</head><body><h2 id="dispatch">Dispatch</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/session-closure.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _terminal_pointer(
    home: Path,
    run_id: str,
    session: str,
    *,
    age_seconds: int = 601,
) -> dict:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: delivered-node\nstatus: complete\ncommits: HEAD\n")
    terminal = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    os.utime(manifest, (terminal.timestamp(), terminal.timestamp()))
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "session": session,
        "repo": "/temporary/repository",
        "node": {
            "id": "delivered-node",
            "plan": "session-closure",
            "time_budget": "20m",
        },
        "phase": "complete",
        "created_at": (terminal - timedelta(days=2)).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "absent-stream.jsonl"),
    }
    crew._write_json(crew.pointer_path(run_id), pointer)
    return pointer


def _own_and_peer(home: Path) -> None:
    _terminal_pointer(home, "r-own", "asking-session")
    _terminal_pointer(home, "r-peer", "peer-session")


def test_session_drain_counts_own_run_and_reports_peer_separately(home) -> None:
    _own_and_peer(home)

    project_wide = crew.drain("proj")
    session_drain = crew.drain("proj", session="asking-session")

    assert project_wide["unreconciled_runs"] == 2
    assert "session" not in project_wide
    assert "peer_runs" not in project_wide
    assert session_drain["unreconciled_runs"] == 1
    assert session_drain["live_pointers"] == 1
    assert session_drain["peer_pointers"] == 1
    assert [row["run_id"] for row in session_drain["runs"]] == ["r-own"]
    assert [row["run_id"] for row in session_drain["peer_runs"]] == ["r-peer"]
    assert session_drain["peer_runs"][0]["session"] == "peer-session"


def test_command_and_tool_thread_the_asking_session_into_the_drain(home) -> None:
    _own_and_peer(home)

    command = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "drain",
            "--project",
            "proj",
            "--session",
            "asking-session",
        ],
    )
    command_payload = json.loads(command.output)
    tool_payload = mcp._crew("proj", view="drain", session="asking-session")

    assert command.exit_code == 0, command.output
    for payload in (command_payload, tool_payload):
        assert payload["unreconciled_runs"] == 1
        assert payload["session"] == "asking-session"
        assert payload["peer_pointers"] == 1
        assert [row["run_id"] for row in payload["runs"]] == ["r-own"]
        assert [row["run_id"] for row in payload["peer_runs"]] == ["r-peer"]
        assert payload["peer_runs"][0]["session"] == "peer-session"


def test_session_drain_cannot_record_a_disposition_on_a_peer(home) -> None:
    _own_and_peer(home)

    command = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "drain",
            "--project",
            "proj",
            "--session",
            "asking-session",
            "--leave",
            "r-peer=handed-off",
        ],
    )

    assert command.exit_code == 1
    assert "belongs to session 'peer-session', not 'asking-session'" in command.output
    assert "closure_disposition" not in crew.read_pointer("r-peer")


def test_dispatch_records_its_own_backlog_and_names_no_peer(
    home, repo, monkeypatch
) -> None:
    """A dispatch is made over its own session's overdue runs, never a peer's.

    The fence used to refuse the dispatch outright and name the peer rows it
    had observed. It records the overdue runs on the run it writes instead of
    refusing, and it reads only the asking session's pointers, so a peer's
    finished run is neither counted, copied nor named there. The drain stays
    the surface that shows a peer, so this fixture is read back from it as the
    control: the peer pointer is live and only the fence's scope keeps it out.
    """
    _own_and_peer(home)
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *args, **kwargs: CONFIG)
    # The spawn seam is where a real worker would start; the dispatch's record
    # is what this case studies, so the launch is stubbed to a pid no process
    # holds.
    monkeypatch.setattr(crew, "_spawn", lambda *args, **kwargs: 4242)

    command = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "session-closure",
            "--section",
            "dispatch",
            "--spec-level",
            "guided",
            "--node",
            "next-node",
            "--goal",
            "record the launch matrix for one backend",
            "--done-when",
            "the focused closure tests report zero failures",
            "--write-path",
            "reckon/_backends.py",
            "--manifest",
            str(home / "next-node-manifest.md"),
            "--session",
            "asking-session",
            "--backend",
            "alpha",
            "--repo",
            str(repo),
            # Delivery to a session's follower is a separate gate with its own
            # refusal; it is waived here so the fence under study is the one
            # this case reaches.
            "--no-watch",
        ],
    )
    # The load notice goes to stderr, so stdout stays the document a caller
    # reads: the run this dispatch wrote.
    payload = json.loads(command.stdout)

    assert command.exit_code == 0, payload
    backlog = payload["unreconciled_override"]
    assert [row["run_id"] for row in backlog["waived_runs"]] == ["r-own"]
    assert backlog["requested"] is False
    assert "r-peer" not in command.stdout
    # The peer's pointer is live and the drain still counts it, so the absence
    # above is the fence's session scope rather than an empty ledger.
    assert crew.drain("proj", session="asking-session")["peer_pointers"] == 1
