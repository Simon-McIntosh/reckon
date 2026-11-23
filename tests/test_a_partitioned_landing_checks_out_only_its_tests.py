"""A partitioned landing's local tree holds only the tests and top-level files.

A landing measured on a partition reads two things from its local tree: the test
files it selects and scans for imports, and the project files at the top. Its
arms build full trees on the nodes that run them. A full checkout on the fleet
node is gigabytes that nothing reads, so the tree carries only what is read, and
it stays a worktree of the real repository so the arm job finds the repository's
objects through it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import landing_measure


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "package").mkdir()
    (repo / "tests" / "test_a.py").write_text("import package.core\n")
    (repo / "tests" / "unit" / "test_b.py").write_text("x = 1\n")
    (repo / "package" / "core.py").write_text("VALUE = 1\n")
    (repo / "package" / "large.dat").write_bytes(b"0" * 4096)
    (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_a_tests_only_tree_holds_the_tests_and_top_level_files(
    repository: tuple[Path, str],
) -> None:
    repo, revision = repository
    with landing_measure.base_checkout(repo, revision, 30.0, tests_only=True) as tree:
        assert (tree / "tests" / "test_a.py").read_text() == "import package.core\n"
        assert (tree / "tests" / "unit" / "test_b.py").is_file()
        assert (tree / "pyproject.toml").is_file()
        assert not (tree / "package").exists()
        common = Path(
            _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        assert common == (repo / ".git").resolve()
        assert landing_measure.importer_population(["package/core.py"], tree) == (
            "tests/test_a.py",
        )
    assert not tree.exists()
    assert str(tree) not in _git(repo, "worktree", "list")


def test_a_full_checkout_still_carries_the_package(
    repository: tuple[Path, str],
) -> None:
    repo, revision = repository
    with landing_measure.base_checkout(repo, revision, 30.0) as tree:
        assert (tree / "package" / "core.py").is_file()
    assert not tree.exists()
