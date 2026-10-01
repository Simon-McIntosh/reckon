"""Promotion copies the dispatching crew session onto the committed row.

A promoted row carries ``node_definition.coordinator.session_id`` but no
top-level notion of the crew session that dispatched the run, and its top-level
``session_id`` is the worker's own harness session. So a reader asking which
coordinator session a promoted run belonged to gets nothing, or the wrong
session. Promotion copies the live pointer's ``session`` onto the row as
``session``, leaving ``session_id`` as the worker's harness session. A run whose
pointer names no session records no ``session`` key rather than an empty one.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

REVIEW_WAIVER = "fixture: the session attribution is the subject, not repository work"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "comments": {},
        },
    )
    # Promotion commits the two tracked stores it writes, so a fixture that
    # promotes must be a git worktree with a committed head to land into.
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _stamp(seconds_ago: int) -> str:
    moment = datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_pointer(
    repository: Path,
    run_id: str,
    manifest: Path,
    *,
    session: str | None,
    session_id: str | None,
) -> None:
    """A complete passed pointer a promotion can land from its gate evidence."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: none\n", encoding="utf-8"
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "cli",
        "role": "implement",
        "member": "worker-a",
        "backend": "claude",
        "argv": ["claude", "-p"],
        "created_at": _stamp(120),
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§2",
            "time_budget": "35m",
            "write_paths": [],
        },
    }
    if session is not None:
        record["session"] = session
    if session_id is not None:
        record["session_id"] = session_id
    _write_json(pointer_path(run_id), record)


def _committed_row(repository: Path, run_id: str) -> dict:
    """The run's row as it stands in the committed ledger store."""
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == run_id),
        None,
    )
    assert row is not None, "the promotion did not commit a ledger row"
    return dict(row)


def _gate_check(log: Path) -> dict:
    """A passing gate check carrying the command, its status and its log path."""
    log.write_text("the check ran\nEXIT=0\n", encoding="utf-8")
    return {
        "command": "true",
        "exit_status": 0,
        "log_path": str(log),
        "log_digest": "",
    }


def test_a_promoted_row_names_its_crew_session(
    repository: Path, tmp_path: Path
) -> None:
    """The row's ``session`` is the pointer's crew session, not the harness one.

    The pointer names both a crew session (``session``) and a worker harness
    session (``session_id``); the committed row must carry each under its own
    key, so the crew session is legible at the top level without being confused
    with the worker's.
    """
    run_id = "r-20260924T101700000000-node-a"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_pointer(
        repository,
        run_id,
        manifest,
        session="s-test",
        session_id="harness-abc",
    )

    crew.complete(
        run_id,
        gate="passed",
        outcome="the node attributed its crew session",
        root=repository,
        gate_check=_gate_check(tmp_path / "gate.log"),
        review_waiver=REVIEW_WAIVER,
    )

    row = _committed_row(repository, run_id)
    assert row.get("session") == "s-test"
    assert row.get("session_id") == "harness-abc"


def test_a_promoted_row_without_a_session_carries_no_key(
    repository: Path, tmp_path: Path
) -> None:
    """A pointer naming no crew session leaves no ``session`` key.

    A present-but-empty ``session`` would read as a run attributed to an empty
    session, so the key must be absent when the pointer never named one.
    """
    run_id = "r-20260924T101800000000-node-a"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_pointer(
        repository,
        run_id,
        manifest,
        session=None,
        session_id="harness-def",
    )

    crew.complete(
        run_id,
        gate="passed",
        outcome="the node carried no crew session",
        root=repository,
        gate_check=_gate_check(tmp_path / "gate.log"),
        review_waiver=REVIEW_WAIVER,
    )

    row = _committed_row(repository, run_id)
    assert "session" not in row
