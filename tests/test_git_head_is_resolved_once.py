"""The repository HEAD is resolved once per commit, not once per lookup.

Finding HEAD was an uncached ``git rev-parse`` in front of two in-process date
maps, so every creation and last-modified lookup paid a subprocess before its
own map could answer. The memo under test keys the resolved sha on the identity
of ``HEAD`` and the ref file it names, so a second lookup against an unchanged
checkout starts no process, while a new commit changes that identity and the
next lookup resolves the new date.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from reckon import serve

FIRST_DATE = "2023-11-14T22:13:20+00:00"
SECOND_DATE = "2023-11-14T22:21:40+00:00"
THIRD_DATE = "2023-11-14T22:30:00+00:00"
FIRST_TS = 1_700_000_000
SECOND_TS = 1_700_000_500
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


class _CountingSubprocess:
    """A stand-in for the ``subprocess`` module that counts git invocations."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.git_calls = 0

    def run(self, args, *rest, **kwargs):
        if args and Path(str(args[0])).name == "git":
            self.git_calls += 1
        return self._real.run(args, *rest, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV, **(env or {})},
    )


def _rev(path: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return completed.stdout.strip()


def _commit(repo: Path, when: str) -> None:
    _git(repo, "add", "docs")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "test commit",
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    _git(repo, "init", "-q")
    (docs / "first.html").write_text("<html>first</html>", encoding="utf-8")
    _commit(repo, FIRST_DATE)
    return repo, docs


def _isolate_caches(monkeypatch) -> None:
    """Point every git-date cache at in-process, temporary state."""

    monkeypatch.setattr(serve, "_GIT_CREATION_CACHE", {})
    monkeypatch.setattr(serve, "_GIT_LAST_MODIFIED_CACHE", {})
    monkeypatch.setattr(serve, "_GIT_HEAD_MEMO", {})
    monkeypatch.setattr(serve, "_load_git_creation_cache", lambda key: None)
    monkeypatch.setattr(serve, "_load_git_last_modified_cache", lambda key: None)
    monkeypatch.setattr(serve, "_store_git_creation_cache", lambda *args: None)
    monkeypatch.setattr(serve, "_store_git_last_modified_cache", lambda *args: None)


def test_lookups_resolve_head_once(tmp_path, monkeypatch):
    repo, docs = _make_repo(tmp_path)
    _isolate_caches(monkeypatch)

    counter = _CountingSubprocess(subprocess)
    monkeypatch.setattr(serve, "subprocess", counter)

    # The first creation and last-modified lookup may each start processes.
    serve._git_first_committed(repo, docs)
    serve._git_last_committed(repo, docs)
    baseline = counter.git_calls
    assert baseline > 0

    # The remaining two lookups of each pair reuse the resolved head and their
    # own maps, so they start no git process for an unchanged checkout.
    for _ in range(2):
        serve._git_first_committed(repo, docs)
        serve._git_last_committed(repo, docs)
    assert counter.git_calls == baseline, (
        "a warm lookup for an unchanged checkout started a git process"
    )

    # A new commit changes the identity, so the next lookup resolves the new
    # date rather than reusing the memoised head.
    (docs / "second.html").write_text("<html>second</html>", encoding="utf-8")
    _commit(repo, SECOND_DATE)
    before = counter.git_calls
    times = serve._git_first_committed(repo, docs)
    assert counter.git_calls > before, "a new commit did not force a re-resolution"
    assert times["docs/second.html"] == SECOND_TS
    assert times["docs/first.html"] == FIRST_TS


def test_a_linked_worktree_resolves_a_new_commit(tmp_path, monkeypatch):
    """A worktree's refs live in the common git directory, not its own.

    Resolving the ref under the worktree's private git directory reads it as
    absent, so a commit made in the worktree would leave the memo's sha frozen
    and the date maps short-circuiting on the stale head.
    """

    repo, _docs = _make_repo(tmp_path)
    _isolate_caches(monkeypatch)
    counter = _CountingSubprocess(subprocess)
    monkeypatch.setattr(serve, "subprocess", counter)

    worktree = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "wtb", str(worktree))
    docs = worktree / "docs"

    head_before = serve._git_head(worktree)
    assert head_before == _rev(worktree)

    # An unchanged worktree reuses the memo.
    baseline = counter.git_calls
    assert serve._git_head(worktree) == head_before
    assert counter.git_calls == baseline

    # A commit in the worktree updates the common ref, so the next lookup
    # resolves the new head rather than the memo's stale sha.
    (docs / "third.html").write_text("<html>third</html>", encoding="utf-8")
    _commit(worktree, THIRD_DATE)
    after = counter.git_calls
    head_after = serve._git_head(worktree)
    assert counter.git_calls > after, "a worktree commit did not force a re-resolution"
    assert head_after == _rev(worktree)
    assert head_after != head_before
