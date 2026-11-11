"""A landing leaves no stale copy of the paths it moved staged in the index.

Landings and promotions write the same shared checkout, and git's index is one
file they all take in turn. A landing moves the branch with a fast-forward; a
peer that read the tree before the merge and writes the index after it leaves
the index holding the pre-landing copy of a path the landing moved. The landing
returns successfully, and the next plain ``git commit`` from any session
commits that index and silently reverts the landed work.

The race is placed where git guarantees it can happen: a ``post-merge`` hook
runs after the fast-forward and before the landing returns, and the hooks here
stage the pre-landing copies there. Every repository, run directory and crew
home is created under a temporary directory.

The declared mutation removes the reconciliation, which leaves the staged stale
copy in the index and lets the following commit revert a landed path;
``RECKON_MERGE_INDEX_RECONCILIATION=removed`` applies it under pytest, and the
assertions on the index and on the following commit are the red log the
manifest names.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import merge
from reckon.crew.promotion_scope import _commit_landing_writes

DECLARED_MUTATION = (
    "with the index reconciliation removed, a staged stale copy of a landed "
    "path survives the landing and the following plain commit reverts it"
)

# ``GIT_OPTIONAL_LOCKS=0`` keeps a read of the index from rewriting it, so a
# case measures the landing rather than the reading of it.
_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

_LANDED = ("ledger.py", "test_ledger.py", "tracked.txt")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(repo, *args)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _init_checkout(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True, env=_ENV)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=_ENV)
    _git(work, "config", "user.email", "author@example.invalid")
    _git(work, "config", "user.name", "Author")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "--", "tracked.txt")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def _reviewed_head(work: Path) -> str:
    """A head adding a module and its test and changing a tracked file."""
    _git(work, "checkout", "-q", "-b", "reviewed-head")
    (work / "ledger.py").write_text("landed ledger\n" * 40, encoding="utf-8")
    (work / "test_ledger.py").write_text("def test_landed():\n    pass\n", "utf-8")
    (work / "tracked.txt").write_text("landed\n", encoding="utf-8")
    _git(work, "add", "--", "ledger.py", "test_ledger.py", "tracked.txt")
    _git(work, "commit", "-q", "-m", "add the landed work")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    return head


def _stage_pre_landing_copies(work: Path) -> None:
    """Install the race at the one point git guarantees after the merge.

    The hook is a peer that read the checkout before the merge: it stages the
    pre-landing copy of the tracked path and drops the two landed paths from
    the index, which is the shape the measured incidents recorded.
    """
    hook = work / ".git" / "hooks" / "post-merge"
    hook.write_text(
        "#!/bin/sh\n"
        'cd "$(git rev-parse --show-toplevel)" || exit 1\n'
        "printf 'base\\n' > tracked.txt\n"
        "git add -- tracked.txt\n"
        "git rm -q --cached --force -- ledger.py test_ledger.py\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def _staged_paths(work: Path) -> list[str]:
    return _run(work, "diff", "--cached", "--name-only").stdout.split()


def _blob(work: Path, revision: str, path: str) -> str:
    return _git(work, "show", f"{revision}:{path}")


def _remove_the_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("RECKON_MERGE_INDEX_RECONCILIATION") == "removed":
        monkeypatch.setattr(merge, "clear_stale_landed_copies", lambda repo, paths: [])


def test_a_landing_leaves_no_landed_path_staged(
    tmp_path: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No landed path is staged against HEAD, and a plain commit keeps them all."""
    _remove_the_reconciliation(monkeypatch)
    _origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work)
    _stage_pre_landing_copies(work)

    report = merge.land_merge(
        work,
        head=head,
        message="Land the reviewed head",
        body="the reviewed head is landed",
        remote="origin",
    )

    landed = _git(work, "rev-parse", "HEAD")
    assert report["commit"] == landed
    assert _git(_origin, "rev-parse", "main") == landed
    assert set(report["unstaged"]) == set(_LANDED)
    assert _staged_paths(work) == []

    # A coordinator's plain plan commit, the action that took the measured
    # incident: it carries the index, so it must carry nothing of the landing.
    (work / "plan.html").write_text("<html>plan</html>\n", encoding="utf-8")
    _git(work, "add", "--", "plan.html")
    _git(work, "commit", "-q", "-m", "docs(plan): record a decision")
    touched = _git(work, "show", "--name-only", "--format=").split()
    assert touched == ["plan.html"]
    for path in _LANDED:
        assert _blob(work, "HEAD", path) == _blob(work, landed, path)


def test_a_clean_landing_unstages_nothing(
    tmp_path: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a writer in the window the landing finds nothing to clear."""
    _remove_the_reconciliation(monkeypatch)
    _origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work)

    report = merge.land_merge(
        work,
        head=head,
        message="Land the reviewed head",
        body="the reviewed head is landed",
        remote="origin",
    )

    assert report["unstaged"] == []
    assert _staged_paths(work) == []
    assert (work / "ledger.py").is_file()


def test_a_peer_edit_stays_uncommitted_rather_than_staged(
    tmp_path: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index is restored; the peer's working-tree file is left alone."""
    _remove_the_reconciliation(monkeypatch)
    _origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work)
    _stage_pre_landing_copies(work)

    merge.land_merge(
        work,
        head=head,
        message="Land the reviewed head",
        body="the reviewed head is landed",
        remote="origin",
    )

    assert (work / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert _staged_paths(work) == []


def test_a_promotion_clears_a_copy_staged_after_its_commit(
    tmp_path: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion's own commit reconciles the paths that commit moved."""
    _remove_the_reconciliation(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "--", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    written = repo / "written.txt"
    written.write_text("the landing's content\n", encoding="utf-8")

    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "printf 'the pre-landing content\\n' > written.txt\n"
        "git add -- written.txt\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = _commit_landing_writes(
        run_id="r-promotion", verdict="passed", checkout=repo, paths=[written]
    )

    assert result["committed"] is True
    assert result["landing_check"]["unstaged"] == ["written.txt"]
    assert _staged_paths(repo) == []
    assert _blob(repo, "HEAD", "written.txt") == "the landing's content"
