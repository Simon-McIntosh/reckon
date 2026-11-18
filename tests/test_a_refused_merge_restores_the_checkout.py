"""A refused ref update cannot leave an unlanded tree serving as source."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from reckon.crew import merge
from tests.helpers.git import git


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("RECKON_CREW_RUN_ID", raising=False)
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    for path in ("source.py", "removed.py", "peer.txt"):
        (repo / path).write_text("base\n")
    git(repo, "add", "source.py", "removed.py", "peer.txt")
    git(repo, "commit", "-qm", "test: seed the checkout")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return repo


def delivered(repo, tmp_path, name, content):
    worker = tmp_path / name
    git(repo, "worktree", "add", "--detach", str(worker), "HEAD")
    (worker / "source.py").write_text(content)
    (worker / "added.py").write_bytes(b"\x00binary\xff\n")
    (worker / "removed.py").unlink()
    (worker / "link.py").symlink_to("source.py")
    (worker / "executable.py").write_text("executable\n")
    (worker / "executable.py").chmod(0o755)
    git(
        worker, "add", "source.py", "added.py", "removed.py", "link.py", "executable.py"
    )
    git(worker, "commit", "-qm", "test: deliver source")
    return git(worker, "rev-parse", "HEAD")


def digest(repo):
    paths = [
        "source.py",
        "added.py",
        "removed.py",
        "link.py",
        "executable.py",
        "peer.txt",
    ]
    index = git(repo, "ls-files", "--stage", "--", *paths)
    worktree = []
    for name in paths:
        path = repo / name
        if path.is_symlink():
            worktree.append((name, "link", os.readlink(path)))
        elif path.exists():
            worktree.append(
                (
                    name,
                    path.stat().st_mode,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        else:
            worktree.append((name, None))
    return index, worktree


def land(repo, head, **kwargs):
    return merge.land_merge(
        repo,
        head=head,
        message="fix: land reviewed source",
        body="Carry reviewed source onto the primary branch.",
        release=False,
        attempts=1,
        **kwargs,
    )


def test_a_refused_ref_update_restores_the_index_and_worktree(checkout, tmp_path):
    repo = checkout
    head = delivered(repo, tmp_path, "first", "first delivery\n")
    other = delivered(repo, tmp_path, "second", "second delivery\n")
    (repo / "peer.txt").write_text("peer staged\n")
    git(repo, "add", "peer.txt")
    (repo / "peer.txt").write_text("peer unstaged\n")
    before = digest(repo)
    base = git(repo, "rev-parse", "HEAD")
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        '#!/bin/sh\nif [ "$1" = prepared ] && grep -q " refs/heads/main$"; then echo "ref update refused" >&2; exit 1; fi\n'
    )
    hook.chmod(0o755)

    with pytest.raises(merge.MergeError) as refusal:
        land(repo, head)

    hook.unlink()
    after = digest(repo)
    assert git(repo, "rev-parse", "HEAD") == base
    # Attempt the following delivery even when residue is present, so the red
    # control records both the stale index and its practical consequence.
    second_error = None
    try:
        report = land(repo, other)
    except merge.MergeError as exc:
        second_error = str(exc)
    assert after == before, (
        f"refusal: {refusal.value}; second merge refused: {second_error}"
    )
    assert second_error is None
    assert "restored" in str(refusal.value)
    assert report["pushed"] is True
    assert (repo / "source.py").read_text() == "second delivery\n"
    assert (repo / "peer.txt").read_text() == "peer unstaged\n"
    assert git(repo, "show", ":peer.txt") == "peer staged"
