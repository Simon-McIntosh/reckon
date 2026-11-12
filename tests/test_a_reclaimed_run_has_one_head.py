"""A reclaimed run's review and promotion name one head.

A run whose worktree has been reclaimed has its head read from the manifest's
``commits:`` field, which is a citation list and not an ordered history: a
worker may write it newest-first, oldest-first or in landing order. Two readers
that disagree on how to order it name different heads for the same run, and a
review of a newest-first list then scores the oldest commit the run landed.

Both readers therefore resolve the head through one function, which compares the
citations by ancestry. These cases pin that: for each ordering, the review
subject and the promotion's head are the newest commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew.promotion_records import _run_head
from reckon.crew.recovery_review_dispatch import _run_head_for_review


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def chained(tmp_path: Path) -> tuple[Path, list[str]]:
    """A repository with three chained commits, oldest first."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    shas: list[str] = []
    for index in range(3):
        (repo / f"step-{index}.txt").write_text(f"{index}\n")
        _git(repo, "add", f"step-{index}.txt")
        _git(repo, "commit", "-q", "-m", f"step {index}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return repo, shas


def _reclaimed_record(tmp_path: Path, repo: Path, commits: list[str]) -> dict:
    """A run record whose worktree is absent from disk, citing ``commits``."""
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "\n".join(["node: node-a", "status: complete", "commits: " + ",".join(commits)])
        + "\n",
        encoding="utf-8",
    )
    worktree = tmp_path / "reclaimed-worktree"
    return {
        "run_id": "r-one-head",
        "worktree": str(worktree),
        "repo": str(repo),
        "manifest_path": str(manifest),
        "node": {"id": "node-a"},
    }


def _heads(record: dict) -> tuple[str, str]:
    return _run_head_for_review(record), _run_head(record)


def test_a_newest_first_citation_list_names_the_newest_head(
    tmp_path: Path, chained: tuple[Path, list[str]]
) -> None:
    """The last entry of a newest-first list is the oldest commit, not the head."""
    repo, shas = chained
    record = _reclaimed_record(tmp_path, repo, list(reversed(shas)))
    assert not Path(record["worktree"]).is_dir()

    review, promotion = _heads(record)

    assert review == shas[-1]
    assert promotion == shas[-1]


def test_an_oldest_first_citation_list_names_the_newest_head(
    tmp_path: Path, chained: tuple[Path, list[str]]
) -> None:
    """Landing order reaches the same head as the reverse ordering."""
    repo, shas = chained
    record = _reclaimed_record(tmp_path, repo, list(shas))
    assert not Path(record["worktree"]).is_dir()

    review, promotion = _heads(record)

    assert review == shas[-1]
    assert promotion == shas[-1]
    assert review == promotion
