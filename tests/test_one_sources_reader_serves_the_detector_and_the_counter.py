"""One revision-source reader serves both the detector and the counter.

The clone detector and the interface counter each read a revision's Python
sources. Both read through the one public sources reader on ``reckon.velocity``:
the counter scopes it to its ``reckon`` prefix, the detector takes the whole
tracked tree. This test reads one synthesised revision through both consumers,
asserts they reached the one reader, and checks the scope each passes — the
detector's set carries a tracked ``.py`` outside ``reckon/`` and ``tests/`` that
the counter's prefix excludes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from reckon import clones, interface_counts, velocity

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")

COUNTER_PATH = "reckon/pkg/module.py"
TEST_PATH = "tests/test_thing.py"
OUTSIDE_PATH = "scripts/tool.py"
NON_PYTHON = "data/notes.txt"

_COUNTER = "def counted():\n    return 1\n"
_TEST = "def checked():\n    return 2\n"
_OUTSIDE = "def outside():\n    return 3\n"


def _git_env() -> dict[str, str]:
    # A git wrapper keyed on the running worker's identity refuses a mutating
    # verb outside the worker's own worktree, so drop the inherited identity
    # before pointing the subprocess at the synthesised repository.
    return {
        name: value
        for name, value in os.environ.items()
        if name not in _DISPATCH_IDENTITY
    }


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return result.stdout.strip()


def _write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", path)


def _commit(repo: Path, message: str) -> None:
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "-q",
        "-m",
        message,
    )


def _build_repository(root: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for path, content in (
        (COUNTER_PATH, _COUNTER),
        (TEST_PATH, _TEST),
        (OUTSIDE_PATH, _OUTSIDE),
        (NON_PYTHON, "notes\n"),
    ):
        _write(repo, path, content)
    _commit(repo, "feat: seed")
    first = _git(repo, "rev-parse", "HEAD")
    _write(repo, COUNTER_PATH, _COUNTER + "def added():\n    return 4\n")
    _commit(repo, "feat: extend the counted module")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_counter_and_detector_read_one_revision_through_one_reader(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "velocity-cache"))
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(tmp_path / "clone-cache"))
    repo, first, second = _build_repository(tmp_path / "code")

    # One reader: both consumers hold the very function velocity defines.
    assert interface_counts.read_sources is velocity.read_sources
    assert clones.read_sources is velocity.read_sources

    calls: list[tuple[str, str, set[str]]] = []
    real = velocity.read_sources

    def spy(repo_arg, revision, *, prefix=""):
        sources = real(repo_arg, revision, prefix=prefix)
        calls.append((revision, prefix, set(sources)))
        return sources

    monkeypatch.setattr(velocity, "read_sources", spy)
    monkeypatch.setattr(interface_counts, "read_sources", spy)
    monkeypatch.setattr(clones, "read_sources", spy)

    trees = interface_counts.read_trees(repo, second)  # the counter's prefixed read
    clones.promotion_clone_matches(repo, base_sha=first, tip=second)  # the detector's

    by_prefix: dict[str, set[str]] = {}
    for _revision, prefix, keys in calls:
        by_prefix.setdefault(prefix, set()).update(keys)

    # Both consumers reached the one reader: the counter under its reckon prefix,
    # the detector with no prefix.
    assert "reckon" in by_prefix
    assert "" in by_prefix

    # The detector reads the whole tracked tree, so its set carries a .py outside
    # reckon/ and tests/ that the counter's prefix excludes.
    assert OUTSIDE_PATH in by_prefix[""]
    assert OUTSIDE_PATH not in by_prefix["reckon"]
    assert TEST_PATH not in by_prefix["reckon"]

    # The counter's parsed set is the reader's reckon-scoped set.
    assert COUNTER_PATH in trees
    assert OUTSIDE_PATH not in trees

    # The reader applies the .py suffix: no non-Python path is read, at any
    # prefix.
    assert NON_PYTHON not in by_prefix[""]
    assert all(path.endswith(".py") for keys in by_prefix.values() for path in keys)
