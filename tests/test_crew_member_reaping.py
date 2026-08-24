"""Hermetic coverage for session-owned roster retirement."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reckon import crew, ledger


@pytest.fixture()
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    repo = tmp_path / "repo"
    config_home.mkdir()
    (repo / "docs" / "state" / "proj").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home, repo


def _register(repo: Path, member_id: str) -> None:
    ledger.register_member(
        "proj",
        member_id,
        harness="worker",
        session_id=f"worker-{member_id}",
        root=repo,
        now="2026-08-20T08:00:00Z",
    )


def test_reaping_preserves_live_named_and_durable_session_state(
    isolated_state: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_state
    idle_member = "session-idle"
    live_member = "session-live"
    named_member = "long-lived-reviewer"
    for member_id in (idle_member, live_member, named_member):
        _register(repo, member_id)

    data, version = ledger.load("proj", root=repo)
    data["runs"] = [
        {
            "run_id": "completed-idle-run",
            "member": idle_member,
            "dispatched_at": "2026-08-20T08:00:00Z",
            "session_id": "worker-session-recoverable",
        },
        {
            "run_id": "completed-live-run",
            "member": live_member,
            "dispatched_at": "2026-08-20T08:00:00Z",
            "session_id": "worker-session-live",
        },
        {
            "run_id": "completed-named-run",
            "member": named_member,
            "dispatched_at": "2026-08-20T08:00:00Z",
            "session_id": "worker-session-named",
        },
    ]
    ledger.write("proj", data, version, root=repo)

    pointer = {
        "run_id": "active-run",
        "project": "proj",
        "repo": str(repo.resolve()),
        "member": live_member,
        "phase": "running",
        "created_at": "2026-08-20T08:00:00Z",
    }
    pointer_path = config_home / "crew" / "live" / "active-run.json"
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(json.dumps(pointer, sort_keys=True) + "\n")
    pointer_snapshot = {
        path.name: path.read_bytes() for path in pointer_path.parent.glob("*.json")
    }

    result = crew.reap_idle_session_members(
        "proj",
        root=repo,
        idle_window="1h",
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
    )

    roster_ids = {member["id"] for member in ledger.members("proj", root=repo)}
    assert result == {"reaped": [idle_member], "idle_window": "1h"}
    assert idle_member not in roster_ids
    assert live_member in roster_ids
    assert named_member in roster_ids
    assert next(
        run["session_id"]
        for run in ledger.runs("proj", root=repo)
        if run["member"] == idle_member
    ) == "worker-session-recoverable"
    assert {
        path.name: path.read_bytes() for path in pointer_path.parent.glob("*.json")
    } == pointer_snapshot
