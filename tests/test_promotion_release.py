"""Promotion releases the worktree and process a completed run made transient."""

from __future__ import annotations

import json
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

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "reckon-ship"
    / "scripts"
    / "worktree_fleet.py"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "seed.txt").write_text("seed\n")
    git(root, "add", "seed.txt", "docs")
    git(root, "commit", "-q", "-m", "test: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def create_worktree(repo: Path, session: str, worker: str, base: str = "HEAD") -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--repo",
            str(repo),
            "--session",
            session,
            "--worker",
            worker,
            "--base",
            base,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def write_manifest(run_dir: Path, *, status: str = "complete") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "manifest.md"
    path.write_text(f"status: {status}\n", encoding="utf-8")
    return path


def write_pointer(
    run_id: str,
    *,
    repo: Path,
    worktree: Path,
    base_sha: str,
    manifest_path: Path | None = None,
    pid: int | None = None,
    pid_start_time: str | None = None,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-03T00:00:00Z",
            "base_sha": base_sha,
            "manifest_path": str(manifest_path)
            if manifest_path
            else "/nonexistent/manifest.md",
            "pid": pid,
            "pid_start_time": pid_start_time,
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "35m",
                "write_paths": [],
            },
        },
    )


def _spawn_process() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


def test_a_clean_ancestor_worktree_and_its_process_are_both_released(
    repository: Path,
) -> None:
    run_id = "r-20260903T000000000000-node-a"
    payload = create_worktree(repository, "run", "worker")
    worktree = Path(payload["path"])
    base_sha = payload["base_sha"]

    proc = _spawn_process()
    start_time = _process_start_time(proc.pid)
    try:
        manifest = write_manifest(worktree.parent / f"{run_id}-manifest")
        write_pointer(
            run_id,
            repo=repository,
            worktree=worktree,
            base_sha=base_sha,
            manifest_path=manifest,
            pid=proc.pid,
            pid_start_time=start_time,
        )

        promoted = crew.complete(
            run_id,
            gate="passed",
            outcome="clean worktree, both released",
            completed_at="2026-09-03T00:05:00Z",
            root=repository,
        )

        assert promoted["release"]["worktree_released"] is True
        assert not worktree.exists()
        assert promoted["release"]["process_signalled"] is True

        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert proc.poll() is not None

        run = ledger.load(PROJECT, repository)[0]["runs"][0]
        assert run["gate"] == "passed"
    finally:
        if process_alive(proc.pid) is True:
            proc.kill()
        proc.wait(timeout=5)


def test_a_dirty_worktree_is_kept_and_named_in_the_result(repository: Path) -> None:
    run_id = "r-20260903T000100000000-node-b"
    payload = create_worktree(repository, "run", "worker-dirty")
    worktree = Path(payload["path"])
    (worktree / "untracked.txt").write_text("uncommitted\n", encoding="utf-8")

    write_pointer(
        run_id, repo=repository, worktree=worktree, base_sha=payload["base_sha"]
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="dirty worktree stays",
        completed_at="2026-09-03T00:05:00Z",
        root=repository,
    )

    release = promoted["release"]
    assert release["worktree_released"] is False
    assert "uncommitted changes" in release["worktree_withheld"]
    assert worktree.exists()


def test_a_non_ancestor_worktree_is_kept_and_named_in_the_result(
    repository: Path,
) -> None:
    run_id = "r-20260903T000200000000-node-c"
    payload = create_worktree(repository, "run", "worker-divergent")
    worktree = Path(payload["path"])
    (worktree / "own-commit.txt").write_text("only here\n", encoding="utf-8")
    git(worktree, "add", "own-commit.txt")
    git(worktree, "commit", "-q", "-m", "test: divergent work")

    write_pointer(
        run_id, repo=repository, worktree=worktree, base_sha=payload["base_sha"]
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="divergent worktree stays",
        completed_at="2026-09-03T00:05:00Z",
        root=repository,
        no_commit="testing worktree release policy; this commit is deliberately uncited",
    )

    release = promoted["release"]
    assert release["worktree_released"] is False
    assert "not reachable" in release["worktree_withheld"]
    assert worktree.exists()


def test_a_run_with_no_terminal_manifest_is_never_signalled(repository: Path) -> None:
    run_id = "r-20260903T000300000000-node-d"
    payload = create_worktree(repository, "run", "worker-no-manifest")
    worktree = Path(payload["path"])

    proc = _spawn_process()
    start_time = _process_start_time(proc.pid)
    try:
        write_pointer(
            run_id,
            repo=repository,
            worktree=worktree,
            base_sha=payload["base_sha"],
            pid=proc.pid,
            pid_start_time=start_time,
        )

        promoted = crew.complete(
            run_id,
            gate="passed",
            outcome="no manifest, no signal",
            completed_at="2026-09-03T00:05:00Z",
            root=repository,
        )

        release = promoted["release"]
        assert release["process_signalled"] is False
        assert process_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_a_failure_inside_release_leaves_the_ledger_row_intact(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20260903T000400000000-node-e"
    payload = create_worktree(repository, "run", "worker-failing-release")
    worktree = Path(payload["path"])

    write_pointer(
        run_id, repo=repository, worktree=worktree, base_sha=payload["base_sha"]
    )

    def _boom(record):
        raise RuntimeError("simulated release failure")

    monkeypatch.setattr(promotion, "_release_run_workspace", _boom)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="release blows up but the ledger still lands",
        completed_at="2026-09-03T00:05:00Z",
        root=repository,
    )

    assert "raised" in promoted["release"]["worktree_withheld"]
    run = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert run["run_id"] == run_id
    assert run["gate"] == "passed"
    assert not pointer_path(run_id).exists()

    # The run is recoverable: re-completing it takes the already-promoted path
    # and does not fail even though the worktree was never actually released.
    write_pointer(
        run_id, repo=repository, worktree=worktree, base_sha=payload["base_sha"]
    )
    replay = crew.complete(
        run_id,
        gate="passed",
        outcome="release blows up but the ledger still lands",
        completed_at="2026-09-03T00:05:00Z",
        root=repository,
    )
    assert replay["already_promoted"] is True
