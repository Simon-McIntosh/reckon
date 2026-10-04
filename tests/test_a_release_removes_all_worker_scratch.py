"""Promotion accounts for every scratch directory written by one worker."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from reckon import _plan_html, crew
from reckon.crew.runs import _write_json, pointer_path

dispatch = importlib.import_module("reckon.crew.dispatch")


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_promotion_removes_worker_tmpdir_and_bare_id_sibling(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "scratch"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(scratch_root))
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    plan = repository / "docs" / "plans" / "sample.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        _plan_html.write_state(
            '<!doctype html><html><head><meta name="docs-project" content="sample">'
            '<title>Sample</title></head><body><main class="plan-doc"></main></body></html>',
            {
                "type": "plan",
                "slug": "sample",
                "title": "Sample",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "seed.txt", "docs")
    _git(repository, "commit", "-q", "-m", "seed repository")
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")

    bare_id = "r-20261004T140000000000"
    run_id = f"{bare_id}-sample"
    scratch = dispatch.worker_scratch_dir(run_id)
    environment = dispatch._worker_runtime_environment(
        {},
        run_id=run_id,
        manifest_path=str(tmp_path / "manifest.md"),
        attempt_started_at="2026-10-04T14:00:00Z",
        coordinator_session="sample",
        claude_headers=False,
    )
    stub = """
import os
from pathlib import Path

scratch = Path(os.environ["TMPDIR"])
(scratch / "inside.bin").write_bytes(b"inner")
bare_id = "-".join(os.environ["RECKON_RUN_ID"].split("-", 2)[:2])
sibling = scratch.parent / bare_id
sibling.mkdir()
(sibling / "outside.bin").write_bytes(b"outer!")
"""
    subprocess.run(
        [sys.executable, "-c", stub], env={**os.environ, **environment}, check=True
    )
    sibling = scratch_root / bare_id
    assert (scratch / "inside.bin").stat().st_size == 5
    assert (sibling / "outside.bin").stat().st_size == 6
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "sample",
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "created_at": "2026-10-04T14:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "scratch": str(scratch),
            "manifest_path": str(tmp_path / "absent.md"),
            "pid": None,
            "pid_start_time": None,
            "node": {
                "id": "sample",
                "plan": "sample",
                "section": "sample",
                "time_budget": "40m",
                "write_paths": [],
            },
        },
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="scratch release",
        completed_at="2026-10-04T14:05:00Z",
        root=repository,
    )

    release = promoted["release"]
    assert not scratch.exists()
    assert not sibling.exists()
    assert {row["path"]: row["bytes"] for row in release["scratch_removed_paths"]} == {
        str(scratch): 5,
        str(sibling): 6,
    }


def test_discard_names_each_removed_scratch_directory(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "scratch"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(scratch_root))
    bare_id = "r-20261004T140100000000"
    run_id = f"{bare_id}-sample"
    scratch = dispatch.ensure_worker_scratch(run_id)
    sibling = scratch_root / bare_id
    sibling.mkdir()
    (scratch / "inside.bin").write_bytes(b"inner")
    (sibling / "outside.bin").write_bytes(b"outer!")
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "repo": str(tmp_path),
            "worktree": str(tmp_path / "absent"),
            "scratch": str(scratch),
            "pid": None,
            "node": {"id": "sample"},
        },
    )

    discarded = crew.discard(run_id)

    assert not scratch.exists()
    assert not sibling.exists()
    assert {
        row["path"]: row["bytes"] for row in discarded["scratch_removed_paths"]
    } == {str(scratch): 5, str(sibling): 6}
