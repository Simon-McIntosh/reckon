"""The repository-tree boundary refusal accepts a reasoned override."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(repository: Path, run_id: str, base: str) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-08-26T12:00:00Z",
            "node": {
                "id": "boundary-check",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": ["allowed.txt"],
            },
        },
    )


def _guarded_pointer(repository: Path, run_tree: Path, run_id: str, base: str) -> None:
    _pointer(repository, run_id, base)
    pointer = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    pointer["worktree"] = str(run_tree)
    pointer["repository_tree_snapshot"] = routing._repository_tree_snapshot(repository)
    _write_json(pointer_path(run_id), pointer)


def _detached_tree(repository: Path, path: Path) -> Path:
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _commit_allowed(run_tree: Path) -> str:
    (run_tree / "allowed.txt").write_text("seed\nallowed\n", encoding="utf-8")
    _git(run_tree, "add", "allowed.txt")
    _git(run_tree, "commit", "-q", "-m", "test: update declared path")
    return _git(run_tree, "rev-parse", "HEAD")


def test_boundary_refusal_promotes_under_waiver_with_reason_and_paths(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-tree-waived"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    (repository / "allowed.txt").write_text("stray\n", encoding="utf-8")
    expected_violation = f"allowed.txt in main checkout {repository}"

    stored = crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        root=repository,
        boundary_waiver="known-wrong verdict, fix incoming",
    )["record"]

    assert stored["commits"] == [commit]
    assert stored["boundary_waiver"] == {
        "reason": "known-wrong verdict, fix incoming",
        "waived_paths": [expected_violation],
    }
    assert not pointer_path(run_id).exists()
    assert ledger.runs(PROJECT, root=repository)[0]["boundary_waiver"] == {
        "reason": "known-wrong verdict, fix incoming",
        "waived_paths": [expected_violation],
    }


def test_boundary_refusal_without_waiver_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-tree-unwaived"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    (repository / "allowed.txt").write_text("stray\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "allowed.txt" in message
    assert f"main checkout {repository}" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_boundary_waiver_on_a_clean_run_is_refused_naming_nothing_waived(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-tree-clean-waiver"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit],
            root=repository,
            boundary_waiver="nothing to see here",
        )

    message = str(refusal.value)
    assert "no repository-tree boundary violation" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
