"""A discarded run releases a clean, integrated worktree so its node can rerun.

A run that died at launch and was discarded leaves its worktree on disk. When
that tree holds nothing — an empty ``git status --porcelain`` and no commit the
integration branch lacks — leaving it behind refuses a redispatch of the same
node, because the worktree path already exists. Discard releases it through the
same audit promotion applies on release, so the two never disagree about which
trees are safe to remove.

The unit under test is ``reckon/crew/promotion.py`` as imported into this
process. Everything else is synthesised under ``tmp_path`` — repository,
worktree, config home — so no real crew directory is read or written.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"
NODE = "discard-node"

# The declared negative control: with this set, discard's release step is a
# no-op, so the clean-worktree case must fail on the surviving path.
NEGATIVE_CONTROL = os.environ.get("DISCARD_RELEASE_NEGATIVE") == "1"


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
    """A real repository with its crew directories under a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed repository")
    return repo


def _worktree(repository: Path, tmp_path: Path, name: str) -> Path:
    worktree = tmp_path / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return worktree


def _discarded_run(repository: Path, run_id: str, worktree: Path) -> None:
    """A stopped run whose pointer names the given worktree, and its run home."""
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "cli",
            "role": "implement",
            "created_at": "2026-09-26T15:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "node": {
                "id": NODE,
                "plan": PLAN,
                "section": "§10",
                "time_budget": "40m",
                "write_paths": [],
            },
        },
    )


def _worktree_paths(repository: Path) -> list[str]:
    listed = _git(repository, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1].strip()
        for line in listed.splitlines()
        if line.startswith("worktree ")
    ]


def test_discard_releases_a_clean_integrated_worktree(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean tree at its base is gone after discard and no longer listed."""
    if NEGATIVE_CONTROL:
        monkeypatch.setattr(
            promotion,
            "_remove_discarded_worktree",
            lambda _record: {
                "worktree_released": False,
                "worktree_withheld": "release skipped",
            },
        )
    run_id = "r-20260926T150000000000-clean"
    worktree = _worktree(repository, tmp_path, "clean")
    assert _git(worktree, "status", "--porcelain") == ""
    _discarded_run(repository, run_id, worktree)

    result = crew.discard(run_id)

    assert not worktree.exists(), "the clean worktree path must be gone"
    assert result["worktree_released"] is True
    assert str(worktree) not in _worktree_paths(repository)


def test_a_dirty_worktree_survives_the_discard(
    repository: Path, tmp_path: Path
) -> None:
    """An uncommitted file withholds the tree, and the reason is in the result."""
    run_id = "r-20260926T150100000000-dirty"
    worktree = _worktree(repository, tmp_path, "dirty")
    (worktree / "uncommitted.txt").write_text("witness\n", encoding="utf-8")
    _discarded_run(repository, run_id, worktree)

    result = crew.discard(run_id)

    assert result["worktree_released"] is False
    assert worktree.is_dir()
    assert "uncommitted changes" in result["worktree_withheld"]


def test_an_unintegrated_worktree_survives_the_discard(
    repository: Path, tmp_path: Path
) -> None:
    """A commit the integration branch lacks withholds the tree for its commit."""
    run_id = "r-20260926T150200000000-unintegrated"
    worktree = _worktree(repository, tmp_path, "unintegrated")
    (worktree / "worker-result.txt").write_text("result\n", encoding="utf-8")
    _git(worktree, "add", "worker-result.txt")
    _git(worktree, "commit", "-q", "-m", "worker result")
    _discarded_run(repository, run_id, worktree)

    result = crew.discard(run_id)

    assert result["worktree_released"] is False
    assert worktree.is_dir()
    assert "not reachable from the integration revision" in result["worktree_withheld"]


def test_the_real_crew_directories_are_untouched(
    repository: Path, tmp_path: Path
) -> None:
    """Every path the discard touches resolves under the temporary home."""
    config_home = tmp_path / "config"
    assert crew.crew_home() == config_home / "crew"
    assert crew.runs_dir() == config_home / "crew" / "runs"
    assert promotion.discard_record_path("r-isolated") == (
        config_home / "crew" / "runs" / "r-isolated" / "discard.json"
    )
