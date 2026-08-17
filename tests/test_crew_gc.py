from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from reckon import cli


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "reckon-ship"
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
    repo.mkdir()
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


def write_pointer(home: Path, run_id: str, worktree: Path) -> None:
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worktree": str(worktree),
                "phase": "working",
                "pid": 999999999,
            }
        )
    )


def test_gc_dry_run_itemizes_worktrees_without_touching_them(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    integrated = create_worktree(repo, "audit", "integrated")
    dirty = create_worktree(repo, "audit", "dirty")
    live = create_worktree(repo, "audit", "live")
    (dirty / "untracked.txt").write_text("dirty\n")
    write_pointer(home, "run-live", live)

    result = CliRunner().invoke(cli.main, ["crew", "gc", "--repo", str(repo)])
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["dry_run"] is True
    assert payload["counts"] == {
        "dirty": 1,
        "integrated": 1,
        "live-referenced": 1,
        "unintegrated": 0,
    }
    by_path = {item["path"]: item for item in payload["worktrees"]}
    assert by_path[str(integrated)]["classification"] == "integrated"
    assert by_path[str(dirty)]["classification"] == "dirty"
    assert by_path[str(live)]["claimed_by_live_runs"] == ["run-live"]
    assert integrated.exists() and dirty.exists() and live.exists()


def test_gc_apply_removes_only_integrated_unclaimed_worktrees(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    integrated = create_worktree(repo, "apply", "integrated")
    dirty = create_worktree(repo, "apply", "dirty")
    live = create_worktree(repo, "apply", "live")
    (dirty / "untracked.txt").write_text("dirty\n")
    write_pointer(home, "run-live", live)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["dry_run"] is False
    assert payload["removed_worktrees"] == [str(integrated)]
    assert not integrated.exists()
    assert dirty.exists() and live.exists()
