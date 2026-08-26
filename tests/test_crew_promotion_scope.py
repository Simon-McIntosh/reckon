"""Promotion scope and cumulative-diff guards."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
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
                "id": "scope-check",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": ["allowed.txt"],
            },
        },
    )


def test_two_commit_scope_escape_is_refused_before_ledger_write(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("seed\nallowed\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: update declared path")
    first = _git(repository, "rev-parse", "HEAD")
    (repository / "outside.txt").write_text("undeclared\n", encoding="utf-8")
    _git(repository, "add", "outside.txt")
    _git(repository, "commit", "-q", "-m", "test: update undeclared path")
    tip = _git(repository, "rev-parse", "HEAD")
    run_id = "r-scope-escape"
    _pointer(repository, run_id, base)

    with pytest.raises(crew.CrewError, match=r"outside\.txt"):
        crew.complete(run_id, gate="passed", commits=[first, tip], root=repository)

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_stored_line_counts_cover_the_cumulative_run_commit_span(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("seed\nfirst\nsecond\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: add declared lines")
    first = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text(
        "seed\nfirst\nsecond\nthird\n", encoding="utf-8"
    )
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: extend declared lines")
    tip = _git(repository, "rev-parse", "HEAD")
    run_id = "r-cumulative-stat"
    _pointer(repository, run_id, base)

    stored = crew.complete(
        run_id, gate="passed", commits=[first, tip], root=repository
    )["record"]

    assert stored["changed_lines"] == {"added": 3, "removed": 0, "files": 1}


def test_primary_advance_before_first_run_commit_is_outside_the_measured_span(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "primary.txt").write_text("advanced\n", encoding="utf-8")
    _git(repository, "add", "primary.txt")
    _git(repository, "commit", "-q", "-m", "test: advance primary")
    (repository / "allowed.txt").write_text("seed\nworker\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: update declared path")
    commit = _git(repository, "rev-parse", "HEAD")
    run_id = "r-advanced-primary"
    _pointer(repository, run_id, base)

    stored = crew.complete(
        run_id, gate="passed", commits=[commit], root=repository
    )["record"]

    assert stored["changed_lines"] == {"added": 1, "removed": 0, "files": 1}
    assert stored["commits"] == [commit]
