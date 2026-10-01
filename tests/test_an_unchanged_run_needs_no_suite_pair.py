"""An armed run that changed nothing promotes without a suite pair.

An armed promotion requires complete baseline and after suite evidence so its
delta can be calculated. A run whose own manifest and worktree prove it changed
nothing has no delta for the pair to measure, so requiring the pair refuses a
run that is provably unchanged. This module covers the exemption and the two
refusals beside it: a run whose head moved past its base, and a run whose empty
manifest is contradicted by a worktree that moved, are both still refused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-effort-hours" content="4">'
        f"<title>{PLAN}</title></head><body></body></html>"
    )
    _seed_git_repository(root)
    return root


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )


def _seed_git_repository(root: Path) -> None:
    """Make the fixture a git worktree with one commit."""
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
    ):
        _run_git(root, *arguments)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if staged.returncode != 0:
        _run_git(root, "commit", "-q", "-m", "seed repository")


def _head_sha(root: Path) -> str:
    return _run_git(root, "rev-parse", "HEAD").stdout.strip()


def _add_commit_past_base(root: Path) -> str:
    """Commit one further change and return the new head's sha."""
    (root / "docs" / "plans" / "extra.txt").write_text("work beyond the base\n")
    _run_git(root, "add", "docs/plans/extra.txt")
    _run_git(root, "commit", "-q", "-m", "work past the base")
    return _head_sha(root)


def _write_manifest(
    path: Path, *, commits: str = "none", changed_paths: str | None = None
) -> None:
    lines = ["node: node-a", "status: complete", f"commits: {commits}", "tests: done"]
    if changed_paths is not None:
        lines.append(f"changed_paths: {changed_paths}")
    path.write_text("\n".join(lines) + "\n")


def _write_pointer(
    run_id: str,
    repository: Path,
    *,
    base_sha: str,
    manifest_path: str,
    worktree: str | None = None,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-08-26T09:00:00Z",
            "manifest_path": manifest_path,
            "base_sha": base_sha,
            "suite_command": "pytest -q",
            "worktree": worktree if worktree is not None else str(repository),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _stored_review(run_id: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(record)


def _complete(run_id: str, repository: Path):
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--checkout-path",
            str(repository),
            "--gate-command",
            "pytest -q",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/gate.log",
            "--no-commit",
            "declined every finding; no repository change",
        ],
    )


def test_decline_only_run_promotes_with_unchanged_suite_delta(
    repository: Path, tmp_path: Path
) -> None:
    """An armed run that changed nothing promotes with suite_delta unchanged."""
    run_id = "r-20260930T120000000000-node-a"
    base_sha = _head_sha(repository)
    manifest = tmp_path / "decline-only.md"
    _write_manifest(manifest)
    _write_pointer(run_id, repository, base_sha=base_sha, manifest_path=str(manifest))
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "unchanged"
    assert suite_delta["added_failure_ids"] == []
    assert suite_delta["suite_command"] == "pytest -q"
    assert suite_delta["reason"]


def test_run_with_a_commit_past_base_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A manifest naming a commit past the base is refused, not exempted."""
    run_id = "r-20260930T120100000000-node-a"
    base_sha = _head_sha(repository)
    moved = _add_commit_past_base(repository)
    manifest = tmp_path / "commit-past-base.md"
    _write_manifest(manifest, commits=moved)
    _write_pointer(run_id, repository, base_sha=base_sha, manifest_path=str(manifest))
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code != 0
    assert "baseline_suite" in result.output
    assert "after_suite" in result.output
    assert pointer_path(run_id).is_file()


def test_empty_manifest_with_a_reclaimed_worktree_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A worktree that is gone proves nothing, so the pair is still required."""
    run_id = "r-20260930T120300000000-node-a"
    base_sha = _head_sha(repository)
    manifest = tmp_path / "reclaimed-worktree.md"
    _write_manifest(manifest)
    _write_pointer(
        run_id,
        repository,
        base_sha=base_sha,
        manifest_path=str(manifest),
        worktree=str(tmp_path / "reclaimed-worktree"),
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code != 0
    assert "baseline_suite" in result.output
    assert "after_suite" in result.output
    assert pointer_path(run_id).is_file()


def test_empty_manifest_with_an_untracked_deliverable_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """An untracked deliverable beside a clean head is repository work."""
    run_id = "r-20260930T120400000000-node-a"
    base_sha = _head_sha(repository)
    (repository / "docs" / "plans" / "deliverable.txt").write_text(
        "written but never staged\n"
    )
    manifest = tmp_path / "untracked-deliverable.md"
    _write_manifest(manifest)
    _write_pointer(run_id, repository, base_sha=base_sha, manifest_path=str(manifest))
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code != 0
    assert "baseline_suite" in result.output
    assert "after_suite" in result.output
    assert pointer_path(run_id).is_file()


def test_empty_manifest_with_a_moved_worktree_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """An empty manifest is contradicted by a worktree whose head moved."""
    run_id = "r-20260930T120200000000-node-a"
    base_sha = _head_sha(repository)
    _add_commit_past_base(repository)
    manifest = tmp_path / "empty-manifest.md"
    _write_manifest(manifest)
    _write_pointer(run_id, repository, base_sha=base_sha, manifest_path=str(manifest))
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code != 0
    assert "baseline_suite" in result.output
    assert "after_suite" in result.output
    assert pointer_path(run_id).is_file()
