"""Promotion retires a run's disposable identity and reaps its process group.

A dispatch that names no member carries a per-run identity and registers no
roster row. Acceptance is where any such identity that does exist is retired,
beside the worktree, the process group and the scratch the release already
reclaims. A run named explicitly keeps its roster row, and a blocked run keeps
both its identity and its process group, because a resume still needs them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion
from reckon.crew.runs import (
    _process_start_time,
    _write_json,
    pointer_path,
    process_alive,
)

PROJECT = "proj"
PLAN = "plan-a"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Promotion retirement",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _write_plan(repository / "docs" / "plans" / f"{PLAN}.html")
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "docs", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _worktree(repository: Path, root: Path, name: str) -> Path:
    worktree = root / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return worktree


def _pointer(
    repository: Path,
    root: Path,
    run_id: str,
    worktree: Path,
    member: str,
    *,
    status: str = "complete",
    pid: int | None = None,
    pid_start_time: str | None = None,
) -> None:
    manifest = root / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: node-a\nstatus: {status}\n", encoding="utf-8")
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree),
        "launch": "in-harness",
        "role": "implement",
        "member": member,
        "backend": "native",
        "created_at": "2026-09-25T07:00:00Z",
        "base_sha": _git(repository, "rev-parse", "HEAD"),
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§4",
            "time_budget": "35m",
            "write_paths": [],
        },
    }
    if pid is not None:
        record["pid"] = pid
        record["pid_start_time"] = pid_start_time
    _write_json(pointer_path(run_id), record)


def _stored_run(repository: Path, run_id: str) -> dict:
    return next(
        run for run in ledger.runs(PROJECT, root=repository) if run["run_id"] == run_id
    )


def _members(repository: Path) -> set[str]:
    return {str(entry["id"]) for entry in ledger.members(PROJECT, root=repository)}


def _stub_process() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


@pytest.fixture()
def negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("PROMOTION_NEGATIVE_IDENTITY"):

        def skipped(record):
            return {
                "identity_retired": False,
                "identity_withheld": "negative control skips identity deletion",
            }

        monkeypatch.setattr(promotion, "_retire_disposable_identity", skipped)


def test_promotion_retires_a_disposable_identity_and_keeps_a_named_one(
    repository: Path, tmp_path: Path, negative_control: None
) -> None:
    run_id = "r-20260925T072000000000-disposable"
    disposable = promotion._disposable_member_id(run_id)
    ledger.register_member(
        PROJECT, disposable, harness="native", root=repository, commit=True
    )
    ledger.register_member(
        PROJECT, "worker-a", harness="native", root=repository, commit=True
    )
    assert disposable in _members(repository)

    worktree = _worktree(repository, tmp_path, "disposable")
    _pointer(repository, tmp_path, run_id, worktree, disposable)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="a disposable identity is retired when its work is accepted",
        review_waiver="the synthesized retirement fixture has no code review",
        root=repository,
    )

    release = promoted["release"]
    assert release["identity_retired"] is True
    assert release["identity_member"] == disposable
    assert disposable not in _members(repository)
    # The explicitly named member is a durable identity its author asked for.
    assert "worker-a" in _members(repository)
    assert not pointer_path(run_id).exists()
    assert _stored_run(repository, run_id)["release"]["worktree_released"] is True


def test_promotion_signals_reaps_and_records_a_live_process_group(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260925T072100000000-live-process"
    disposable = promotion._disposable_member_id(run_id)
    worktree = _worktree(repository, tmp_path, "live-process")
    process = _stub_process()
    try:
        _pointer(
            repository,
            tmp_path,
            run_id,
            worktree,
            disposable,
            pid=process.pid,
            pid_start_time=_process_start_time(process.pid),
        )

        promoted = crew.complete(
            run_id,
            gate="passed",
            outcome="a live process group is signalled and reaped at acceptance",
            review_waiver="the synthesized retirement fixture has no code review",
            root=repository,
        )

        release = promoted["release"]
        assert release["process_signalled"] is True
        assert release["process_stopped_pid"] == process.pid

        deadline = time.monotonic() + 5
        while process_alive(process.pid) is True and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_alive(process.pid) is not True

        stored = _stored_run(repository, run_id)
        assert stored["release"]["process_signalled"] is True
        assert stored["release"]["process_stopped_pid"] == process.pid
    finally:
        if process_alive(process.pid) is True:
            process.kill()
        process.wait(timeout=5)


def test_a_blocked_run_keeps_its_identity_and_process_for_resume(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260925T072200000000-blocked"
    disposable = promotion._disposable_member_id(run_id)
    ledger.register_member(
        PROJECT, disposable, harness="native", root=repository, commit=True
    )
    worktree = _worktree(repository, tmp_path, "blocked")
    process = _stub_process()
    try:
        _pointer(
            repository,
            tmp_path,
            run_id,
            worktree,
            disposable,
            status="blocked",
            pid=process.pid,
            pid_start_time=_process_start_time(process.pid),
        )

        promoted = crew.complete(
            run_id,
            gate="failed",
            failure_classification="negative-result",
            outcome="a blocked run is not accepted and keeps its resume path",
            root=repository,
        )

        release = promoted["release"]
        assert release["identity_retired"] is False
        assert disposable in _members(repository)
        assert release["process_signalled"] is False
        assert process_alive(process.pid) is True
    finally:
        if process_alive(process.pid) is True:
            process.kill()
        process.wait(timeout=5)
