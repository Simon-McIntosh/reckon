"""Read-only declarations and malformed path items keep their own meanings."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.promotion_evidence import _commit_resolves_in
from reckon.crew.recovery_review_acceptance import promote_review_run_itself
from reckon.crew.reports import audit_manifest, parse_manifest
from reckon.crew.review import review_path
from reckon.crew.runs import pointer_path, read_pointer, run_dir
from tests import test_a_review_run_promotes_itself as review_fixture
from tests.helpers.git import git

repository = review_fixture.repository

COMMITS_DECLARATION = (
    "none — a read-only run has no commit to cite (the deliverable is the "
    "stored review record under review-records/, not a git change)"
)


def _delivered_review(repository: Path, tmp_path: Path) -> Path:
    """A stopped review with both stored records outside its clean repository."""
    stored = review_fixture._store_review_record()
    directory = run_dir(review_fixture.REVIEWED_RUN_ID) / "review-records"
    directory.mkdir(parents=True, exist_ok=True)
    keyed = directory / stored.name
    keyed.write_text(stored.read_text(), encoding="utf-8")
    sibling = (
        directory
        / review_path(review_fixture.PROJECT, review_fixture.REVIEWED_RUN_ID).name
    )
    sibling.write_text(stored.read_text(), encoding="utf-8")
    review_fixture._write_review_pointer(
        repository, tmp_path, worker_pid=None, record_path=keyed
    )
    review_fixture._write_exit_record()
    manifest = Path(read_pointer(review_fixture.REVIEW_RUN_ID)["manifest_path"])
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(
            f"commits: {COMMITS_DECLARATION}\n"
            f"changed_paths:\n  - {keyed}\n  - {sibling}\n"
            "tests: /usr/bin/true\n"
        )
    assert keyed.is_file() and sibling.is_file()
    assert not keyed.is_relative_to(repository)
    assert not sibling.is_relative_to(repository)
    assert git(repository, "status", "--porcelain") == ""
    return manifest


@pytest.mark.parametrize("promotion", ["no-commit", "review-self"])
def test_a_whole_absence_reason_promotes_with_two_run_directory_paths(
    repository: Path, tmp_path: Path, promotion: str
) -> None:
    manifest = _delivered_review(repository, tmp_path)
    audit = audit_manifest(manifest.read_text(), repository=repository)
    assert audit["ok"], audit["findings"]

    if promotion == "no-commit":
        result = crew.complete(
            review_fixture.REVIEW_RUN_ID,
            gate="not-run",
            no_commit="the stored review is outside the repository",
            root=repository,
        )
        assert result["record"]["commits"] == []
    else:
        result = promote_review_run_itself(read_pointer(review_fixture.REVIEW_RUN_ID))
        assert result["promoted"], result
        assert result["promoted_by"] == "review-self"

    assert not pointer_path(review_fixture.REVIEW_RUN_ID).exists()
    [row] = ledger.runs(review_fixture.PROJECT, root=repository)
    assert row["commits"] == []
    if promotion == "review-self":
        assert row["promoted_by"] == "review-self"


@pytest.mark.parametrize("item", ["2>/dev/null", ">output.log", "2>&1", "<input.txt"])
def test_a_shell_redirection_is_refused_as_a_changed_path(
    repository: Path, tmp_path: Path, item: str
) -> None:
    manifest = _delivered_review(repository, tmp_path)
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(f"  - {item}\n")
    # Keep the item in changed_paths rather than appending it to the tests field.
    text = manifest.read_text().replace(
        f"tests: /usr/bin/true\n  - {item}\n",
        f"  - {item}\ntests: /usr/bin/true\n",
    )
    manifest.write_text(text, encoding="utf-8")

    with pytest.raises(crew.CrewError) as refused:
        crew.complete(review_fixture.REVIEW_RUN_ID, gate="not-run", root=repository)
    refusal = str(refused.value)
    assert item in refusal, refusal
    assert "changed_paths" in refusal, refusal
    assert "remove" in refusal.lower() or "replace" in refusal.lower(), refusal
    assert "missing" not in refusal.lower(), refusal
    audit = audit_manifest(text, repository=repository)
    assert not audit["ok"], audit
    finding = " ".join(audit["findings"])
    assert item in finding
    assert "changed_paths" in finding
    assert "remove" in finding.lower() or "replace" in finding.lower()
    assert "missing" not in finding.lower()

    assert pointer_path(review_fixture.REVIEW_RUN_ID).exists()
    assert ledger.runs(review_fixture.PROJECT, root=repository) == []


def test_an_unresolvable_citation_after_a_real_one_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    manifest = _delivered_review(repository, tmp_path)
    real = git(repository, "rev-parse", "HEAD")
    absent = "f" * 40
    assert git(repository, "cat-file", "-t", real) == "commit"
    assert not _commit_resolves_in(repository, absent)
    manifest.write_text(
        manifest.read_text().replace(COMMITS_DECLARATION, f"{real}, {absent}"),
        encoding="utf-8",
    )
    parsed = parse_manifest(manifest.read_text())
    assert parsed["commits"] == [real, absent]
    audit = audit_manifest(manifest.read_text(), repository=repository)
    assert not audit["ok"]
    assert absent in " ".join(audit["findings"])

    with pytest.raises(crew.CrewError, match=absent) as refused:
        crew.complete(
            review_fixture.REVIEW_RUN_ID,
            gate="not-run",
            commits=(real, absent),
            root=repository,
        )
    assert "does not resolve" in str(refused.value), refused.value
    assert pointer_path(review_fixture.REVIEW_RUN_ID).exists()
    assert ledger.runs(review_fixture.PROJECT, root=repository) == []


def test_a_redirection_inside_a_real_filename_is_a_path() -> None:
    manifest = parse_manifest(
        "status: complete\ncommits: none\nchanged_paths: logs/input>output.txt\n"
    )
    assert manifest["changed_paths"] == ["logs/input>output.txt"]
