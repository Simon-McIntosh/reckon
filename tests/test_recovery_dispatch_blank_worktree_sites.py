"""A missing run worktree cannot borrow the caller's repository."""

import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.dispatch import _inherited_worktree_reading


@pytest.mark.parametrize("site", ["commits", "diff", "handoff"])
def test_blank_worktree_ignores_unrelated_repository(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "unrelated"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (root / "evidence.txt").write_text("base\n")
    git("add", "evidence.txt")
    git("commit", "-qm", "test: establish fixture\n\nCreate an unrelated base.")
    base = git("rev-parse", "HEAD")
    (root / "evidence.txt").write_text("ambient change\n")
    git("commit", "-qam", "test: change fixture\n\nMake ambient history observable.")
    head = git("rev-parse", "HEAD")
    assert git("rev-list", "--count", f"{base}..HEAD") == "1"
    assert git("diff", "--name-only", base) == "evidence.txt"
    monkeypatch.chdir(root)

    if site == "commits":
        assert recovery._commits_beyond_base({"worktree": "", "base_sha": base}) == 0
    elif site == "diff":
        assert recovery._worktree_diff_paths({"worktree": "", "base_sha": base}) == []
    else:
        reading = _inherited_worktree_reading({"worktree": ""})
        assert "Inherited worktree could not be read." in reading
        assert head not in reading
