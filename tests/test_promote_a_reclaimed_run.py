"""Promotion of a run whose worktree has been reclaimed.

A worktree is released once its run ends, and the run's commits survive it in
the repository, which shares the object store. Promotion resolved the run's
citations through the worktree path itself, so the first git call whose working
directory no longer existed raised ``FileNotFoundError``, and a run whose
worktree was already gone could not be promoted at all. Promotion now reads a
reclaimed run's commits through its repository, so citations resolve there
instead of failing, while the readings that are about the worktree's own
working state — its ``HEAD``, its uncommitted paths — stay silent rather than
being taken in a tree the run never worked in.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"

# A forty-character hexadecimal value that resolves to no commit object.
_GONE_40 = "0123456789abcdef0123456789abcdef01234567"


def _git(repository: Path, *arguments: Path | str) -> str:
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


def _reclaimed_run(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    manifests: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Set up a run whose worktree is deleted, returning its base and commit.

    The run commits on a detached worktree of the repository and the promotion
    merges that commit, so the work is both the run's own and reachable from
    the integration branch when the worktree directory is taken away.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / f"{run_id}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), base)
    (run_tree / "allowed.txt").write_text("seed\nreclaimed\n", encoding="utf-8")
    _git(run_tree, "add", "allowed.txt")
    _git(run_tree, "commit", "-q", "-m", "test: update declared path")
    commit = _git(run_tree, "rev-parse", "HEAD")
    _git(repository, "merge", "-q", "--no-ff", commit, "-m", "Merge run commit")

    manifest_path = tmp_path / f"{run_id}-manifest.md"
    for name, body in (manifests or {}).items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    manifest_path.write_text(
        "node: reclaimed-fixture\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: allowed.txt\n"
        "tests: pytest tests/example.py -> 1 passed\n",
        encoding="utf-8",
    )
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(run_tree),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-10-03T03:00:00Z",
        "manifest_path": str(manifest_path),
        "node": {
            "id": "reclaimed-fixture",
            "plan": "fixture",
            "section": "reclaimed",
            "time_budget": "25m",
            "write_paths": ["allowed.txt"],
        },
    }
    _write_json(pointer_path(run_id), pointer)

    shutil.rmtree(run_tree)
    _git(repository, "worktree", "prune")
    assert not run_tree.exists()
    return base, commit


def test_a_reclaimed_run_promotes_and_registers_its_commit(
    repository: Path, tmp_path: Path
) -> None:
    """The done-when: the worktree directory is gone and the run still promotes."""
    run_id = "r-reclaimed-promotes"
    _, commit = _reclaimed_run(repository, tmp_path, run_id)

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]


def test_a_reclaimed_run_cites_through_its_repository(
    repository: Path, tmp_path: Path
) -> None:
    """A reclaimed run's citations are measured, not silently skipped.

    The reading is taken through the repository, which is what makes a citation
    that names no object refused: a guard that could not read the run's store
    would accept the fabricated value as unmeasured.
    """
    run_id = "r-reclaimed-cites"
    _, commit = _reclaimed_run(repository, tmp_path, run_id)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit, _GONE_40],
            root=repository,
        )

    assert _GONE_40 in str(refusal.value)
    assert "does not resolve" in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).exists()
