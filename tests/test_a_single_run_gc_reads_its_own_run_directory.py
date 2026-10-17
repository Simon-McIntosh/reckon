from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import routing
from reckon.crew.runs import _write_json, pointer_path

RUN_DIRECTORY_COUNT = 200


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


def add_extraction(path: Path) -> Path:
    """Create a git-less tree of the kind a worker extracts for measuring."""
    path.mkdir(parents=True)
    (path / "seed.txt").write_text("seed\n")
    return path.resolve()


def park_run(repo: Path, run_id: str, worktree: Path) -> None:
    """Record a live pointer so gc resolves the named run's own tree."""
    _write_json(
        Path(pointer_path(run_id)),
        {
            "run_id": run_id,
            "pid": 0,
            "worktree": str(worktree),
            "repo": str(repo),
            "phase": "complete",
        },
    )


def build_runs_root(home: Path, extraction_runs: set[str]) -> Path:
    runs_root = home / "crew" / "runs"
    for index in range(RUN_DIRECTORY_COUNT):
        run_id = f"run-{index:03d}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        if run_id in extraction_runs:
            add_extraction(run_dir / "base-source")
    return runs_root


def extraction_rows(report: dict) -> list[dict]:
    return [item for item in report["worktrees"] if item.get("kind") == "extraction"]


def listed_run_directories(report: dict) -> set[str]:
    return {Path(item["path"]).parent.name for item in extraction_rows(report)}


def test_a_single_run_gc_reads_only_that_runs_directory(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    named = "run-000"
    others = {"run-001", "run-050", "run-199"}
    runs_root = build_runs_root(home, {named} | others)
    park_run(repo, named, runs_root / named / "held-tree")

    report = routing.garbage_collect(repo=repo, run_id=named)

    expected = (runs_root / named / "base-source").resolve()
    assert [Path(item["path"]) for item in extraction_rows(report)] == [expected]
    assert listed_run_directories(report) == {named}
    assert len(listed_run_directories(report)) == 1


def test_a_gc_naming_no_run_still_reports_every_extraction(
    tmp_path: Path, home: Path
) -> None:
    repo = repository(tmp_path)
    named = "run-000"
    others = {"run-001", "run-050", "run-199"}
    build_runs_root(home, {named} | others)

    report = routing.garbage_collect(repo=repo)

    assert listed_run_directories(report) == {named} | others
    assert len(extraction_rows(report)) == 4
