"""The stop hook refuses a turn end that leaves the run's worktree dirty.

A worktree holding uncommitted changes cannot be released without a force, so
the turn end is refused until the worker commits or discards them — unless the
manifest is blocked or failed and names every path it leaves. Each case builds
its own temporary repository and run directory, so nothing here reads or writes
the machine's live fleet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from reckon.hooks import worker_stop as hook

IDENT = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")
CLEAN_MANIFEST = "node: fixture\nstatus: complete\ncheckpoint: done\n"


def _git(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo(tmp_path: Path) -> Path:
    """A one-commit repository, standing in for a dispatched run's worktree."""
    repo = tmp_path / "worktree"
    repo.mkdir()
    assert _git("init", "-q", "-b", "main", cwd=repo).returncode == 0
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    assert _git(*IDENT, "add", "tracked.txt", cwd=repo).returncode == 0
    assert _git(*IDENT, "commit", "-q", "-m", "base", cwd=repo).returncode == 0
    return repo


def _write_manifest(run: Path, text: str) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    manifest = run / "manifest.md"
    manifest.write_text(text, encoding="utf-8")
    return manifest


def _bind(monkeypatch, home: Path, manifest: Path, worktree: Path | None) -> None:
    """Bind the hook to the manifest, and optionally a live pointer to the tree."""
    if worktree is not None:
        live = home / "crew" / "live"
        live.mkdir(parents=True, exist_ok=True)
        (live / "r-fixture.json").write_text(
            json.dumps({"worktree": str(worktree), "manifest_path": str(manifest)}),
            encoding="utf-8",
        )
    monkeypatch.setenv("RECKON_MANIFEST", str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(home))


def _stop(cwd: Path) -> dict:
    return {"hook_event_name": "Stop", "cwd": str(cwd), "stop_hook_active": False}


def test_a_clean_end_is_allowed(tmp_path, monkeypatch) -> None:
    """A committed worktree resolves from the stopping process's cwd."""
    repo = _repo(tmp_path)
    run = tmp_path / "run"
    manifest = _write_manifest(run, CLEAN_MANIFEST)
    _bind(monkeypatch, tmp_path / "config", manifest, worktree=None)

    blocked, reason = hook.decide(_stop(repo))

    assert (blocked, reason) == (False, None)
    assert not (run / hook.COUNTER_NAME).exists()


def test_a_dirty_end_is_refused_and_names_every_path(tmp_path, monkeypatch) -> None:
    """The refusal names each uncommitted path and says to commit or discard."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    assert _git(*IDENT, "add", ".gitignore", cwd=repo).returncode == 0
    assert _git(*IDENT, "commit", "-q", "-m", "ignore", cwd=repo).returncode == 0
    (repo / "tracked.txt").write_text("base\nedited\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    run = tmp_path / "run"
    manifest = _write_manifest(run, CLEAN_MANIFEST)
    _bind(monkeypatch, tmp_path / "config", manifest, worktree=repo)

    # The stopping process stands in the run directory, so the worktree comes
    # from the live pointer rather than from the cwd.
    blocked, reason = hook.decide(_stop(run))

    assert blocked is True
    assert reason is not None
    assert "tracked.txt" in reason
    assert "notes.txt" in reason
    assert "Commit each path or discard it" in reason
    assert "ignored.txt" not in reason
    assert (run / hook.COUNTER_NAME).read_text().strip() == "1"


def test_a_blocked_end_that_names_its_paths_may_end_dirty(
    tmp_path, monkeypatch
) -> None:
    """A blocked worker may leave its mess once the manifest says what it is;
    the same dirty worktree without that line is refused."""
    repo = _repo(tmp_path)
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")

    run = tmp_path / "run"
    manifest = _write_manifest(
        run,
        "node: fixture\nstatus: blocked\n"
        "blocker: the notes.txt edit is not ready to commit\n",
    )
    _bind(monkeypatch, tmp_path / "config", manifest, worktree=repo)

    blocked, reason = hook.decide(_stop(run))

    assert (blocked, reason) == (False, None)

    _write_manifest(
        run,
        "node: fixture\nstatus: blocked\nblocker: the work did not finish\n",
    )
    blocked, reason = hook.decide(_stop(run))

    assert blocked is True
    assert reason is not None
    assert "notes.txt" in reason


def test_the_provisioning_links_are_exempt(tmp_path, monkeypatch) -> None:
    """The .venv and .env links every worktree is handed are not dirt."""
    repo = _repo(tmp_path)
    (repo / ".venv").symlink_to(tmp_path / "shared-venv")
    (repo / ".env").symlink_to(tmp_path / "shared.env")
    run = tmp_path / "run"
    manifest = _write_manifest(run, CLEAN_MANIFEST)
    _bind(monkeypatch, tmp_path / "config", manifest, worktree=repo)

    blocked, reason = hook.decide(_stop(run))

    assert (blocked, reason) == (False, None)

    (repo / "loose.txt").write_text("untracked\n", encoding="utf-8")
    blocked, reason = hook.decide(_stop(run))

    assert blocked is True
    assert reason is not None
    assert "loose.txt" in reason
    assert ".venv" not in reason
    assert ".env" not in reason


def test_the_run_directory_inside_the_tree_is_exempt(tmp_path, monkeypatch) -> None:
    """A run directory under the worktree holds the manifest, not dirt."""
    repo = _repo(tmp_path)
    run = repo / ".run"
    manifest = _write_manifest(run, CLEAN_MANIFEST)
    (run / "gate.log").write_text("EXIT=0\n", encoding="utf-8")
    _bind(monkeypatch, tmp_path / "config", manifest, worktree=repo)

    blocked, reason = hook.decide(_stop(run))

    assert (blocked, reason) == (False, None)

    (repo / "loose.txt").write_text("untracked\n", encoding="utf-8")
    blocked, reason = hook.decide(_stop(run))

    assert blocked is True
    assert reason is not None
    assert "loose.txt" in reason
    assert ".run" not in reason
