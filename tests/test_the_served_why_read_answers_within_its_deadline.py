"""A git date lookup that ran out of time is not paid again on the next discovery.

Every project discovery asks for the first-commit and last-modified maps, and
each of those runs a bounded ``git log``. On a mount where that query does not
finish inside its ten-second bound, the lookup returns its fallback and records
nothing, so the next discovery at the same commit pays the timeout again — a
served read that has to build its inventory first spends two of these before
it reaches the answer it was asked for, on every fresh discovery.

These tests drive the real reader the discovery calls, with a stand-in for the
``subprocess`` module that lets ``git rev-parse`` answer normally and makes
``git log`` run past its bound, and count the ``git log`` spawns through it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from reckon import git_dates

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str, extra: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV, **(extra or {})},
    )


def _commit(repo: Path, when: str) -> None:
    _git(repo, "add", "docs")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "add a doc page",
        extra={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    _git(repo, "init", "-q")
    (docs / "first.html").write_text("<html>first</html>", encoding="utf-8")
    _commit(repo, "2023-11-14T22:13:20+00:00")
    return repo, docs


class _GitLogTimesOut:
    """Run real git, except that ``git log`` runs past the lookup's bound."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.log_spawns = 0

    def run(self, args, *rest, **kwargs):
        if list(args)[:2] == ["git", "log"]:
            self.log_spawns += 1
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
        return self._real.run(args, *rest, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _PersistedMaps:
    """The on-disk maps, held in memory so a cleared memo reads as a fresh process."""

    def __init__(self) -> None:
        self.creation: dict[tuple[str, str], git_dates._GitCreationEntry] = {}
        self.last_modified: dict[tuple[str, str], git_dates._GitCreationEntry] = {}

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(
            git_dates,
            "_load_git_creation_cache",
            lambda key: self.creation.get(key),
        )
        monkeypatch.setattr(
            git_dates,
            "_store_git_creation_cache",
            lambda key, entry: self.creation.__setitem__(key, entry),
        )
        monkeypatch.setattr(
            git_dates,
            "_load_git_last_modified_cache",
            lambda key: self.last_modified.get(key),
        )
        monkeypatch.setattr(
            git_dates,
            "_store_git_last_modified_cache",
            lambda key, entry: self.last_modified.__setitem__(key, entry),
        )


def _clear_memo(monkeypatch) -> None:
    monkeypatch.setattr(git_dates, "_GIT_CREATION_CACHE", {})
    monkeypatch.setattr(git_dates, "_GIT_LAST_MODIFIED_CACHE", {})
    monkeypatch.setattr(git_dates, "_GIT_HEAD_MEMO", {})


def _discover(repo: Path, docs: Path) -> tuple[dict[str, int], dict[str, int]]:
    """The two date lookups every project discovery makes before its inventory."""

    return (
        git_dates._git_first_committed(repo, docs),
        git_dates._git_last_committed(repo, docs),
    )


def test_a_timed_out_lookup_is_not_spawned_again_at_one_head(tmp_path, monkeypatch):
    repo, docs = _make_repo(tmp_path)
    _clear_memo(monkeypatch)
    _PersistedMaps().install(monkeypatch)
    git = _GitLogTimesOut(subprocess)
    monkeypatch.setattr(git_dates, "subprocess", git)

    first = _discover(repo, docs)
    after_first = git.log_spawns
    assert after_first == 2, (
        "the first discovery did not spawn one bounded log query per lookup"
    )
    assert first == ({}, {}), "a timed-out lookup did not fall back to an empty map"

    # A second discovery at the same commit reuses what the first one recorded.
    second = _discover(repo, docs)
    assert git.log_spawns == after_first, (
        "the next discovery at one HEAD paid the timed-out lookup again: "
        f"{git.log_spawns - after_first} extra spawn(s)"
    )
    assert second == first, "a reused record answered differently from the timeout"

    # A commit moves HEAD, so the lookups resolve afresh.
    (docs / "second.html").write_text("<html>second</html>", encoding="utf-8")
    _commit(repo, "2023-11-14T22:21:40+00:00")
    before_move = git.log_spawns
    third = _discover(repo, docs)
    assert git.log_spawns == before_move + 2, (
        "a new HEAD did not resolve the date lookups afresh"
    )
    assert third == ({}, {}), "a lookup after the move did not fall back as before"


def test_a_fresh_process_reuses_the_recorded_lookup(tmp_path, monkeypatch):
    """The record outlives the process memo, so a restart does not re-pay it."""

    repo, docs = _make_repo(tmp_path)
    _clear_memo(monkeypatch)
    maps = _PersistedMaps()
    maps.install(monkeypatch)
    git = _GitLogTimesOut(subprocess)
    monkeypatch.setattr(git_dates, "subprocess", git)

    _discover(repo, docs)
    assert git.log_spawns == 2

    # A new process: the in-memory maps are empty, the persisted record is not.
    _clear_memo(monkeypatch)
    spawns = git.log_spawns
    _discover(repo, docs)
    assert git.log_spawns == spawns, (
        "a fresh process re-paid a lookup that had already run out of time"
    )