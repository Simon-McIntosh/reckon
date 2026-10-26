"""One resolver answers every commit citation.

The commit-citation check reads a manifest's ``commits`` field and asks git
whether each cited id names a commit object. Two definitions of that predicate
once existed, one in ``reckon.crew.promotion_evidence`` and one in
``reckon.crew.reports``; they agreed by measurement, which is what made a
second copy free to drift unnoticed. These tests hold the single definition:
both modules hand out the same function object, and that one function both
accepts a commit the fixture repository holds and refuses ids it does not.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

promotion_evidence = importlib.import_module("reckon.crew.promotion_evidence")
reports = importlib.import_module("reckon.crew.reports")

_FIXTURE_EMAIL = "worker@example.invalid"
_FIXTURE_NAME = "Worker"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _fixture_repository(tmp_path: Path) -> tuple[Path, str]:
    """A repository holding one commit, and that commit's full id."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", _FIXTURE_EMAIL)
    _git(repo, "config", "user.name", _FIXTURE_NAME)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    return repo, _git(repo, "rev-parse", "HEAD")


def _citation_findings(repo: Path, entry: str) -> list[str]:
    return reports._commit_citation_findings(
        {"commits": [entry]}, worktree=repo, repository=repo
    )


def test_reports_and_promotion_evidence_expose_one_reader() -> None:
    assert reports._commit_resolves_in is promotion_evidence._commit_resolves_in


def test_the_one_reader_accepts_the_commits_own_id(tmp_path: Path) -> None:
    repo, sha = _fixture_repository(tmp_path)

    assert reports._commit_resolves_in(repo, sha) is True
    assert _citation_findings(repo, sha) == []


def test_the_one_reader_refuses_an_all_zero_hash(tmp_path: Path) -> None:
    repo, _ = _fixture_repository(tmp_path)
    all_zero = "0" * 40

    assert reports._commit_resolves_in(repo, all_zero) is False
    findings = _citation_findings(repo, all_zero)
    assert len(findings) == 1
    assert all_zero in findings[0]


def test_the_one_reader_refuses_a_hash_naming_no_object(tmp_path: Path) -> None:
    repo, sha = _fixture_repository(tmp_path)
    nowhere = sha[:30] + ("0" if sha[30] != "0" else "1")
    assert nowhere != sha

    assert reports._commit_resolves_in(repo, nowhere) is False
    findings = _citation_findings(repo, nowhere)
    assert len(findings) == 1
    assert nowhere in findings[0]
