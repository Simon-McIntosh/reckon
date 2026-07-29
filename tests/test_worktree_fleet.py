from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "reckon-ship"
    / "scripts"
    / "worktree_fleet.py"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "test: seed repository")
    return repo


def command(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def create_worktree(repo: Path, session: str, worker: str) -> Path:
    result = command(
        repo,
        "create",
        "--repo",
        str(repo),
        "--session",
        session,
        "--worker",
        worker,
    )
    assert result.returncode == 0, result.stdout
    return Path(json.loads(result.stdout)["path"])


def cleanup(repo: Path, session: str) -> subprocess.CompletedProcess[str]:
    return command(
        repo,
        "cleanup-session",
        "--repo",
        str(repo),
        "--session",
        session,
        "--integrated-into",
        "HEAD",
    )


def test_cleanup_requires_reachable_commit(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "delivery", "worker-a")
    (worktree / "result.txt").write_text("result\n")
    git(worktree, "add", "result.txt")
    git(worktree, "commit", "-m", "test: add isolated result")
    worker_head = git(worktree, "rev-parse", "HEAD")

    refused = cleanup(repo, "delivery")
    assert refused.returncode == 2
    assert "reachable" in refused.stdout
    assert worktree.exists()

    git(repo, "merge", "--no-ff", worker_head, "-m", "test: integrate result")
    removed = cleanup(repo, "delivery")
    assert removed.returncode == 0, removed.stdout
    assert not worktree.exists()


def test_cleanup_refuses_dirty_worktree(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "dirty-check", "worker-b")
    dirty = worktree / "untracked.txt"
    dirty.write_text("not committed\n")

    refused = cleanup(repo, "dirty-check")
    assert refused.returncode == 2
    assert "untracked.txt" in refused.stdout
    assert worktree.exists()

    dirty.unlink()
    removed = cleanup(repo, "dirty-check")
    assert removed.returncode == 0, removed.stdout
    assert not worktree.exists()


def test_rejects_unsafe_session_token(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    result = command(
        repo,
        "create",
        "--repo",
        str(repo),
        "--session",
        "../outside",
        "--worker",
        "worker-c",
    )
    assert result.returncode == 2
    assert "session must match" in result.stdout
