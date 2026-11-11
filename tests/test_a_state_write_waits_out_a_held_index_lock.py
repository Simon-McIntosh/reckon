"""A state write waits out a held index.lock, and refuses when it outlasts the wait.

A peer committing in the same checkout holds ``.git/index.lock`` for a moment,
which refuses both the add and the commit a state write makes. These cases drive
``_commit_state_write`` in a throwaway repository: a lock released between
attempts lets the write commit and the retry is counted, a lock held past the
wait's bound raises naming the lock, and a commit that fails with no lock behind
it raises at once without waiting.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import merge


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root: Path) -> tuple[Path, Path, int]:
    """A throwaway checkout with one tracked state file; returns its commit count."""
    repo = root / "checkout"
    state = repo / "docs" / "state" / "proj"
    state.mkdir(parents=True)
    row = state / "crew.json"
    row.write_text("{}\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(repo, *args)
    return repo, row, int(_git(repo, "rev-list", "--count", "HEAD"))


def _lock(repo: Path) -> Path:
    return repo / ".git" / "index.lock"


def test_write_waits_out_a_lock_released_between_attempts(tmp_path: Path) -> None:
    repo, row, base_commits = _repo(tmp_path)
    row.write_text('{"_version": 1}\n')
    lock = _lock(repo)
    lock.write_text("")
    waits: list[float] = []

    def release(_seconds: float) -> None:
        # The peer that held the lock releases it after the first attempt.
        waits.append(_seconds)
        lock.unlink()

    retries = ledger._commit_state_write(
        "proj",
        "chore: write",
        "Commit the state write after the lock is released.",
        row,
        sleep=release,
    )

    assert retries == 1, "the retry the write made is counted"
    assert len(waits) == 1
    assert not lock.exists()
    assert _git(repo, "rev-list", "--count", "HEAD") == str(base_commits + 1)
    assert _git(repo, "show", "HEAD:docs/state/proj/crew.json") == '{"_version": 1}'
    assert _git(repo, "status", "--porcelain") == ""


def test_lock_held_past_the_bound_raises_naming_the_lock(tmp_path: Path) -> None:
    repo, row, base_commits = _repo(tmp_path)
    row.write_text('{"_version": 2}\n')
    lock = _lock(repo)
    lock.write_text("")
    waits: list[float] = []

    with pytest.raises(ledger.LedgerError, match=r"index\.lock"):
        ledger._commit_state_write(
            "proj",
            "chore: write",
            "Refused while the lock stays held.",
            row,
            sleep=waits.append,
        )

    assert len(waits) == merge.DEFAULT_ATTEMPTS - 1
    assert lock.exists()
    assert _git(repo, "rev-list", "--count", "HEAD") == str(base_commits)


def test_commit_failing_without_a_lock_raises_at_once(tmp_path: Path) -> None:
    repo, row, base_commits = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'refused by the hook' >&2\nexit 1\n")
    hook.chmod(0o755)
    row.write_text('{"_version": 3}\n')
    waits: list[float] = []

    with pytest.raises(ledger.LedgerError, match="refused by the hook"):
        ledger._commit_state_write(
            "proj",
            "chore: write",
            "Refused by the hook, with no lock behind it.",
            row,
            sleep=waits.append,
        )

    assert waits == [], "a failure with no lock behind it is not retried"
    assert _git(repo, "rev-list", "--count", "HEAD") == str(base_commits)
