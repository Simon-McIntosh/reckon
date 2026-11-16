"""Run-directory deliverables promote without a repository commit."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.recovery_review_acceptance import promote_review_run_itself
from reckon.crew.runs import pointer_path, read_pointer, run_dir
from tests import test_a_review_run_promotes_itself as review_fixture
from tests.helpers.git import git

repository = review_fixture.repository


def _delivered_review(repository: Path, tmp_path: Path) -> Path:
    stored = review_fixture._store_review_record()
    directory = run_dir(review_fixture.REVIEW_RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / "review.json", directory / "review-copy.json"]
    for path in paths:
        path.write_text(stored.read_text(), encoding="utf-8")
        assert path.is_relative_to(directory)
        assert not path.is_relative_to(repository)
    review_fixture._write_review_pointer(
        repository, tmp_path, worker_pid=None, record_path=stored
    )
    review_fixture._write_exit_record()
    manifest = Path(read_pointer(review_fixture.REVIEW_RUN_ID)["manifest_path"])
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(
            "commits: none\n"
            "changed_paths:\n"
            + "".join(f"  - {path}\n" for path in paths)
            + "tests: /usr/bin/true\n"
        )
    assert git(repository, "status", "--porcelain") == ""
    return manifest


@pytest.mark.parametrize("promotion", ["complete", "review-self"])
def test_a_review_manifest_naming_only_run_files_promotes(
    repository: Path, tmp_path: Path, promotion: str
) -> None:
    _delivered_review(repository, tmp_path)
    if promotion == "complete":
        result = crew.complete(
            review_fixture.REVIEW_RUN_ID, gate="not-run", root=repository
        )
        assert result["record"]["commits"] == []
    else:
        result = promote_review_run_itself(read_pointer(review_fixture.REVIEW_RUN_ID))
        assert result["promoted"], result
        assert result["promoted_by"] == "review-self"
    assert not pointer_path(review_fixture.REVIEW_RUN_ID).exists()
    [row] = ledger.runs(review_fixture.PROJECT, root=repository)
    assert row["run_id"] == review_fixture.REVIEW_RUN_ID
    assert row["commits"] == []


def test_a_review_manifest_naming_a_repository_file_still_needs_a_commit(
    repository: Path, tmp_path: Path
) -> None:
    manifest = _delivered_review(repository, tmp_path)
    text = manifest.read_text()
    start = text.index("changed_paths:")
    end = text.index("tests:", start)
    manifest.write_text(text[:start] + "changed_paths: seed.txt\n" + text[end:])
    with pytest.raises(crew.CrewError, match=r"commits.*missing") as refused:
        crew.complete(review_fixture.REVIEW_RUN_ID, gate="not-run", root=repository)
    assert "repository paths" in str(refused.value)
    assert pointer_path(review_fixture.REVIEW_RUN_ID).is_file()
    assert ledger.runs(review_fixture.PROJECT, root=repository) == []
