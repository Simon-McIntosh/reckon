"""A presented head speaks for the commits beneath it.

A coordinator reads a worker's log and presents the tip, not every commit the
delivery contains. The shortfall report used to compare the presented list with
the manifest's list as sets, so a complete delivery cited by its head was
reported as short of itself: measured on a promotion presenting the head of a
manifest naming sixteen commits, the report named a commit that was an ancestor
of the one presented. A manifest commit is short only when no presented commit
covers it, and a presented commit covers itself plus every commit in ``base..``
that commit.

Four cases fix the rule, each an outcome a separate-cited list could not give:

* a head covers the commits beneath it, so citing it reports nothing;
* a manifest commit outside ``base..`` the presentation is named, because the
  run did not make it;
* a commit at or below the base is named too, for the same reason;
* two commits on separate branches above the base each cover their own
  ancestors, so presenting both covers the union of their lines.

A presented commit the manifest does not declare stays silent: that is the
coordinator declining the worker's revision, where the presented list is the
one that resolves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import promotion
from tests.helpers.git import git as _git


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "chore: seed")
    return root


def _commit(repository: Path, name: str) -> str:
    """Create one commit and return its canonical revision."""
    path = f"{name}.txt"
    (repository / path).write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", path)
    _git(repository, "commit", "-q", "-m", f"feat: add {name}")
    return _git(repository, "rev-parse", "HEAD")


def _record(
    repository: Path,
    directory: Path,
    *,
    base: str,
    declared: list[str],
) -> dict:
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: fixture\n"
        "status: complete\n"
        f"commits: {', '.join(declared)}\n"
        "tests: done\n",
        encoding="utf-8",
    )
    return {
        "run_id": "r-fixture",
        "worktree": str(repository),
        "base_sha": base,
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "node": {"role": "implement"},
    }


def _shortfall_report(record: dict, *, commits: tuple[str, ...]) -> dict | None:
    return promotion._require_gate_evidence(
        str(record["run_id"]),
        record,
        verdict="passed",
        commits=commits,
        no_commit_reason="",
    )


def test_a_presented_head_covers_the_commits_beneath_it(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    declared = [_commit(repository, name) for name in "abcde"]
    record = _record(repository, tmp_path, base=base, declared=declared)

    # The manifest lists five commits and the promotion presents only the
    # fifth; the other four are its ancestors above the base.
    assert _shortfall_report(record, commits=(declared[-1],)) is None


def test_a_manifest_commit_outside_the_presentation_is_named(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "side", base)
    off_branch = _commit(repository, "side")
    _git(repository, "checkout", "-q", "main")
    first = _commit(repository, "one")
    head = _commit(repository, "two")
    record = _record(
        repository, tmp_path, base=base, declared=[first, off_branch, head]
    )

    report = _shortfall_report(record, commits=(head,))

    assert report is not None
    assert report["missing"] == [off_branch]
    assert off_branch in report["message"]


def test_a_manifest_commit_at_or_below_the_base_is_named(
    repository: Path, tmp_path: Path
) -> None:
    below = _commit(repository, "below")
    base = _commit(repository, "base")
    head = _commit(repository, "head")
    record = _record(repository, tmp_path, base=base, declared=[below, base, head])

    report = _shortfall_report(record, commits=(head,))

    assert report is not None
    assert report["missing"] == sorted([base, below])


def test_two_presented_branches_each_cover_their_own_ancestors(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "left", base)
    left_one = _commit(repository, "left-one")
    left_head = _commit(repository, "left-head")
    _git(repository, "checkout", "-q", "-b", "right", base)
    right_one = _commit(repository, "right-one")
    right_head = _commit(repository, "right-head")
    _git(repository, "checkout", "-q", "main")
    record = _record(
        repository,
        tmp_path,
        base=base,
        declared=[left_one, left_head, right_one, right_head],
    )

    assert _shortfall_report(record, commits=(left_head, right_head)) is None


def test_a_presented_commit_the_manifest_does_not_declare_stays_silent(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "side", base)
    declined = _commit(repository, "declined")
    _git(repository, "checkout", "-q", "main")
    first = _commit(repository, "one")
    second = _commit(repository, "two")
    record = _record(repository, tmp_path, base=base, declared=[first, second])

    # The coordinator declined the worker's revisions and cited its own; the
    # presented list is the one that resolves, so it is not reported.
    assert _shortfall_report(record, commits=(declined,)) is None
