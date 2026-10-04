"""An empty worktree field is absent, never the directory a process runs in.

A promotion reads the run's worktree from its live pointer to resolve the
revision it asserts. Built as ``Path(str(record.get("worktree") or ""))``, an
empty field becomes ``Path(".")``, which is a directory, so a pointer carrying
no worktree resolved its revision from whatever repository the promotion
happened to start in. That is a confident wrong answer rather than a missing
one: the revision recorded as landed would be the head of an unrelated
repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew.promotion import _run_promoted_revision


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_repository(root: Path) -> str:
    root.mkdir(parents=True)
    # The path inside the content keeps two repositories seeded in the same
    # second from hashing to one commit, which would let the ambient head
    # stand in for the run's head unnoticed.
    (root / "file.txt").write_text(f"{root}\n")
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "worker@example.invalid", cwd=root)
    _git("config", "user.name", "Worker", cwd=root)
    _git("add", "file.txt", cwd=root)
    _git("commit", "-q", "-m", "seed repository", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture()
def unrelated_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """A git repository the promotion was never dispatched for, as the cwd."""
    root = tmp_path / "unrelated"
    head = _seed_repository(root)
    monkeypatch.chdir(root)
    return root, head


@pytest.mark.parametrize(
    "record",
    [
        {"worktree": "", "repo": ""},
        {"worktree": "   ", "repo": ""},
        {"repo": ""},
    ],
    ids=["empty", "whitespace", "absent"],
)
def test_an_empty_worktree_field_never_resolves_the_ambient_repository(
    unrelated_repository: tuple[Path, str], record: dict[str, str]
) -> None:
    _, ambient_head = unrelated_repository

    resolved = _run_promoted_revision(record, [])

    assert resolved != ambient_head
    assert resolved == ""


def test_a_record_with_no_worktree_reads_the_repository_it_names(
    tmp_path: Path, unrelated_repository: tuple[Path, str]
) -> None:
    run_tree = tmp_path / "run-tree"
    run_head = _seed_repository(run_tree)

    resolved = _run_promoted_revision({"worktree": "", "repo": str(run_tree)}, [])

    assert resolved == run_head


def test_a_named_worktree_still_resolves_its_own_head(tmp_path: Path) -> None:
    run_tree = tmp_path / "named-tree"
    run_head = _seed_repository(run_tree)

    resolved = _run_promoted_revision(
        {"worktree": str(run_tree), "repo": ""},
        [],
    )

    assert resolved == run_head
