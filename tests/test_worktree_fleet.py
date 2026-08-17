from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path


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
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "test: seed repository")
    return repo


def command(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def command_without_reckon(
    repo: Path, runner: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    runner.write_text(
        """\
import builtins
import runpy
import sys

real_import = builtins.__import__

def reject_reckon(name, *args, **kwargs):
    if name == "reckon" or name.startswith("reckon."):
        raise ImportError("reckon is unavailable in this interpreter")
    return real_import(name, *args, **kwargs)

builtins.__import__ = reject_reckon
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
runpy.run_path(script, run_name="__main__")
"""
    )
    return subprocess.run(
        [sys.executable, str(runner), str(SCRIPT), *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def create_worktree(repo: Path, session: str, worker: str) -> Path:
    result = command(
        repo,
        "create",
        "--repo",
        str(repo),
        "--session",
        session,
        "--worker",
        worker,
    )
    assert result.returncode == 0, result.stdout
    return Path(json.loads(result.stdout)["path"])


def cleanup(repo: Path, session: str) -> subprocess.CompletedProcess[str]:
    return command(
        repo,
        "cleanup-session",
        "--repo",
        str(repo),
        "--session",
        session,
        "--integrated-into",
        "HEAD",
    )


def write_live_pointer(home: Path, run_id: str, worktree: Path, **fields) -> Path:
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    path = live / f"{run_id}.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worktree": str(worktree),
                "manifest_path": "",
                **fields,
            }
        )
    )
    return path


def test_cleanup_requires_reachable_commit(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "delivery", "worker-a")
    (worktree / "result.txt").write_text("result\n")
    git(worktree, "add", "result.txt")
    git(worktree, "commit", "-m", "test: add isolated result")
    worker_head = git(worktree, "rev-parse", "HEAD")

    refused = cleanup(repo, "delivery")
    assert refused.returncode == 2
    assert "reachable" in refused.stdout
    assert worktree.exists()

    git(repo, "merge", "--no-ff", worker_head, "-m", "test: integrate result")
    removed = cleanup(repo, "delivery")
    assert removed.returncode == 0, removed.stdout
    assert not worktree.exists()


def test_cleanup_refuses_dirty_worktree(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "dirty-check", "worker-b")
    dirty = worktree / "untracked.txt"
    dirty.write_text("not committed\n")

    refused = cleanup(repo, "dirty-check")
    assert refused.returncode == 2
    assert "untracked.txt" in refused.stdout
    assert worktree.exists()

    dirty.unlink()
    removed = cleanup(repo, "dirty-check")
    assert removed.returncode == 0, removed.stdout
    assert not worktree.exists()


def test_cleanup_refuses_a_worktree_claimed_by_a_running_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "live-claim", "worker-live")
    pointer = write_live_pointer(
        home,
        "run-active",
        worktree,
        phase="working",
        pid=999999999,
    )

    refused = cleanup(repo, "live-claim")

    assert refused.returncode == 2
    assert "claimed_by_live_runs" in refused.stdout
    assert "run-active" in refused.stdout
    assert worktree.exists()

    pointer.unlink()
    removed = cleanup(repo, "live-claim")
    assert removed.returncode == 0, removed.stdout
    assert not worktree.exists()


def test_cleanup_reports_live_and_dirty_refusals_together(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    claimed = create_worktree(repo, "mixed-refusal", "worker-live")
    dirty = create_worktree(repo, "mixed-refusal", "worker-dirty")
    (dirty / "untracked.txt").write_text("not committed\n")
    write_live_pointer(
        home,
        "run-visible",
        claimed,
        process_alive=True,
    )

    refused = cleanup(repo, "mixed-refusal")

    assert refused.returncode == 2
    assert "run-visible" in refused.stdout
    assert str(claimed) in refused.stdout
    assert "untracked.txt" in refused.stdout
    assert str(dirty) in refused.stdout
    assert claimed.exists()
    assert dirty.exists()


def test_cleanup_ignores_an_abandoned_pointer(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "stale-claim", "worker-stale")
    write_live_pointer(
        home,
        "run-abandoned",
        worktree,
        pid=999999999,
    )

    removed = cleanup(repo, "stale-claim")

    assert removed.returncode == 0, removed.stdout
    assert not worktree.exists()


def test_create_and_cleanup_run_without_reckon_importable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    runner = tmp_path / "without_reckon.py"

    created = command_without_reckon(
        repo,
        runner,
        "create",
        "--repo",
        str(repo),
        "--session",
        "standalone",
        "--worker",
        "worker-independent",
    )
    assert created.returncode == 0, created.stderr
    worktree = Path(json.loads(created.stdout)["path"])
    assert worktree.exists()

    removed = command_without_reckon(
        repo,
        runner,
        "cleanup-session",
        "--repo",
        str(repo),
        "--session",
        "standalone",
        "--integrated-into",
        "HEAD",
    )
    assert removed.returncode == 0, removed.stderr
    assert not worktree.exists()


def test_standalone_cleanup_still_refuses_a_live_claim(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    worktree = create_worktree(repo, "standalone-claim", "worker-live")
    runner = tmp_path / "without_reckon.py"
    pointer = write_live_pointer(
        home,
        "run-standalone",
        worktree,
        process_alive=True,
    )

    refused = command_without_reckon(
        repo,
        runner,
        "cleanup-session",
        "--repo",
        str(repo),
        "--session",
        "standalone-claim",
        "--integrated-into",
        "HEAD",
    )
    assert refused.returncode == 2
    assert "run-standalone" in refused.stdout
    assert worktree.exists()

    pointer.unlink()
    removed = command_without_reckon(
        repo,
        runner,
        "cleanup-session",
        "--repo",
        str(repo),
        "--session",
        "standalone-claim",
        "--integrated-into",
        "HEAD",
    )
    assert removed.returncode == 0, removed.stderr
    assert not worktree.exists()


def test_worktree_script_has_no_reckon_import() -> None:
    tree = ast.parse(SCRIPT.read_text())
    imports = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import)
            and any(alias.name == "reckon" or alias.name.startswith("reckon.") for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "reckon" or node.module.startswith("reckon."))
        )
    ]
    assert imports == []


def test_rejects_unsafe_session_token(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    result = command(
        repo,
        "create",
        "--repo",
        str(repo),
        "--session",
        "../outside",
        "--worker",
        "worker-c",
    )
    assert result.returncode == 2
    assert "session must match" in result.stdout


def test_create_refuses_to_nest_a_second_worktree_root(tmp_path: Path) -> None:
    nested_parent = tmp_path / ".reckon-worktrees" / "existing" / "session"
    nested_parent.mkdir(parents=True)
    repo = repository(nested_parent)

    result = command(
        repo,
        "create",
        "--repo",
        str(repo),
        "--session",
        "nested",
        "--worker",
        "worker-c",
    )

    assert result.returncode == 2
    assert "refusing to nest another reckon-worktrees root" in result.stdout
