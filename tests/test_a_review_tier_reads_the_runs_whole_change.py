"""A run's review tier is measured over everything it changed, not what it cites.

The tier decides whether a run owes a review before it lands, and promotion,
the review reflex and the obligations view all read it through one resolver.
That resolver measured only the commits a manifest or a coordinator cited, so a
run that rewrote runtime source and cited only its docs-only evidence commit
resolved to tier none and was offered for promotion with no review at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from reckon import review_tiers
from reckon.crew import promotion_checks


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_tree(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A base, then a runtime-source commit, then a docs-only commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    base = _git(repo, "rev-parse", "HEAD")
    package = repo / "reckon" / "crew"
    package.mkdir(parents=True)
    (package / "logic.py").write_text(
        "".join(f"VALUE_{n} = {n}\n" for n in range(40)), encoding="utf-8"
    )
    _git(repo, "add", "reckon")
    _git(repo, "commit", "-q", "-m", "fix: change runtime behaviour")
    code = _git(repo, "rev-parse", "HEAD")
    evidence = repo / "docs" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "record.html").write_text("<p>evidence</p>\n", encoding="utf-8")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "docs(evidence): record it")
    docs = _git(repo, "rev-parse", "HEAD")
    return repo, base, code, docs


def _record(repo: Path, base: str) -> dict:
    return {
        "run_id": "r-whole-change",
        "project": "sample",
        "worktree": str(repo),
        "base_sha": base,
        "node": {"spec_level": "guided"},
    }


def test_citing_only_the_docs_commit_still_owes_the_change_its_review(
    tmp_path: Path,
) -> None:
    repo, base, _code, docs = _run_tree(tmp_path)
    tier = promotion_checks._run_review_tier(
        "r-whole-change", _record(repo, base), commit_list=[docs], root=None
    )
    assert tier != review_tiers.NONE


def test_a_run_whose_whole_change_is_docs_only_still_owes_none(
    tmp_path: Path,
) -> None:
    repo, _base, code, docs = _run_tree(tmp_path)
    # Based on the code commit, the run's whole change is the docs commit alone.
    tier = promotion_checks._run_review_tier(
        "r-whole-change", _record(repo, code), commit_list=[docs], root=None
    )
    assert tier == review_tiers.NONE
