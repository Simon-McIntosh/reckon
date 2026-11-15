"""A conflict check leaves no object in the repository it checks.

``git merge-tree --write-tree`` answers whether a head merges cleanly by
writing the merged tree. A landing pass asks that question on every candidate,
every pass, and a pass that lands nothing committed nothing — so the tree it
wrote was never reachable and the store grew by it. These tests measure the
candidate's object store itself rather than a report, because the leak is
invisible to every return value: a pass that refused the candidate reads
exactly like a pass that left the repository untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import merge
from tests.helpers.git import git

MESSAGE = "merge: integrate the reviewed delivery"
BODY = "Combine the reviewed delivery with the primary branch."


def _objects(repository: Path) -> set[str]:
    """Every object the repository holds, reachable from a ref or not."""
    listed = git(
        repository, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"
    )
    return {line.strip() for line in listed.splitlines() if line.strip()}


def _landing(repository: Path, head: str) -> None:
    """Run one landing pass over ``head`` and expect the candidate refused."""
    with pytest.raises(merge.MergeConflictError):
        merge.land_merge(
            repository, head=head, message=MESSAGE, body=BODY, release=False
        )


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A repository with a bare origin and two files to change."""
    checkout = tmp_path / "repository"
    checkout.mkdir()
    git(checkout, "init", "-q", "-b", "main")
    git(checkout, "config", "user.name", "Test")
    git(checkout, "config", "user.email", "test@example.invalid")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    git(checkout, "remote", "add", "origin", str(origin))
    (checkout / "core.txt").write_text("base\n")
    (checkout / "notes.txt").write_text("notes\n")
    git(checkout, "add", "core.txt", "notes.txt")
    git(checkout, "commit", "-qm", "test: seed the repository")
    git(checkout, "push", "-q", "origin", "main")
    return checkout


def _delivered_head(repository: Path, tmp_path: Path, contents: str) -> str:
    """Commit ``contents`` of ``core.txt`` on a detached head of the checkout."""
    worker = tmp_path / "worker"
    git(repository, "worktree", "add", "--detach", str(worker), "HEAD")
    (worker / "core.txt").write_text(contents)
    git(worker, "add", "core.txt")
    git(worker, "commit", "-qm", "test: deliver a change")
    return git(worker, "rev-parse", "HEAD")


def _advance_primary(repository: Path, cycle: int) -> str:
    """Write a commit onto the primary branch that leaves the conflict alone."""
    (repository / "notes.txt").write_text(f"notes {cycle}\n")
    git(repository, "add", "notes.txt")
    git(repository, "commit", "-qm", f"test: advance the notes {cycle}")
    return git(repository, "rev-parse", "HEAD")


def test_a_pass_that_lands_nothing_leaves_the_object_set_unchanged(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pass reaches the conflict check, and no object remains after them."""
    head = _delivered_head(repository, tmp_path, "delivered\n")
    (repository / "core.txt").write_text("primary\n")
    git(repository, "add", "core.txt")
    git(repository, "commit", "-qm", "test: move the primary onto the same file")

    reached = 0
    checked = merge._merge_tree

    def counting_check(*args, **kwargs):
        nonlocal reached
        reached += 1
        return checked(*args, **kwargs)

    monkeypatch.setattr(merge, "_merge_tree", counting_check)

    before = _objects(repository)
    fixture_objects: set[str] = set()
    passes = 3
    for cycle in range(passes):
        if cycle:
            previous = _objects(repository)
            _advance_primary(repository, cycle)
            fixture_objects |= _objects(repository) - previous
        _landing(repository, head)

    assert reached == passes, "a pass never reached the conflict check"
    assert _objects(repository) == before | fixture_objects, (
        "the passes left objects beyond the ones the fixture itself wrote"
    )


def test_a_landed_merge_writes_its_objects(repository: Path, tmp_path: Path) -> None:
    """A merge that lands brings the objects it needs into the repository."""
    head = _delivered_head(repository, tmp_path, "delivered\n")
    before = _objects(repository)

    report = merge.land_merge(
        repository, head=head, message=MESSAGE, body=BODY, release=False
    )

    assert report["pushed"] is True
    landed = report["commit"]
    assert git(repository, "rev-parse", "HEAD") == landed
    merged_tree = git(repository, "rev-parse", f"{landed}^1^{{tree}}")
    # ``cat-file -e`` exits non-zero and the helper raises when it is absent,
    # so reaching the next line is the receipt that the object is there.
    git(repository, "cat-file", "-e", f"{landed}^{{commit}}")
    git(repository, "cat-file", "-e", merged_tree)
    git(repository, "cat-file", "-e", f"{merged_tree}:core.txt")
    assert _objects(repository) > before, "the merge wrote nothing into the store"
