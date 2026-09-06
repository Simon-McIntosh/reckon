from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import routing

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
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


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


def add_run_directory_worktree(repo: Path, path: Path) -> Path:
    """Register a real git worktree inside a run directory's checkout tree."""
    git(repo, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path.resolve()


def add_extraction(path: Path) -> Path:
    """Create a git-less tree of the kind a worker extracts for measuring."""
    path.mkdir(parents=True)
    (path / "seed.txt").write_text("seed\n")
    (path / ".gitignore").write_text("__pycache__/\n")
    return path.resolve()


def rows(report) -> dict[Path, dict]:
    return {Path(item["path"]): item for item in report["worktrees"]}


def test_a_registered_worktree_under_a_run_directory_is_reported(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    run_dir = home / "crew" / "runs" / "run-one"
    tree = add_run_directory_worktree(repo, run_dir / "checkouts" / "candidate")

    report = routing.garbage_collect(repo=repo)
    by_path = rows(report)

    assert tree in by_path
    item = by_path[tree]
    assert item["classification"] == "integrated"
    assert item["claimed_by_live_runs"] == []
    assert item["reclaimable"] is True


def test_an_extraction_with_no_git_directory_is_reported_as_its_own_kind(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    runs_root = home / "crew" / "runs"
    base = add_extraction(runs_root / "run-a" / "base-source")
    checkout = add_extraction(runs_root / "run-b" / "checkouts" / "candidate")

    report = routing.garbage_collect(repo=repo)
    by_path = rows(report)
    assert not (Path(str(base)) / ".git").exists()
    assert not (Path(str(checkout)) / ".git").exists()

    for tree in (base, checkout):
        item = by_path[tree]
        assert item["kind"] == "extraction"
        assert item["classification"] == "extraction"
        assert item["reclaimable"] is False
        assert "no git directory" in item["withheld"]


def test_a_run_directory_worktree_with_modified_files_is_held(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    run_dir = home / "crew" / "runs" / "run-one"
    tree = add_run_directory_worktree(repo, run_dir / "checkouts" / "candidate")
    (tree / "untracked.txt").write_text("dirty\n")

    report = routing.garbage_collect(repo=repo)
    item = rows(report)[tree]

    assert item["classification"] == "dirty"
    assert item["reclaimable"] is False
    assert "uncommitted changes" in item["withheld"]


def test_an_ordinary_managed_worktree_is_classified_exactly_as_today(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    managed = create_worktree(repo, "managed", "candidate")

    report = routing.garbage_collect(repo=repo)
    by_path = rows(report)

    assert by_path == {managed.resolve(): by_path[managed.resolve()]}
    assert by_path[managed.resolve()]["classification"] == "integrated"
    assert by_path[managed.resolve()]["reclaimable"] is True
    # The run-directory additions must leave the summary vocabulary untouched.
    assert report["counts"] == {
        "dirty": 0,
        "disposable": 0,
        "integrated": 1,
        "live-referenced": 0,
        "unintegrated": 0,
        "reclaimable": 1,
    }


def test_apply_removes_only_the_clean_contained_run_directory_worktree(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    runs_root = home / "crew" / "runs"
    integrated = add_run_directory_worktree(
        repo, runs_root / "run-clean" / "checkouts" / "candidate"
    )
    dirty = add_run_directory_worktree(
        repo, runs_root / "run-dirty" / "checkouts" / "candidate"
    )
    (dirty / "untracked.txt").write_text("dirty\n")
    extraction = add_extraction(runs_root / "run-extract" / "base-source")

    report = routing.garbage_collect(repo=repo, apply=True)

    assert report["removed_worktrees"] == [str(integrated)]
    assert not integrated.exists()
    assert dirty.exists()
    assert extraction.exists()
    assert report["counts"]["reclaimable"] == 1
