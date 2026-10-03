"""gc names the tree it could not read and reports the trees it already removed.

Measured defect: a sweep stopped mid-pass with only ``git rev-parse HEAD
failed: fatal: not a git repository`` — no directory named, and no word for the
worktrees it had already deleted before it reached the tree that failed. Two
obligations follow. A git failure names the directory its command ran in. A
registered worktree whose directory is not a git repository is a reported row
naming its path and the reason, and the sweep continues past it, so one
unreadable registration cannot stop the reclaim of every other tree. Any other
mid-sweep failure still reports the steps already applied — the removed list,
the rows already judged and the path the failure was reached through — before
the command exits nonzero.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import routing

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "reckon-build"
    / "scripts"
    / "worktree_fleet.py"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-q", "-m", "test: seed")
    return repo


def create_worktree(repo: Path, session: str, worker: str) -> Path:
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
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(json.loads(result.stdout)["path"])


def merged_worktree(repo: Path, session: str, worker: str) -> Path:
    """A clean worktree whose commit the repository's main branch carries."""
    worktree = create_worktree(repo, session, worker)
    delivered = f"delivered-{worker}.txt"
    (worktree / delivered).write_text("delivered\n")
    git(worktree, "add", delivered)
    git(worktree, "commit", "-q", "-m", "test: delivered work")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        git(worktree, "rev-parse", "HEAD"),
        "-m",
        "test: merge the delivered work",
    )
    return worktree


def registered(repo: Path) -> set[Path]:
    return {path.resolve() for path in routing._registered_worktrees(repo)}


def test_a_worktree_that_is_not_a_repository_is_a_row_beside_the_release(
    tmp_path: Path, monkeypatch
) -> None:
    """One unreadable registration must not stop the reclaim of the rest.

    A directory left where a worktree was — not a git repository any more — is
    reported with its own path and git's reason, and the pass continues to the
    tree that is reclaimable. The sweep completes without a failure, so its
    exit status is the ordinary success status.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    released = merged_worktree(repo, "s21-nova", "integrated")
    broken = create_worktree(repo, "s21-nova", "broken")
    shutil.rmtree(broken)
    broken.mkdir()
    assert broken.is_dir() and not (broken / ".git").exists()
    assert broken.resolve() in registered(repo)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True

    # The reclaimable tree is reclaimed and named in the report.
    assert payload["removed_worktrees"] == [str(released)]
    assert not released.exists()

    # The tree that is not a repository is a row naming its path and the
    # reason, rather than a sweep failure.
    rows = {Path(item["path"]).name: item for item in payload["worktrees"]}
    assert rows["broken"]["path"] == str(broken)
    assert rows["broken"]["classification"] == "unavailable"
    assert "not a git repository" in rows["broken"]["detail"]
    assert rows["broken"]["reclaimable"] is False
    assert "not a git working tree" in rows["broken"]["withheld"]

    # Nothing else is touched: the broken directory and its registration stay.
    assert broken.is_dir()
    assert str(broken) not in payload["removed_worktrees"]
    assert broken.resolve() in registered(repo)


def test_a_git_failure_names_the_directory_it_ran_in(tmp_path: Path) -> None:
    """The message names the directory, the command and git's own output."""
    directory = tmp_path / "not-a-repository"
    directory.mkdir()

    with pytest.raises(routing.CrewError) as caught:
        routing._git(directory, "rev-parse", "HEAD")

    message = str(caught.value)
    assert str(directory) in message
    assert "git rev-parse HEAD" in message
    assert "not a git repository" in message


def test_a_stopped_sweep_reports_the_steps_it_already_applied(
    tmp_path: Path, monkeypatch
) -> None:
    """A gc that has already deleted a worktree says so before it exits.

    A worktree locked by another session refuses removal after an earlier tree
    was released. The command exits nonzero, and the report it prints names the
    tree already removed, the rows already judged, and the path the failure was
    reached through.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    released = merged_worktree(repo, "s1-nova", "released")
    locked = merged_worktree(repo, "s2-nova", "locked")
    git(repo, "worktree", "lock", str(locked))

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 1, result.output
    assert payload["ok"] is False
    assert str(released) in payload["removed_worktrees"]
    assert not released.exists()
    assert payload["failed_path"] == str(locked)
    assert str(locked) in payload["error"]

    rows = {Path(item["path"]).name: item for item in payload["worktrees"]}
    assert set(rows) == {"released", "locked"}
    assert locked.exists()
