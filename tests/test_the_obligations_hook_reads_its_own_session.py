"""The obligations hook answers for the harness session that invoked it.

A session host can hold a follower for another session, and that follower's
arming process still descends from this hook's harness process. Session
resolution therefore reads the harness session's own registration first, and
falls back to descent only when the harness session names none of its own.
Each drive uses a temporary crew home holding registrations for two sessions,
both armed by the caller's process, with the foreign one sorting first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import _session_from_followers
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "session-ownership-fixture"
# The two sessions the fixture registers. Both registrations name the caller's
# process as the one that armed them, so descent alone cannot tell them apart;
# the foreign name sorts first so a descent-first scan adopts it.
OWN_SESSION = "harness-session-a"
FOREIGN_SESSION = "aaa-session-b"
FOREIGN_RUN_ID = "run-foreign-blocked"
FOREIGN_NODE_ID = "foreign-blocked-node"


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed session ownership fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _register(session: str) -> None:
    """Hold a delivery registration for one session, armed by this process."""
    with runs.follower_claim(PROJECT, session):
        pass


def _armed_parent_pid() -> int:
    """The process both registrations name as having armed them."""
    for row in runs.list_followers(PROJECT):
        record = row.get("follower")
        if isinstance(record, dict) and record.get("parent_pid"):
            return int(record["parent_pid"])
    raise AssertionError("the fixture registered no follower")


def _blocked_run(repository: Path, tmp_path: Path, *, session: str) -> None:
    """Record one live run, owned by ``session``, that owes a blocked duty."""
    manifest = tmp_path / "manifests" / f"{FOREIGN_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {FOREIGN_NODE_ID}\nstatus: blocked\n", encoding="utf-8")
    runs._write_json(
        runs.pointer_path(FOREIGN_RUN_ID),
        {
            "run_id": FOREIGN_RUN_ID,
            "project": PROJECT,
            "session": session,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "process_alive": False,
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": FOREIGN_NODE_ID,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _publish_snapshot(session: str) -> None:
    """Publish one session's derived obligations as a fresh snapshot."""
    computed_at = datetime.now(tz=UTC)
    document = obligation_snapshot.document_for(
        obligations_view(PROJECT, session),
        computed_at=computed_at,
        stream_offset=0,
        producer={
            "pid": os.getpid(),
            "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
            "started_at": computed_at.isoformat(),
            "code_stamp": obligation_snapshot.source_code_stamp(),
        },
    )
    obligation_snapshot.write_snapshot(PROJECT, session, document)


def _hook(mode: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    _publish_snapshot(OWN_SESSION)
    _publish_snapshot(FOREIGN_SESSION)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_armed_parent_pid())
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _stop_payload(directory: Path, session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "cwd": str(directory)}


def test_the_hooks_own_registration_wins_over_an_earlier_descending_one(
    repository: Path,
) -> None:
    """Harness session A resolves to A although B descends and sorts first."""
    _register(FOREIGN_SESSION)
    _register(OWN_SESSION)
    rows = [row["session"] for row in obligation_snapshot.read_followers(PROJECT)]
    assert rows == [FOREIGN_SESSION, OWN_SESSION], (
        "the foreign registration must sort first for this case to mean anything"
    )

    resolved = _session_from_followers(
        PROJECT,
        harness_session=OWN_SESSION,
        claude_pid=_armed_parent_pid(),
    )

    assert resolved == OWN_SESSION


def test_the_stop_hook_does_not_block_on_another_sessions_duty(
    repository: Path, tmp_path: Path
) -> None:
    """A stop in A is silent while a run only B owns holds its turn open."""
    _blocked_run(repository, tmp_path, session=FOREIGN_SESSION)
    _register(FOREIGN_SESSION)
    _register(OWN_SESSION)

    stopping = _hook("stop", _stop_payload(repository, OWN_SESSION))

    assert stopping.returncode == 0
    assert stopping.stderr == ""
    assert stopping.stdout == ""


def test_without_a_harness_session_descent_still_resolves_a_registration(
    repository: Path,
) -> None:
    """The descent fallback is kept for a harness that names no session."""
    _register(FOREIGN_SESSION)
    _register(OWN_SESSION)

    resolved = _session_from_followers(
        PROJECT,
        harness_session="",
        claude_pid=_armed_parent_pid(),
    )

    assert resolved == FOREIGN_SESSION
