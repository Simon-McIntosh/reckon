"""A promotion leaves unrelated staged changes in the index."""

from pathlib import Path

from reckon import ledger
from tests import test_crew_promotion as promotion_tests
from tests.helpers.git import git
from tests.test_crew_promotion import (
    PLAN,
    PROJECT,
    _promote,
    _promotion_commit,
)

repository = promotion_tests.repository


def test_promotion_commit_preserves_another_runs_staged_deletion(
    repository: Path,
) -> None:
    other_run = "r-20260914T190320000000-other"
    other_file = ledger.run_path(PROJECT, other_run, repository)
    other_file.parent.mkdir(parents=True, exist_ok=True)
    other_file.write_text('{"run_id": "other"}\n', encoding="utf-8")
    relative = other_file.relative_to(repository).as_posix()
    git(repository, "add", "--", relative)
    git(repository, "commit", "-q", "-m", "test: seed another run file")

    other_file.unlink()
    git(repository, "add", "--", relative)
    assert git(repository, "diff", "--cached", "--name-status", "--", relative) == (
        f"D\t{relative}"
    )

    run_id = "r-20260914T190323000000-scoped"
    _promote(repository, run_id, outcome="the landing includes only its own paths")
    landing = _promotion_commit(repository, run_id)
    assert set(
        git(repository, "show", "--format=", "--name-only", landing).split()
    ) == {
        f"docs/state/{PROJECT}/runs/{run_id}.json",
        f"docs/plans/{PLAN}.html",
    }
    assert git(repository, "show", f"HEAD:{relative}") == '{"run_id": "other"}'
    assert git(repository, "diff", "--cached", "--name-status", "--", relative) == (
        f"D\t{relative}"
    )
