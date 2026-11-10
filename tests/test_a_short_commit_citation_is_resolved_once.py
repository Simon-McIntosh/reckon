"""Commit citations reuse immutable answers and expire misses when HEAD moves."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.helpers.git import git

subject = importlib.import_module("reckon.crew.recovery_review_subject")
watch = importlib.import_module("reckon.crew.recovery_watch")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    tree = tmp_path / "repo"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "config", "user.name", "Fixture")
    git(tree, "config", "user.email", "fixture@example.invalid")
    git(tree, "commit", "--allow-empty", "-qm", "test: seed repository")
    sha = git(tree, "rev-parse", "HEAD")
    monkeypatch.setattr(subject, "_RESOLVE_COMMIT_CACHE", {})
    spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr(
        subject,
        "subprocess",
        SimpleNamespace(
            run=spy,
            TimeoutExpired=subprocess.TimeoutExpired,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    return tree, sha, spy


def test_twenty_cached_short_resolutions_start_git_once(repository):
    tree, sha, spy = repository
    for _ in range(20):
        assert subject._resolve_commit(tree, sha[:10], cached=True) == sha
    assert spy.call_count == 1
    assert watch._stored_commit_answer(tree, sha[:10]) == sha


def test_twenty_missing_short_resolutions_start_git_once(repository):
    tree, sha, spy = repository
    missing = "f" * 10 if not sha.startswith("f" * 10) else "e" * 10
    for _ in range(20):
        assert subject._resolve_commit(tree, missing, cached=True) == ""
    assert spy.call_count == 1
    assert watch._stored_commit_answer(tree, missing) == ""


def test_moving_head_retries_a_missing_short_resolution(repository):
    tree, sha, spy = repository
    missing = "f" * 10 if not sha.startswith("f" * 10) else "e" * 10
    assert subject._resolve_commit(tree, missing, cached=True) == ""
    git(tree, "commit", "--allow-empty", "-qm", "test: move head")
    assert subject._resolve_commit(tree, missing, cached=True) == ""
    assert spy.call_count == 2


def test_strict_resolution_starts_git_every_call(repository):
    tree, sha, spy = repository
    assert subject._resolve_commit(tree, sha[:10], cached=True) == sha
    spy.reset_mock()
    for _ in range(20):
        assert subject._resolve_commit(tree, sha[:10], strict=True, cached=True) == sha
    assert spy.call_count == 20


def test_a_ref_name_is_never_kept(repository):
    tree, sha, spy = repository
    for _ in range(20):
        assert subject._resolve_commit(tree, "main", cached=True) == sha
    assert spy.call_count == 20
    assert watch._stored_commit_answer(tree, "main") == ""
    assert (str(tree), "main") not in subject._RESOLVE_COMMIT_CACHE
    git(tree, "commit", "--allow-empty", "-qm", "test: move branch")
    moved = git(tree, "rev-parse", "HEAD")
    assert moved != sha
    assert subject._resolve_commit(tree, "main", cached=True) == moved


def test_a_new_process_can_read_the_short_answer_from_the_store(repository):
    tree, sha, spy = repository
    assert subject._resolve_commit(tree, sha[:10], cached=True) == sha
    subject._RESOLVE_COMMIT_CACHE.clear()
    assert subject._resolve_commit(tree, sha[:10], cached=True) == sha
    assert spy.call_count == 1


def test_a_hex_ref_that_is_not_a_prefix_is_never_kept(repository):
    tree, sha, spy = repository
    candidate = "f" * 10 if not sha.startswith("f" * 10) else "e" * 10
    git(tree, "branch", candidate)
    for _ in range(2):
        assert subject._resolve_commit(tree, candidate, cached=True) == sha
    assert spy.call_count == 2
    assert watch._stored_commit_answer(tree, candidate) == ""


def test_instrument_failure_is_retried_and_strictly_refused(repository):
    tree, sha, spy = repository
    spy.side_effect = subprocess.TimeoutExpired("git", 10)
    for _ in range(2):
        assert subject._resolve_commit(tree, sha[:10], cached=True) == ""
    assert spy.call_count == 2
    with pytest.raises(subject.CrewError, match="instrument fault"):
        subject._resolve_commit(tree, sha[:10], strict=True, cached=True)
    assert spy.call_count == 3
