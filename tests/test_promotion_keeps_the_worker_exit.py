"""Promotion copies the run directory's exit record onto the committed row.

A CLI dispatch's supervisor writes ``exit.json`` beside the worker and the
coordinator reaches promotion after the worker has ended. Promotion releases the
run's live pointer and its worktree, but not the run directory: that survives,
``exit.json`` with it, until ``crew gc`` prunes it on a retention window, so the
ledger row is the durable copy once gc runs. The worker's exit must ride that row
under ``worker_exit``. A missing record leaves the key off the row entirely,
because a present empty key reads as a supervisor that ran and recorded nothing.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "proj"
PLAN = "plan-a"

REVIEW_WAIVER = "fixture: the run's exit record is the subject, not repository work"


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


def _write_passing_pointer(repository: Path, run_id: str, manifest: Path) -> None:
    """A complete passed pointer a promotion can land from its gate evidence."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: none\n", encoding="utf-8"
    )
    _write_json(
        pointer_path(run_id),
        {
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
        },
    )


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


def test_a_promoted_run_carries_its_worker_exit(
    repository: Path, tmp_path: Path
) -> None:
    """The run directory's exit record survives promotion under worker_exit.

    The exit object is copied verbatim, and the row's gate ``exit_status`` is the
    value the coordinator passed to the gate check — the worker's exit is a
    separate fact and never overwrites the gate command's status.
    """
    run_id = "r-20260924T101500000000-node-a"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_passing_pointer(repository, run_id, manifest)
    exit_record = {"signal_name": "SIGKILL", "ended_during": "working"}
    exit_path = run_dir(run_id) / "exit.json"
    exit_path.parent.mkdir(parents=True, exist_ok=True)
    exit_path.write_text(json.dumps(exit_record), encoding="utf-8")

    crew.complete(
        run_id,
        gate="passed",
        outcome="the node delivered its exit record",
        root=repository,
        gate_check=_gate_check(tmp_path / "gate.log"),
        review_waiver=REVIEW_WAIVER,
    )

    row = _committed_row(repository, run_id)
    assert row.get("worker_exit") == exit_record
    assert row.get("gate_check", {}).get("exit_status") == 0


def test_a_promoted_run_without_an_exit_record_carries_no_key(
    repository: Path, tmp_path: Path
) -> None:
    """A missing exit record leaves no key, rather than an empty one.

    A present-but-empty ``worker_exit`` would read as a supervisor that ran and
    recorded nothing, so the key must be absent.
    """
    run_id = "r-20260924T101600000000-node-a"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_passing_pointer(repository, run_id, manifest)

    crew.complete(
        run_id,
        gate="passed",
        outcome="the node delivered its outcome",
        root=repository,
        gate_check=_gate_check(tmp_path / "gate.log"),
        review_waiver=REVIEW_WAIVER,
    )

    row = _committed_row(repository, run_id)
    assert "worker_exit" not in row
