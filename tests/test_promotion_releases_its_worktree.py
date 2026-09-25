"""Promotion reclaims only worktrees that are safe to remove."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion
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
                "title": "Promotion cleanup",
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
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _write_plan(repository / "docs" / "plans" / f"{PLAN}.html")
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "docs", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _worktree(
    repository: Path, root: Path, name: str, *, divergent: bool = False
) -> Path:
    worktree = root / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    if divergent:
        (worktree / "worker-result.txt").write_text("worker result\n", encoding="utf-8")
        _git(worktree, "add", "worker-result.txt")
        _git(worktree, "commit", "-q", "-m", "test: worker result")
    return worktree


def _pointer(
    repository: Path,
    root: Path,
    run_id: str,
    worktree: Path,
    *,
    status: str = "complete",
) -> None:
    manifest = root / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: node-a\nstatus: {status}\n", encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-25T07:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§4",
                "time_budget": "35m",
                "write_paths": [],
            },
        },
    )


def _stored_run(repository: Path, run_id: str) -> dict:
    return next(
        run for run in ledger.runs(PROJECT, root=repository) if run["run_id"] == run_id
    )


def test_promotion_releases_only_the_integrated_clean_case(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("PROMOTION_NEGATIVE_CONTROL"):
        monkeypatch.setattr(promotion, "_live_worktree_claims", dict)
    cases = (
        ("clean", False, False, False, "passed"),
        ("dirty", True, False, False, "passed"),
        ("unintegrated", False, True, False, "passed"),
        ("live-reference", False, False, True, "passed"),
        ("failed", False, False, False, "failed"),
    )

    for name, dirty, divergent, live_reference, gate in cases:
        run_id = f"r-20260925T070000000000-{name}"
        worktree = _worktree(repository, tmp_path, name, divergent=divergent)
        if dirty:
            (worktree / "uncommitted.txt").write_text("keep me\n", encoding="utf-8")
        _pointer(repository, tmp_path, run_id, worktree)
        peer_id = f"r-20260925T070000000000-{name}-peer"
        if live_reference:
            _pointer(repository, tmp_path, peer_id, worktree)

        kwargs = {
            "run_id": run_id,
            "gate": gate,
            "outcome": f"{name} promotion case",
            "root": repository,
        }
        if divergent:
            kwargs["no_commit"] = (
                "the worker result is deliberately uncited for this case"
            )
        if gate == "failed":
            kwargs["failure_classification"] = "negative-result"
        else:
            kwargs["review_waiver"] = (
                "the synthesized cleanup fixture has no code review"
            )
        promoted = crew.complete(**kwargs)
        release = promoted["release"]
        stored = _stored_run(repository, run_id)

        if name == "clean":
            assert release["worktree_released"] is True
            assert not worktree.exists()
            assert stored["release"]["worktree_released"] is True
        else:
            assert release["worktree_released"] is False
            assert worktree.is_dir()
            assert stored["release"] == release

        if name == "dirty":
            assert "uncommitted changes" in release["worktree_withheld"]
        elif name == "unintegrated":
            assert "not reachable" in release["worktree_withheld"]
        elif name == "live-reference":
            assert "live run pointer" in release["worktree_withheld"]
            assert pointer_path(peer_id).exists()
        elif name == "failed":
            assert "not passing" in release["worktree_withheld"]

        if pointer_path(peer_id).exists():
            pointer_path(peer_id).unlink()
