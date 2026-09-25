"""A promotion asserts only what the run made beyond its recorded base.

Measured 2026-09-25: a citation of a run's own base, or of any commit behind
it, satisfies every ancestry question a later sweep asks of the row, so a run
whose work was never committed reads as landed. The same hole opens without a
citation at all: a commitless promotion over a worktree still holding the run's
declared work records the run as complete and then releases the tree.

Both states are refused before any store is written, so the refusal leaves no
ledger row, no promotion commit and no pointer removed. The comparison is made
in the run's own tree, and the declaration it charges dirt to is the run's own
— a shared checkout and a fleet worktree both carry other sessions' work.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _real_pointer_dir() -> Path:
    return Path.home() / ".config" / "reckon" / "crew" / "live"


def _assert_real_home_carries_no_pointer(run_id: str) -> None:
    """The synthesised run must never reach the operator's own config home.

    A reader can only be wrong; a writer makes someone else wrong, and a test
    that writes its live pointer into the real pointer directory collides with
    concurrent runs. This asserts the write direction, not just the read.
    """
    assert not (_real_pointer_dir() / f"{run_id}.json").exists()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "in_scope.txt").write_text("seed\n", encoding="utf-8")
    (root / "earlier.txt").write_text("earlier\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "earlier.txt"),
        ("commit", "-q", "-m", "chore: seed earlier"),
        ("add", "in_scope.txt"),
        ("commit", "-q", "-m", "chore: seed scope"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_tree: Path,
    run_id: str,
    base: str,
    *,
    write_paths: tuple[str, ...] = ("in_scope.txt",),
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(run_tree),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-09-25T12:00:00Z",
            "node": {
                "id": "base-citation",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": list(write_paths),
            },
        },
    )


def _run_tree(repository: Path, tmp_path: Path, name: str) -> Path:
    tree = tmp_path / name
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _commits_ahead(tree: Path, base: str) -> int:
    return int(_git(tree, "rev-list", "--count", f"{base}..HEAD"))


def test_a_citation_of_the_run_base_is_refused_without_a_ledger_row(
    repository: Path, tmp_path: Path
) -> None:
    """The base itself is not the run's work, however well it resolves."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, "run-base-citation")
    run_id = "r-cites-its-base"
    _pointer(repository, tree, run_id, base)
    head_before = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[base], root=repository)

    message = str(refusal.value)
    assert base in message, "the cited sha is named"
    assert "base" in message, "the base it was measured against is named"
    assert ledger.runs(PROJECT, root=repository) == []
    assert _git(repository, "rev-parse", "HEAD") == head_before
    assert _commits_ahead(repository, head_before) == 0
    assert not (repository / "docs" / "state" / PROJECT / "runs").exists()
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_citation_behind_the_base_is_refused_as_not_the_runs_own(
    repository: Path, tmp_path: Path
) -> None:
    """A commit the base already contains belongs to whoever made it."""
    base = _git(repository, "rev-parse", "HEAD")
    earlier = _git(repository, "rev-parse", "HEAD~1")
    tree = _run_tree(repository, tmp_path, "run-earlier-citation")
    run_id = "r-cites-earlier"
    _pointer(repository, tree, run_id, base)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[earlier], root=repository)

    message = str(refusal.value)
    assert earlier in message
    assert base in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_an_abbreviated_citation_of_the_base_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Equality is judged on the resolved commit, not on the spelling."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, "run-abbreviated-citation")
    run_id = "r-cites-abbreviated-base"
    _pointer(repository, tree, run_id, base)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[base[:12]], root=repository)

    message = str(refusal.value)
    assert base in message
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_commitless_run_holding_its_declared_work_is_refused_naming_it(
    repository: Path, tmp_path: Path
) -> None:
    """Work in the tree is not work on the record, and release takes the tree."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, "run-uncommitted")
    run_id = "r-holds-uncommitted-work"
    _pointer(repository, tree, run_id, base)
    (tree / "in_scope.txt").write_text("seed\ndelivered\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    message = str(refusal.value)
    assert "in_scope.txt" in message, "the uncommitted path is named"
    assert ledger.runs(PROJECT, root=repository) == []
    assert (tree / "in_scope.txt").read_text(encoding="utf-8") == "seed\ndelivered\n"
    assert tree.is_dir(), "the refusal leaves the work where it is"
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_an_undeclared_stray_change_is_not_charged_to_the_run(
    repository: Path, tmp_path: Path
) -> None:
    """A shared tree carries other sessions' work; only the run's is its own."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, "run-stray")
    run_id = "r-carries-a-stray"
    _pointer(repository, tree, run_id, base, write_paths=("in_scope.txt",))
    (tree / "stray_peer_note.txt").write_text("someone else's\n", encoding="utf-8")

    stored = crew.complete(run_id, gate="passed", root=repository)["record"]

    assert stored["run_id"] == run_id
    assert stored["commits"] == []
    assert pointer_path(run_id).exists() is False
    assert (tree / "stray_peer_note.txt").read_text(encoding="utf-8") == (
        "someone else's\n"
    )
    _assert_real_home_carries_no_pointer(run_id)


def test_a_citation_beyond_the_base_still_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The ordinary case is unchanged: the run's own commit lands."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, "run-normal")
    run_id = "r-committed-beyond-base"
    _pointer(repository, tree, run_id, base)
    (tree / "in_scope.txt").write_text("seed\ndelivered\n", encoding="utf-8")
    _git(tree, "add", "in_scope.txt")
    _git(tree, "commit", "-q", "-m", "fix(scope): deliver the in-scope file")
    commit = _git(tree, "rev-parse", "HEAD")

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert stored["base_sha"] == base
    assert stored["promoted_revision"] == commit
    assert not pointer_path(run_id).exists()
    assert ledger.runs(PROJECT, root=repository)
    _assert_real_home_carries_no_pointer(run_id)
