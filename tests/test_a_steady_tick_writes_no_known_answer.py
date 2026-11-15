"""A steady tick stores no git answer the store already holds.

The crew home's git answer store keeps one file per tree, shared by every process
reading it, and its write is an exclusive lock plus a whole-file rewrite through
a rename. ``_resolve_commit`` is called uncached while a tick composes each run's
review subject, so every tick re-resolved the same citations and rewrote the same
tree's file with the values it already carried: the lock was taken once per
citation, and each rewrite handed the file a new stat identity, which is what
stopped a reader's held copy from matching.

The store holds answers that are functions of immutable object ids, so an answer
already present is the answer the caller is about to store. Only a new or
different answer may write; a second pass over the same citations must leave the
file's identity untouched.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.git import git

subject = importlib.import_module("reckon.crew.recovery_review_subject")
watch = importlib.import_module("reckon.crew.recovery_watch")


def _seed_commits(tree: Path, count: int) -> list[str]:
    """Make ``count`` commits and return their full object ids in order."""
    shas: list[str] = []
    for index in range(count):
        git(tree, "commit", "--allow-empty", "-qm", f"test: commit {index}")
        shas.append(git(tree, "rev-parse", "HEAD"))
    return shas


@pytest.fixture()
def tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temporary crew home, a repository of 20 commits, and a write recorder."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(subject, "_RESOLVE_COMMIT_CACHE", {})
    tree = tmp_path / "repo"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "config", "user.name", "Fixture")
    git(tree, "config", "user.email", "fixture@example.invalid")
    shas = _seed_commits(tree, 20)

    writes: list[str] = []
    store_path = watch._git_answer_tree_path(tree)
    store_writer = watch._update_git_answer_store

    def recording(updated_tree: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
        writes.append(str(watch._git_answer_tree_path(updated_tree)))
        store_writer(updated_tree, mutate)

    monkeypatch.setattr(watch, "_update_git_answer_store", recording)
    return tree, shas, writes


def test_a_second_pass_over_stored_answers_writes_nothing(tick):
    tree, shas, writes = tick
    candidates = [sha[:10] for sha in shas]

    first = [subject._resolve_commit(tree, candidate) for candidate in candidates]
    assert first == shas
    assert len(writes) == 20

    store_path = watch._git_answer_tree_path(tree)
    assert store_path.is_file()
    before = store_path.stat()

    writes.clear()
    second = [subject._resolve_commit(tree, candidate) for candidate in candidates]
    after = store_path.stat()

    assert second == first
    assert len(writes) == 0
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_new_answer_is_written_once_and_reads_back_from_the_store(tick):
    tree, shas, writes = tick
    for sha in shas:
        assert subject._resolve_commit(tree, sha[:10]) == sha

    git(tree, "commit", "--allow-empty", "-qm", "test: twenty-first commit")
    fresh = git(tree, "rev-parse", "HEAD")
    assert fresh not in shas

    writes.clear()
    assert subject._resolve_commit(tree, fresh[:10]) == fresh
    assert len(writes) == 1
    assert watch._stored_commit_answer(tree, fresh[:10]) == fresh
