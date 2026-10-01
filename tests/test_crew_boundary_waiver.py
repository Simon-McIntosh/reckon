"""The repository-tree boundary refusal accepts a reasoned override."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing
from reckon.crew.promotion import BOUNDARY_REFERENT_MAX_AGE_SECONDS
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


def _stamp(*, before_now: timedelta) -> str:
    """Return a UTC stamp the given interval before the current clock."""
    moment = datetime.now(tz=UTC) - before_now
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _pointer(
    repository: Path, run_id: str, base: str, *, created_at: str = ""
) -> None:
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
            "created_at": created_at or _stamp(before_now=timedelta(hours=1)),
            "node": {
                "id": "boundary-check",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": ["allowed.txt"],
            },
        },
    )


def _guarded_pointer(
    repository: Path, run_tree: Path, run_id: str, base: str, *, created_at: str = ""
) -> None:
    _pointer(repository, run_id, base, created_at=created_at)
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
    assert "no_referent" not in stored["boundary_waiver"]
    assert not pointer_path(run_id).exists()
    assert ledger.runs(PROJECT, root=repository)[0]["boundary_waiver"] == {
        "reason": "known-wrong verdict, fix incoming",
        "waived_paths": [expected_violation],
    }


def test_boundary_waiver_past_the_age_bound_names_no_referent(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-tree-stale"
    stale = _stamp(
        before_now=timedelta(seconds=BOUNDARY_REFERENT_MAX_AGE_SECONDS + 24 * 3600)
    )
    _guarded_pointer(repository, run_tree, run_id, base, created_at=stale)
    commit = _commit_allowed(run_tree)
    (repository / "allowed.txt").write_text("stray\n", encoding="utf-8")
    expected_violation = f"allowed.txt in main checkout {repository}"

    stored = crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        root=repository,
        boundary_waiver="accepting the boundary could not be checked",
    )["record"]

    waiver = stored["boundary_waiver"]
    assert waiver["reason"] == "accepting the boundary could not be checked"
    assert waiver["waived_paths"] == [expected_violation]
    assert "no_referent" in waiver
    assert "older than the" in waiver["no_referent"]


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
