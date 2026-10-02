"""gc --apply must carry past a registered worktree whose directory is gone.

Measured defect: a whole-registry reclaim aborted on the first tree git still
registered when its directory had been removed, so one vanished registration
stopped every other reclaimable tree from being reclaimed and nothing was
removed. The dry run reported the same raise. The pass now reports the
unavailable tree under its own classification, leaves its git registration in
place, and reclaims every reclaimable tree beside it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import routing

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "reckon-build"
    / "scripts"
    / "worktree_fleet.py"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-q", "-m", "test: seed")
    return repo


def create_worktree(repo: Path, session: str, worker: str) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--repo",
            str(repo),
            "--session",
            session,
            "--worker",
            worker,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(json.loads(result.stdout)["path"])


def merged_worktree(repo: Path, session: str, worker: str) -> Path:
    """A worktree whose commit the main branch already carries (integrated, clean)."""
    worktree = create_worktree(repo, session, worker)
    (worktree / "delivered.txt").write_text("delivered\n")
    git(worktree, "add", "delivered.txt")
    git(worktree, "commit", "-q", "-m", "test: delivered work")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        git(worktree, "rev-parse", "HEAD"),
        "-m",
        "test: merge the delivered work",
    )
    return worktree


def test_apply_reclaims_the_rest_past_a_vanished_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    reclaimed = merged_worktree(repo, "s21", "reclaimed")
    vanished = create_worktree(repo, "s21", "vanished")
    shutil.rmtree(vanished)
    # The directory is gone while git still registers the tree: the exact state
    # that aborted the whole collection.
    assert not vanished.exists()
    assert vanished.resolve() in {
        path.resolve() for path in routing._registered_worktrees(repo)
    }

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli.main, ["crew", "gc", "--apply"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True

    # The reclaimable tree is reclaimed.
    assert str(reclaimed) in payload["removed_worktrees"]
    assert not reclaimed.exists()

    rows = {Path(item["path"]).name: item for item in payload["worktrees"]}

    # The vanished tree is reported with its detail and not removed.
    assert rows["vanished"]["classification"] == "unavailable"
    assert "no longer available" in rows["vanished"]["detail"]
    assert rows["vanished"]["reclaimable"] is False
    assert "left in place" in rows["vanished"]["withheld"]
    assert str(vanished) not in payload["removed_worktrees"]

    # Its git registration survives the pass, untouched.
    assert vanished.resolve() in {
        path.resolve() for path in routing._registered_worktrees(repo)
    }
    assert payload["counts"]["integrated"] == 1


def test_dry_run_reports_the_contract_before_apply_reclaims(
    monkeypatch, tmp_path: Path
) -> None:
    """The dry run states the same classification without removing anything."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    reclaimed = merged_worktree(repo, "s21", "reclaimed")
    vanished = create_worktree(repo, "s21", "vanished")
    shutil.rmtree(vanished)

    report = routing.garbage_collect(repo=repo, apply=False)

    assert report["removed_worktrees"] == []
    assert reclaimed.exists()
    rows = {Path(item["path"]).name: item for item in report["worktrees"]}
    assert rows["vanished"]["classification"] == "unavailable"
    assert rows["reclaimed"]["classification"] == "integrated"
