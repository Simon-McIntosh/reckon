"""Promotion's release path spares a worktree a parked peer's pointer names.

Promotion removes the promoted run's pointer and then releases its worktree
through ``_release_run_workspace``. The release judges liveness through the
wide claim — every worktree a live pointer names, whatever phase that pointer
carries — because a peer parked on an external wait keeps its live pointer
while its phase reads terminal-looking: it shares the tree, and the tree is
not this run's to remove. A phase-gated reading classified the shared tree as
clean and integrated and removed it under the parked peer.

Every case works on synthesised pointers, ledgers and repositories under a
temporary config home; no real fleet directory is read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Promotion release claim",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _write_plan(repository / "docs" / "plans" / f"{PLAN}.html")
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "docs", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: seed the release-claim fixture")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _worktree(repository: Path, root: Path, name: str) -> Path:
    worktree = root / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return worktree


def _dead_pid() -> int:
    """A pid the kernel reports as gone: a reaped child."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _pointer(
    repository: Path,
    root: Path,
    run_id: str,
    worktree: Path,
    *,
    status: str = "complete",
    phase: str | None = None,
    pid: int | None = None,
) -> None:
    manifest = root / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: node-a\nstatus: {status}\n", encoding="utf-8")
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-10-03T14:55:00Z",
        "base_sha": _git(repository, "rev-parse", "HEAD"),
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§4",
            "time_budget": "35m",
            "write_paths": [],
        },
    }
    if phase is not None:
        record["phase"] = phase
    if pid is not None:
        record["pid"] = pid
    _write_json(pointer_path(run_id), record)


def test_promotion_spares_a_worktree_a_parked_peer_names(
    repository: Path, tmp_path: Path
) -> None:
    """A parked peer's pointer keeps the tree the promoted run shares with it.

    The peer's phase reads terminal, which is the parked-between-turns state:
    the phase-gated claim does not see it, so a release deciding through that
    reading classified the shared tree as clean and integrated and removed it.
    """
    peer_run = "r-20261003T145500000000-parked-peer"
    run_id = "r-20261003T145500000001-promoted-subject"
    worktree = _worktree(repository, tmp_path, "shared-tree")
    _pointer(repository, tmp_path, run_id, worktree)
    _pointer(
        repository,
        tmp_path,
        peer_run,
        worktree,
        status="waiting",
        phase="complete",
        pid=_dead_pid(),
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the promoted run shares its worktree with a parked peer",
        review_waiver="the synthesized cleanup fixture has no code review",
        root=repository,
    )

    release = promoted["release"]
    assert release["worktree_released"] is False
    assert worktree.is_dir()
    assert "live run pointer" in release["worktree_withheld"]
    assert pointer_path(peer_run).exists()


def test_promotion_still_releases_a_worktree_no_live_pointer_names(
    repository: Path, tmp_path: Path
) -> None:
    """The wide claim does not freeze every tree: an unshared one still goes."""
    run_id = "r-20261003T145500000002-unshared-subject"
    worktree = _worktree(repository, tmp_path, "unshared-tree")
    _pointer(repository, tmp_path, run_id, worktree)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the promoted run holds its worktree alone",
        review_waiver="the synthesized cleanup fixture has no code review",
        root=repository,
    )

    assert promoted["release"]["worktree_released"] is True
    assert not worktree.exists()
