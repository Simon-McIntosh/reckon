"""A crew project is recognised by its own configuration, not by its ledger.

The run ledger lives in the crew store outside every repository, so a checkout
carries no ``crew.json``. The two pre-tool-use guards fail open by design, so a
detection keyed on that file would turn both silently off in every tree once
the untracking lands. Each case here builds a project whose state directory
holds only the project configuration that stays tracked, removes both ledger
files, and requires the refusal anyway.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from reckon import cli_entry
from reckon.crew import routing
from reckon.crew.dispatching import comparisons
from reckon.hooks import native_agent_guard, worker_message_guard


def _project_repo(tmp_path: Path, *, project: str = "proj") -> Path:
    """A repository whose project state survives with no tracked ledger.

    The directory carries a tracked project configuration file and nothing
    else; the two ledger files are created and then removed, which is the state
    every checkout reaches once the ledger leaves git.
    """
    repo = tmp_path / "repo"
    state = repo / "docs" / "state" / project
    state.mkdir(parents=True)
    (repo / ".git").mkdir()
    (state / "index.json").write_text("{}\n")
    for ledger_name in ("crew.json", "flight.yaml"):
        ledger = state / ledger_name
        ledger.write_text("")
        ledger.unlink()
    return repo


def _plain_repo(tmp_path: Path) -> Path:
    """A repository with no crew project state of any kind."""
    repo = tmp_path / "plain-repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _agent_payload(cwd: Path) -> dict:
    return {"tool_name": "Agent", "cwd": str(cwd), "tool_input": {}}


def _send_payload(cwd: Path, *, to: str) -> dict:
    return {
        "tool_name": "SendMessage",
        "cwd": str(cwd),
        "tool_input": {"to": to, "message": "hello", "summary": "steer"},
    }


def _write_live_pointer(home: Path) -> None:
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "r-live.json").write_text(
        json.dumps(
            {"run_id": "r-live", "launcher_host": socket.gethostname(), "pid": 1}
        )
    )


# ── Both guards still refuse once the ledger files are gone ────────────────


def test_native_agent_guard_refuses_a_project_with_no_tracked_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _project_repo(tmp_path)
    monkeypatch.delenv(native_agent_guard.OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(native_agent_guard.RUN_ID_ENV, raising=False)

    allowed, message = native_agent_guard.decide(_agent_payload(repo))

    assert allowed is False
    assert "refused" in message


def test_worker_message_guard_refuses_a_send_with_no_tracked_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _project_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(worker_message_guard.OVERRIDE_ENV, raising=False)
    _write_live_pointer(home)

    allowed, message = worker_message_guard.decide(_send_payload(repo, to="r-live"))

    assert allowed is False
    assert "r-live" in message


def test_a_repository_with_no_project_state_is_left_alone(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _plain_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(native_agent_guard.OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(worker_message_guard.OVERRIDE_ENV, raising=False)
    _write_live_pointer(home)

    assert native_agent_guard.crew_managed_projects(repo) == []
    assert native_agent_guard.decide(_agent_payload(repo)) == (True, None)
    assert worker_message_guard.decide(_send_payload(repo, to="r-live")) == (
        True,
        None,
    )


# ── The CLI's sync-time recognition agrees with the guards ─────────────────


def test_cli_entry_recognises_a_project_with_no_tracked_ledger(
    tmp_path: Path,
) -> None:
    repo = _project_repo(tmp_path)

    assert cli_entry._crew_state_exists(repo / "docs", "proj") is True
    assert cli_entry._crew_state_exists(repo / "docs", "absent") is False


# ── Shadow dispatch finds the project without a tracked ledger ─────────────


def test_shadow_source_finds_a_project_with_no_tracked_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _project_repo(tmp_path)
    record = {
        "run_id": "r-primary",
        "base_sha": "0" * 40,
        "agent": {"backend": "local"},
        "node_definition": {
            "id": "some-node",
            "goal": "prove the shadow finds its project",
            "plan": "some-plan",
            "done_when": "the project resolves",
            "write_paths": ["some_path.py"],
        },
    }
    monkeypatch.setattr(routing, "repository_projects", lambda path: ())
    monkeypatch.setattr(comparisons.ledger, "runs", lambda project, root=None: [record])

    source = comparisons.shadow_source("r-primary", repo=repo)

    assert source["project"] == "proj"
    assert source["node"].id == "some-node"
