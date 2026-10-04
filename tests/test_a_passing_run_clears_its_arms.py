"""Promotion clears the scratch arms a run's own manifest cites.

Arms a worker places outside its scratch directory are named by nothing but the
manifest that cites them, so the release reads that citation rather than the
directory's name. The removal is bounded to the node-local temp root and to
paths no other live run cites, and every cited log inside a removed arm is
copied into the run directory first so the evidence survives the removal.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

from reckon import _plan_html, crew
from reckon.crew.runs import _write_json, pointer_path

dispatch = importlib.import_module("reckon.crew.dispatch")


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository_with_plan(tmp_path: Path, home: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    plan = repository / "docs" / "plans" / "sample.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        _plan_html.write_state(
            '<!doctype html><html><head><meta name="docs-project" content="sample">'
            '<title>Sample</title></head><body><main class="plan-doc"></main>'
            "</body></html>",
            {
                "type": "plan",
                "slug": "sample",
                "title": "Sample",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "seed.txt", "docs")
    _git(repository, "commit", "-q", "-m", "seed repository")
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    return repository, worktree


def _pointer(repository: Path, worktree: Path, run_id: str, manifest: Path) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "sample",
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "created_at": "2026-10-04T14:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "scratch": str(dispatch.worker_scratch_dir(run_id)),
            "manifest_path": str(manifest),
            "pid": None,
            "pid_start_time": None,
            "node": {
                "id": "sample",
                "plan": "sample",
                "section": "sample",
                "time_budget": "40m",
                "write_paths": [],
            },
        },
    )


def test_promotion_removes_cited_arms_and_keeps_the_rest(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    temp_root = tmp_path / "node-tmp"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(temp_root / "scratch"))
    repository, worktree = _repository_with_plan(tmp_path, home)

    run_id = "r-20261004T140000000000-sample"
    other_run = "r-20261004T140000000001-sibling"
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    crew.run_dir(other_run).mkdir(parents=True, exist_ok=True)
    dispatch.ensure_worker_scratch(run_id)

    base_arm = temp_root / "recon-base-arm"
    control_tree = temp_root / "rev-arms"
    outside = tmp_path / "work-area" / "held-tree"
    shared = temp_root / "shared-basetemp"
    for directory in (base_arm, control_tree, outside, shared):
        directory.mkdir(parents=True)
    (base_arm / "gate.log").write_text("EXIT=0\n", encoding="utf-8")
    (base_arm / "payload.bin").write_bytes(b"base")
    (control_tree / "control.log").write_text("EXIT=0\n", encoding="utf-8")
    (control_tree / "payload.bin").write_bytes(b"control")
    (outside / "kept.log").write_text("EXIT=0\n", encoding="utf-8")
    (shared / "shared.log").write_text("EXIT=0\n", encoding="utf-8")

    other_manifest = tmp_path / "other-manifest.md"
    other_manifest.write_text(
        f"node: sibling\nstatus: complete\ncontrol_tree: {shared}\n",
        encoding="utf-8",
    )
    _pointer(repository, tmp_path / "other-worktree", other_run, other_manifest)

    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: sample\n"
        "status: complete\n"
        f"base_arm: {base_arm}\n"
        f"control_tree: {control_tree}\n"
        f"sandbox_tree: {outside}\n"
        f"shared_basetemp: {shared}\n"
        "arm_logs:\n"
        f"  - {base_arm}/gate.log\n"
        f"  - {control_tree}/control.log\n",
        encoding="utf-8",
    )
    _pointer(repository, worktree, run_id, manifest)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="cited arms released",
        completed_at="2026-10-04T14:05:00Z",
        root=repository,
        review_waiver="the arm release is the subject; this synthetic run stores no review",
    )

    release = promoted["release"]
    assert not base_arm.exists()
    assert not control_tree.exists()
    assert outside.is_dir()
    assert shared.is_dir()
    removed = {row["path"]: row["bytes"] for row in release["arms_removed_paths"]}
    assert removed == {
        str(base_arm.resolve()): 11,
        str(control_tree.resolve()): 14,
    }
    kept = {row["path"]: row["reason"] for row in release["arms_kept"]}
    assert "node-local temp root" in kept[str(outside.resolve())]
    assert other_run in kept[str(shared.resolve())]
    copied = {row["log"] for row in release["arms_logs_copied"]}
    assert copied == {
        str(base_arm / "gate.log"),
        str(control_tree / "control.log"),
    }
    run_dir = crew.run_dir(run_id)
    assert (run_dir / "cleared-arms" / "recon-base-arm" / "gate.log").read_text(
        encoding="utf-8"
    ) == "EXIT=0\n"
    assert (run_dir / "cleared-arms" / "rev-arms" / "control.log").is_file()
    assert (
        promoted["record"]["release"]["arms_removed_paths"]
        == release["arms_removed_paths"]
    )
