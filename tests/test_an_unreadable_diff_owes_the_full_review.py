"""A cited-commit scope whose cumulative diff cannot be read is unmeasured.

The review tier resolver reads an empty path list as a run that touched no
runtime source, so a scope reader that reports an unread diff as a measured
empty list hands the resolver a confident wrong answer: the run promotes with
no review owed. The repository's first commit is the shape that triggers this
— it has no first parent, so its diff cannot be read — and a missing object or
a transient git error produces the same absence.

The scope reader therefore reports such a scope as unmeasured, which the
resolver reads as the fuller review. A scope that was read keeps the tier the
resolver already gives it, including a read diff that changes only non-runtime
paths, which still resolves the lesser tier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import review_tiers
from reckon.crew import promotion_checks
from tests.helpers.git import git

RUN_ID = "r-20261019T230000000001-unreadable-diff"


@pytest.fixture()
def crew_home_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary configuration home, so no host plan or flight layer is read."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _record(tree: Path) -> dict[str, Any]:
    """The run's record: it names its own tree and a fixing specification level."""
    return {"worktree": str(tree), "repo": str(tree), "node": {"spec_level": "exact"}}


@pytest.fixture()
def run_repository(
    tmp_path: Path, crew_home_config: Path
) -> tuple[Path, str, str, str]:
    """A repository whose root commit changes runtime source.

    The root commit has no first parent, so its diff cannot be read. The two
    later commits read cleanly: one changes only a non-runtime path, the other
    changes a file under ``reckon/``.
    """
    tree = tmp_path / "run-tree"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "config", "user.email", "worker@example.invalid")
    git(tree, "config", "user.name", "Worker")
    (tree / "reckon").mkdir()
    (tree / "reckon" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    git(tree, "add", "reckon/mod.py")
    git(tree, "commit", "-q", "-m", "root changes runtime source")
    root = git(tree, "rev-parse", "HEAD")
    (tree / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(tree, "add", "seed.txt")
    git(tree, "commit", "-q", "-m", "changes only a non-runtime path")
    seed_only = git(tree, "rev-parse", "HEAD")
    (tree / "reckon" / "mod.py").write_text("value = 1\nother = 2\n", encoding="utf-8")
    git(tree, "add", "reckon/mod.py")
    git(tree, "commit", "-q", "-m", "changes runtime source")
    runtime = git(tree, "rev-parse", "HEAD")
    return tree, root, seed_only, runtime


def test_an_unavailable_scope_is_reported_unmeasured(
    run_repository: tuple[Path, str, str, str],
) -> None:
    """The root commit's unread diff is an absence, not a measured empty list."""
    tree, root, _seed_only, _runtime = run_repository

    paths, lines, measured = promotion_checks._review_changed_scope(
        RUN_ID, _record(tree), [root]
    )

    assert measured is False
    assert lines is None
    assert paths == ()


def test_a_root_commit_scope_resolves_the_full_tier(
    run_repository: tuple[Path, str, str, str],
) -> None:
    """A run whose cited diff cannot be read owes the fuller review."""
    tree, root, _seed_only, _runtime = run_repository

    tier = promotion_checks._run_review_tier(
        RUN_ID, _record(tree), commit_list=[root], root=tree
    )

    assert tier == review_tiers.FULL


def test_a_read_non_runtime_scope_resolves_none(
    run_repository: tuple[Path, str, str, str],
) -> None:
    """A read diff that changes only non-runtime paths still owes no review."""
    tree, _root, seed_only, _runtime = run_repository

    tier = promotion_checks._run_review_tier(
        RUN_ID, _record(tree), commit_list=[seed_only], root=tree
    )

    assert tier == review_tiers.NONE


def test_a_read_runtime_scope_resolves_the_tier_it_resolves_today(
    run_repository: tuple[Path, str, str, str],
) -> None:
    """A read diff of runtime source keeps the tier it resolves today."""
    tree, _root, _seed_only, runtime = run_repository

    tier = promotion_checks._run_review_tier(
        RUN_ID, _record(tree), commit_list=[runtime], root=tree
    )

    assert tier == review_tiers.LIGHT
