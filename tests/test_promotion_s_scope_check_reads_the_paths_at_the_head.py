"""Promotion charges only cited paths whose change survives at the run's head."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion_checks import _landing_scope_products
from reckon.crew.promotion_evidence import _committed_scope

DECLARED = "docs/evidence/archive/result.html"
UNDECLARED = "docs/evidence/archive/draft.html"
IMPORTED = "docs/evidence/archive/imported.html"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit(repo: Path, *paths: str) -> str:
    _git(repo, "add", "--", *paths)
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "fix: update evidence",
        "-m",
        "Record the content delivered by this change.",
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def run(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _write(repo, "seed.txt", "seed\n")
    _write(repo, UNDECLARED, "unchanged\n")
    base = _commit(repo, "seed.txt", UNDECLARED)
    return {
        "run_id": "r-scope",
        "project": "sample",
        "role": "implement",
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base,
        "node": {"id": "scope-at-head", "write_paths": [DECLARED]},
    }


def _promote_scope(run: dict, commits: list[str]) -> dict:
    return _landing_scope_products(
        run["run_id"],
        run,
        shadow=False,
        node=run["node"],
        commits=commits,
        accepted_paths=None,
    )


def test_a_file_created_outside_scope_and_renamed_into_scope_is_admitted(run):
    repo = Path(run["worktree"])
    temporary = "docs/evidence/archive/temporary.html"
    _write(repo, temporary, "delivered\n")
    created = _commit(repo, temporary)
    (repo / temporary).rename(repo / DECLARED)
    renamed = _commit(repo, temporary, DECLARED)
    assert not (repo / temporary).exists()
    assert (repo / DECLARED).read_text() == "delivered\n"
    assert temporary in _committed_scope(cwd=repo, commits=[created, renamed]).paths

    assert _promote_scope(run, [created, renamed])["scope_acceptances"] == []


def test_an_undeclared_change_restored_before_the_head_is_admitted(run):
    repo = Path(run["worktree"])
    _write(repo, UNDECLARED, "intermediate\n")
    changed = _commit(repo, UNDECLARED)
    _write(repo, UNDECLARED, "unchanged\n")
    _write(repo, DECLARED, "delivered\n")
    restored = _commit(repo, UNDECLARED, DECLARED)
    assert _git(repo, "diff", run["base_sha"], restored, "--", UNDECLARED) == ""
    assert UNDECLARED in _committed_scope(cwd=repo, commits=[changed, restored]).paths

    assert _promote_scope(run, [changed, restored])["scope_acceptances"] == []


def test_an_undeclared_change_left_at_the_head_is_refused(run):
    repo = Path(run["worktree"])
    _write(repo, UNDECLARED, "still changed\n")
    changed = _commit(repo, UNDECLARED)

    with pytest.raises(CrewError, match="outside its declared write scope") as error:
        _promote_scope(run, [changed])

    assert UNDECLARED in str(error.value)


def test_an_undeclared_deletion_left_at_the_head_is_refused(run):
    repo = Path(run["worktree"])
    (repo / UNDECLARED).unlink()
    deleted = _commit(repo, UNDECLARED)

    with pytest.raises(CrewError, match="outside its declared write scope") as error:
        _promote_scope(run, [deleted])

    assert UNDECLARED in str(error.value)


def test_a_cited_merge_does_not_charge_the_imported_branch_paths(run):
    repo = Path(run["worktree"])
    peer = repo.parent / "peer"
    _git(repo, "worktree", "add", "-q", "--detach", str(peer), run["base_sha"])
    try:
        _write(peer, IMPORTED, "from another branch\n")
        imported = _commit(peer, IMPORTED)
        _write(repo, DECLARED, "delivered\n")
        own = _commit(repo, DECLARED)
        _git(
            repo,
            "merge",
            "--no-ff",
            imported,
            "-m",
            "fix: integrate evidence",
            "-m",
            "Combine independent evidence changes.",
        )
        merged = _git(repo, "rev-parse", "HEAD")
        assert IMPORTED in _git(repo, "diff", "--name-only", run["base_sha"], merged)
        assert _committed_scope(cwd=repo, commits=[own, merged]).paths == (DECLARED,)

        assert _promote_scope(run, [own, merged])["scope_acceptances"] == []
    finally:
        _git(repo, "worktree", "remove", str(peer))
