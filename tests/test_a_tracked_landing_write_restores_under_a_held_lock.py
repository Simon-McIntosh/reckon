"""A tracked landing write is restored while a peer holds the index lock.

A refused landing rolls its own store writes back. The whole-path restore is a
single index-writing git call, so a lock another process holds refuses it — and
that lock is exactly what caused the refusal in the first place. The fallback
then settles each path: a tracked file is rewritten in the working tree from
HEAD's blob, which takes no index lock, so it returns to its committed content
instead of leaving the landing write for the next promotion to meet as an edit
the run never made.

These cases hold a real ``.git/index.lock``, so the refusals come from git
state rather than a stubbed exit code. They drive the landing commit itself, so
the refusal under test is the one a coordinator reads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion, promotion_scope
from reckon.crew.runs import run_dir


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=check
    )


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A temporary repository whose crew home resolves inside the test tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    plan = root / "plan.html"
    plan.write_text("committed plan\n", encoding="utf-8")
    _git(root, "add", "--", str(plan))
    _git(root, "commit", "-qm", "test: seed", "-m", "Seed the tracked plan.")
    assert run_dir("tracked-restore").is_relative_to(tmp_path)
    return root, plan


def _hold_index_lock(root: Path) -> Path:
    lock = root / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    return lock


def test_a_tracked_landing_write_is_restored_under_a_held_lock(repository):
    """The plan file returns to HEAD's content while the index lock is held.

    The lock refuses the landing's staging, so the rollback runs. Its
    index-dependent restore is refused too, and the fallback rewrites the plan
    file from HEAD's blob; the rollback then reports the path restored and the
    working copy carries HEAD's content again.
    """
    root, plan = repository
    head = _git(root, "show", "HEAD:plan.html").stdout
    plan.write_text("landing narrative the refusal left behind\n", encoding="utf-8")
    lock = _hold_index_lock(root)
    try:
        with pytest.raises(promotion.CrewError) as caught:
            promotion._commit_landing_writes(
                run_id="tracked-restore",
                verdict="passed",
                checkout=root,
                paths=[plan],
            )
    finally:
        lock.unlink()

    message = str(caught.value)
    report = getattr(caught.value, promotion.LANDING_ROLLBACK_ATTRIBUTE)
    assert "could not stage the landing writes" in message
    assert report[str(plan)] is True, "the rollback must report the plan file restored"
    assert plan.read_text(encoding="utf-8") == head, (
        "a restored landing write must carry HEAD's content, not the landing write"
    )


def test_an_unreadable_blob_names_the_plan_file_as_unrestored(repository, monkeypatch):
    """When HEAD's blob cannot be read, the refusal names the file unrestored.

    The blob read is made to fail, as it would for an object git cannot open.
    The path keeps its landing write, the rollback reports it unrestored, and
    the refusal the coordinator reads names the file — so a coordinator sees
    which file to restore before re-promoting.
    """
    root, plan = repository
    plan.write_text("landing narrative the refusal left behind\n", encoding="utf-8")
    monkeypatch.setattr(promotion_scope, "_git_blob", lambda *args, **kwargs: None)
    lock = _hold_index_lock(root)
    try:
        with pytest.raises(promotion.CrewError) as caught:
            promotion._commit_landing_writes(
                run_id="unreadable-blob",
                verdict="passed",
                checkout=root,
                paths=[plan],
            )
    finally:
        lock.unlink()

    message = str(caught.value)
    report = getattr(caught.value, promotion.LANDING_ROLLBACK_ATTRIBUTE)
    assert report[str(plan)] is False, "an unrestored path is not a restored one"
    assert plan.read_text(encoding="utf-8").startswith("landing narrative"), (
        "an unrestored landing write stays in the working tree"
    )
    assert str(plan) in message, "the refusal must name the unrestored file"
    assert "could not be restored" in message
