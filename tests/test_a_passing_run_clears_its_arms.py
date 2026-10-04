"""Promotion clears the arms a passing run's own manifest declares.

Arms a worker places outside its scratch directory are named by nothing but the
manifest that declares them, so the release reads the structured fields that
carry a declared path — the log fields, the suite records' log and basetemp, and
the artifacts field — rather than any path that happens to appear in prose. The
removal is bounded to the node-local temp root and to paths no other live run
cites, it runs only for a run whose gate passed because a failed run's arms are
the evidence a repair reads, and every declared log inside a removed arm is
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

REVIEW_WAIVER = "the arm release is the subject; this synthetic run stores no review"


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


def _world(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A config home, a repository with a worktree, and a node-local temp root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    temp_root = tmp_path / "node-tmp"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(temp_root / "scratch"))
    repository, worktree = _repository_with_plan(tmp_path, home)
    run_id = "r-20261004T140000000000-sample"
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    dispatch.ensure_worker_scratch(run_id)
    return {
        "home": home,
        "temp_root": temp_root,
        "repository": repository,
        "worktree": worktree,
        "run_id": run_id,
    }


def test_promotion_removes_declared_arms_and_keeps_the_rest(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    temp_root = world["temp_root"]
    run_id = str(world["run_id"])
    repository = world["repository"]
    worktree = world["worktree"]

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

    other_run = "r-20261004T140000000001-sibling"
    other_manifest = tmp_path / "other-manifest.md"
    other_manifest.write_text(
        "node: sibling\n"
        "status: complete\n"
        "test_logs:\n"
        f"  - {shared}/shared.log\n"
        "baseline_suite:\n"
        f"  command: pytest --basetemp={shared} tests/\n",
        encoding="utf-8",
    )
    _pointer(repository, tmp_path / "other-worktree", other_run, other_manifest)

    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: sample\n"
        "status: complete\n"
        "test_logs:\n"
        f"  - {base_arm}/gate.log\n"
        f"  - {control_tree}/control.log\n"
        f"  - {shared}/shared.log\n"
        f"negative_control_log: {control_tree}/control.log\n"
        "baseline_suite:\n"
        f"  log_path: {base_arm}/gate.log\n"
        f"  command: uv run --no-sync pytest --basetemp={base_arm} tests/\n"
        "after_suite: "
        + json.dumps(
            {
                "revision": "0" * 40,
                "log_path": str(control_tree / "control.log"),
                "command": f"pytest --basetemp={control_tree} tests/",
            }
        )
        + "\n"
        "artifacts:\n"
        f"  - {outside}\n"
        f"  - {base_arm}\n"
        f"  - {shared}\n"
        f"checkpoint: checked the sibling at {shared} before promoting\n",
        encoding="utf-8",
    )
    _pointer(repository, worktree, run_id, manifest)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="declared arms released",
        completed_at="2026-10-04T14:05:00Z",
        root=repository,
        review_waiver=REVIEW_WAIVER,
    )

    release = promoted["release"]
    assert not base_arm.exists()
    assert not control_tree.exists()
    assert outside.is_dir()
    assert shared.is_dir()
    assert release["arms_withheld"] == ""
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


def test_a_directory_named_only_in_prose_is_not_removed(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    temp_root = world["temp_root"]
    run_id = str(world["run_id"])
    repository = world["repository"]
    worktree = world["worktree"]

    base_arm = temp_root / "recon-base-arm"
    prose_peer = temp_root / "peer-session-arms"
    for directory in (base_arm, prose_peer):
        directory.mkdir(parents=True)
    (base_arm / "gate.log").write_text("EXIT=0\n", encoding="utf-8")
    (prose_peer / "peer.log").write_text("EXIT=0\n", encoding="utf-8")

    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: sample\n"
        "status: complete\n"
        "baseline_suite:\n"
        f"  log_path: {base_arm}/gate.log\n"
        f"  command: pytest --basetemp={base_arm} tests/\n"
        f"checkpoint: observed {prose_peer} while reading a peer session\n"
        "follow_ons:\n"
        f"  - the peer session should clear {prose_peer} on its own promotion\n",
        encoding="utf-8",
    )
    _pointer(repository, worktree, run_id, manifest)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="a prose mention is not a declaration of ownership",
        completed_at="2026-10-04T14:05:00Z",
        root=repository,
        review_waiver=REVIEW_WAIVER,
    )

    release = promoted["release"]
    assert not base_arm.exists()
    assert prose_peer.is_dir()
    assert (prose_peer / "peer.log").is_file()
    mentioned = {row["path"] for row in release["arms_kept"]}
    assert str(prose_peer.resolve()) not in mentioned
    assert {row["path"] for row in release["arms_removed_paths"]} == {
        str(base_arm.resolve())
    }


def test_an_artifact_description_is_not_a_declaration(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    temp_root = world["temp_root"]
    run_id = str(world["run_id"])
    repository = world["repository"]
    worktree = world["worktree"]

    declared_arm = temp_root / "declared-artifact"
    described = temp_root / "peer-session-tree"
    for directory in (declared_arm, described):
        directory.mkdir(parents=True)
    (declared_arm / "payload.bin").write_bytes(b"arm")
    (described / "peer.bin").write_bytes(b"peer")

    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: sample\n"
        "status: complete\n"
        "artifacts:\n"
        f"  {declared_arm}: the hand-off extraction this run produced; the peer "
        f"tree at {described} is not this run's\n",
        encoding="utf-8",
    )
    _pointer(repository, worktree, run_id, manifest)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the artifact key is a declaration, its description is prose",
        completed_at="2026-10-04T14:05:00Z",
        root=repository,
        review_waiver=REVIEW_WAIVER,
    )

    release = promoted["release"]
    assert not declared_arm.exists()
    assert described.is_dir()
    assert (described / "peer.bin").is_file()
    assert {row["path"] for row in release["arms_removed_paths"]} == {
        str(declared_arm.resolve())
    }
    assert str(described.resolve()) not in {row["path"] for row in release["arms_kept"]}


def test_a_failed_gate_keeps_both_arms(tmp_path: Path, monkeypatch) -> None:
    world = _world(tmp_path, monkeypatch)
    temp_root = world["temp_root"]
    run_id = str(world["run_id"])
    repository = world["repository"]
    worktree = world["worktree"]

    base_arm = temp_root / "recon-base-arm"
    control_tree = temp_root / "rev-arms"
    for directory in (base_arm, control_tree):
        directory.mkdir(parents=True)
    (base_arm / "gate.log").write_text("EXIT=1\n", encoding="utf-8")
    (control_tree / "control.log").write_text("EXIT=1\n", encoding="utf-8")

    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: sample\n"
        "status: complete\n"
        "test_logs:\n"
        f"  - {base_arm}/gate.log\n"
        f"  - {control_tree}/control.log\n"
        "negative_control_log: "
        f"{control_tree}/control.log\n"
        "baseline_suite:\n"
        f"  log_path: {base_arm}/gate.log\n"
        f"  command: pytest --basetemp={base_arm} tests/\n"
        "after_suite:\n"
        f"  log_path: {control_tree}/control.log\n"
        f"  command: pytest --basetemp={control_tree} tests/\n",
        encoding="utf-8",
    )
    _pointer(repository, worktree, run_id, manifest)

    promoted = crew.complete(
        run_id,
        gate="failed",
        failure_classification="negative-result",
        outcome="the arm release is not reached while the gate is red",
        completed_at="2026-10-04T14:05:00Z",
        root=repository,
    )

    release = promoted["release"]
    assert base_arm.is_dir()
    assert control_tree.is_dir()
    assert release["arms_removed_paths"] == []
    assert release["arms_logs_copied"] == []
    assert "not passing" in release["arms_withheld"]
    assert not (crew.run_dir(run_id) / "cleared-arms").exists()
