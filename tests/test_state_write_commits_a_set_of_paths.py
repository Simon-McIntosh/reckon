"""A state write commits a set of paths in one commit, or exactly one.

A review run's promotion commits its ledger row and its review record
together, so the record lands in the commit the promotion already makes. These
tests drive ``_commit_state_write`` with two paths in a throwaway repository
and assert both land in one commit, that a single-path call commits exactly as
before, and that a set which cannot be one commit leaves nothing staged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import ledger


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root: Path) -> Path:
    """A throwaway checkout carrying two tracked state files."""
    repo = root / "checkout"
    record_dir = repo / "docs" / "state" / "proj" / "reviews" / "run" / "r1"
    record_dir.mkdir(parents=True)
    (repo / "docs" / "state" / "proj" / "crew.json").write_text("{}\n")
    (record_dir / "x.json").write_text("{}\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(repo, *args)
    return repo


def _head_blob(repo: Path, relative: str) -> str:
    return _git(repo, "show", f"HEAD:{relative}")


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def test_two_paths_land_in_one_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ledger_row = repo / "docs" / "state" / "proj" / "crew.json"
    record = repo / "docs" / "state" / "proj" / "reviews" / "run" / "r1" / "x.json"
    ledger_row.write_text('{"_version": 1}\n')
    record.write_text('{"id": "r1", "findings": []}\n')

    before = _commit_count(repo)
    ledger._commit_state_write(
        "proj",
        "chore(reviews): record r1",
        "Commit the review record and its ledger row together.",
        [ledger_row, record],
    )
    after = _commit_count(repo)

    assert after == before + 1
    assert _head_blob(repo, "docs/state/proj/crew.json") == '{"_version": 1}'
    assert (
        _head_blob(repo, "docs/state/proj/reviews/run/r1/x.json")
        == '{"id": "r1", "findings": []}'
    )
    assert _git(repo, "status", "--porcelain") == ""


def test_single_path_commits_as_before(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ledger_row = repo / "docs" / "state" / "proj" / "crew.json"
    ledger_row.write_text('{"_version": 2}\n')

    before = _commit_count(repo)
    ledger._commit_state_write(
        "proj",
        "chore(roster): update",
        "Commit the single state file immediately.",
        ledger_row,
    )
    assert _commit_count(repo) == before + 1
    assert _head_blob(repo, "docs/state/proj/crew.json") == '{"_version": 2}'
    assert _git(repo, "status", "--porcelain") == ""


def test_refused_set_leaves_nothing_staged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ledger_row = repo / "docs" / "state" / "proj" / "crew.json"
    ledger_row.write_text('{"_version": 3}\n')
    # A second checkout, so the pair cannot share one commit.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    outside = other / "record.json"
    outside.write_text("{}\n")

    before = _commit_count(repo)
    with pytest.raises(ledger.LedgerError):
        ledger._commit_state_write(
            "proj",
            "chore(reviews): record",
            "Refused: the pair does not share a checkout.",
            [ledger_row, outside],
        )

    assert _commit_count(repo) == before
    # Refused before staging: the write is still a working-tree modification,
    # and the index is empty.
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert _git(repo, "status", "--porcelain").strip() == "M docs/state/proj/crew.json"
