from __future__ import annotations

import json
from pathlib import Path

from tests.test_worktree_fleet import command, git, repository


def create(repo, session: str, worker: str) -> dict:
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
    return json.loads(result.stdout)


def test_worktree_of_a_repo_with_root_venv_links_it_and_never_makes_a_directory(
    tmp_path,
) -> None:
    repo = repository(tmp_path)
    venv = repo / ".venv"
    venv.mkdir()
    (venv / "bin").mkdir()

    record = create(repo, "venue", "w-a")
    link = Path(record["path"]) / ".venv"

    assert link.is_symlink()
    assert link.resolve() == venv.resolve()
    assert record["provisioned"][".venv"] == f"linked -> {venv}"


def test_worktree_of_a_repo_without_venv_gets_no_entry_and_a_skip_reason(
    tmp_path,
) -> None:
    repo = repository(tmp_path)

    record = create(repo, "missing-venv", "w-b")
    worktree = Path(record["path"])

    assert not (worktree / ".venv").exists()
    assert "skipped" in record["provisioned"][".venv"]


def test_env_links_independently_of_venv(tmp_path) -> None:
    repo = repository(tmp_path)
    env = repo / ".env"
    env.write_text("KEY=value\n")

    record = create(repo, "env-only", "w-c")
    worktree = Path(record["path"])

    assert (worktree / ".env").is_symlink()
    assert (worktree / ".env").resolve() == env.resolve()
    assert record["provisioned"][".env"] == f"linked -> {env}"
    assert "skipped" in record["provisioned"][".venv"]
    assert not (worktree / ".venv").exists()


def test_provisioned_symlinks_are_ignored_so_the_worktree_reports_clean(
    tmp_path,
) -> None:
    repo = repository(tmp_path)
    venv = repo / ".venv"
    venv.mkdir()
    (repo / ".env").write_text("KEY=value\n")
    # Mirrors the project's own .gitignore, which carries the bare .venv and
    # .env patterns so a provisioned worktree stays clean.
    (repo / ".gitignore").write_text(".venv\n.venv/\n.env\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "test: ignore shared env links")

    record = create(repo, "clean-check", "w-d")
    worktree = Path(record["path"])

    status = git(worktree, "status", "--porcelain")
    assert status == ""


def test_create_succeeds_when_neither_source_exists(tmp_path) -> None:
    repo = repository(tmp_path)

    record = create(repo, "neither", "w-e")

    assert record["ok"] is True
    assert "skipped" in record["provisioned"][".venv"]
    assert "skipped" in record["provisioned"][".env"]
