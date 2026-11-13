"""A boundary check reads only the roots a run can be charged for.

A promotion's repository-boundary check compares the status of every
dispatch-visible tree against the baseline its dispatch recorded, and charges a
changed path only when the run's own declaration covers it. The untracked half
of that read is the expensive half: ``git status --untracked-files=all`` lists
every file under every untracked directory, and a shared checkout keeps its
worktree, cache and tool trees — none of them declared by any run — inside it,
so an unrestricted read spends one directory walk per untracked file on the
filesystem and charges nothing for any of them.

These tests show the read is bounded to the declared roots, that a file at a
declared path under an untracked directory is still charged, and that a
checkout whose untracked tree holds 20,000 files outside every declared root
costs no more than one holding none. Every absence claim carries its positive
control: the same snapshot taken without the bound must see the very entries
the bounded one omits.
"""

from __future__ import annotations

import importlib
import json
import statistics
import time
from pathlib import Path

import pytest

from reckon.crew import promotion
from reckon.crew.node import CrewError
from reckon.crew.routing import _repository_tree_snapshot
from reckon.crew.runs import run_dir
from tests.helpers.git import git as _git

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "sample"
UNTRACKED_FILES = 20_000
TIMING_RUNS = 5


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private configuration home, so no live run or ledger is ever read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _make_repository(base: Path) -> Path:
    """A throwaway checkout with one tracked file at a declared path."""
    root = base / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "shared-write-paths.json").write_text(
        json.dumps({"shared_roots": []}), encoding="utf-8"
    )
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt", "docs/state/sample/shared-write-paths.json"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    return root


def _add_run_worktree(repository: Path, base: Path) -> Path:
    """A registered worktree registered for the run, distinct from the checkout."""
    run_tree = base / "trees" / "run"
    run_tree.parent.mkdir(parents=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    return run_tree


def _record(
    repository: Path, run_tree: Path, run_id: str, *, write_paths: list[str]
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(run_tree),
        "base_sha": _git(repository, "rev-parse", "HEAD"),
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "fenced": False,
        "created_at": "2026-09-29T12:00:00Z",
        "node": {
            "id": "boundary-check",
            "plan": "fixture",
            "section": "s1",
            "time_budget": "25m",
            "write_paths": write_paths,
        },
    }


def _write_baseline(repository: Path, run_tree: Path, run_id: str) -> None:
    """Record the boundary baseline the way dispatch records it."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    dispatch_module._write_boundary_tree_snapshot(
        directory, repository, worktree=run_tree, fenced=False
    )


def _status_paths(snapshot: dict[str, object]) -> set[str]:
    return {
        str(entry.get("path") or "")
        for tree in snapshot["trees"]  # type: ignore[index]
        for entry in tree.get("status_entries") or ()
    }


def _fill_untracked_tree(root: Path, count: int) -> None:
    """One untracked directory holding ``count`` files, one per subdirectory.

    A file per subdirectory is what a cache or worktree tree looks like on a
    real checkout, and it makes the walk pay for a directory read as well as a
    file stat, so the measurement discriminates a bounded read from an
    unbounded one rather than timing two equally shallow scans.
    """
    for index in range(count):
        directory = root / f"d{index:05d}"
        directory.mkdir()
        (directory / "f.txt").write_text("x", encoding="utf-8")


def _time_boundary_check(
    run_id: str, record: dict[str, object]
) -> tuple[float, list[str]]:
    """The median of five boundary checks, and the violations the last found."""
    durations: list[float] = []
    violations: list[str] = []
    for _ in range(TIMING_RUNS):
        started = time.perf_counter()
        violations = promotion._repository_tree_boundary_violations(run_id, record)
        durations.append(time.perf_counter() - started)
    return statistics.median(durations), violations


def test_an_untracked_tree_outside_the_declared_roots_costs_an_empty_one(
    home: Path, tmp_path: Path
) -> None:
    """20,000 untracked files no run declares cost the check nothing.

    Both measurements run against one checkout, one recorded baseline and one
    run record; only the contents of the untracked directory change between
    them. Taking the baseline while that directory is empty is the case the
    check must not pay for — the tree grows between the dispatch that recorded
    the baseline and the promotion that re-reads it — and comparing within one
    checkout keeps the two figures from differing for any reason but the one
    under test.
    """
    repository = _make_repository(tmp_path)
    run_tree = _add_run_worktree(repository, tmp_path)
    run_id = "r-untracked-tree"
    untracked = repository / "bulk"
    untracked.mkdir()
    _write_baseline(repository, run_tree, run_id)
    record = _record(repository, run_tree, run_id, write_paths=["allowed.txt"])

    empty_seconds, empty_violations = _time_boundary_check(run_id, record)
    _fill_untracked_tree(untracked, UNTRACKED_FILES)
    bulk_seconds, bulk_violations = _time_boundary_check(run_id, record)

    assert (untracked / "d00000" / "f.txt").is_file()
    assert len(list(untracked.iterdir())) == UNTRACKED_FILES
    assert empty_violations == []
    assert bulk_violations == []
    assert bulk_seconds <= 2 * empty_seconds


def test_untracked_entries_outside_the_declared_roots_are_not_read(
    home: Path, tmp_path: Path
) -> None:
    """The bounded read omits what the unbounded one reports, and keeps its own."""
    repository = _make_repository(tmp_path)
    (repository / "bulk").mkdir()
    (repository / "bulk" / "one.txt").write_text("x", encoding="utf-8")
    (repository / "fixtures").mkdir()
    (repository / "fixtures" / "kept.txt").write_text("x", encoding="utf-8")

    bounded = _repository_tree_snapshot(
        repository, roots=[repository], untracked_roots=(Path("fixtures"),)
    )
    bounded_paths = _status_paths(bounded)

    unrestricted = _repository_tree_snapshot(repository, roots=[repository])
    unrestricted_paths = _status_paths(unrestricted)

    assert "fixtures/kept.txt" in bounded_paths
    assert not any(path.startswith("bulk/") for path in bounded_paths)
    assert "bulk/one.txt" not in bounded_paths
    # Positive control: the same reader does see the directory when nothing
    # bounds it, so the absence above is the bound rather than a blind read.
    assert "bulk/one.txt" in unrestricted_paths
    assert "fixtures/kept.txt" in unrestricted_paths


def test_a_run_declaring_no_root_reads_no_untracked_entry(
    home: Path, tmp_path: Path
) -> None:
    """Nothing can be charged to a run that declares nothing, so nothing is read."""
    repository = _make_repository(tmp_path)
    (repository / "bulk").mkdir()
    (repository / "bulk" / "one.txt").write_text("x", encoding="utf-8")

    bounded = _repository_tree_snapshot(
        repository, roots=[repository], untracked_roots=()
    )

    assert all(not tree["status_entries"] for tree in bounded["trees"])
    unrestricted = _repository_tree_snapshot(repository, roots=[repository])
    assert "bulk/one.txt" in _status_paths(unrestricted)


def test_a_stray_at_a_declared_path_inside_an_untracked_directory_is_refused(
    home: Path, tmp_path: Path
) -> None:
    """The bound keeps a declared path, even under an untracked directory."""
    repository = _make_repository(tmp_path)
    run_tree = _add_run_worktree(repository, tmp_path)
    fixtures = repository / "fixtures"
    fixtures.mkdir()
    (fixtures / "kept.txt").write_text("kept\n", encoding="utf-8")

    run_id = "r-untracked-declared"
    _write_baseline(repository, run_tree, run_id)
    (fixtures / "stray.txt").write_text("stray\n", encoding="utf-8")
    record = _record(repository, run_tree, run_id, write_paths=["fixtures"])

    with pytest.raises(CrewError) as refusal:
        promotion._require_repository_tree_boundary(run_id, record)

    assert "fixtures/stray.txt" in str(refusal.value)


def test_a_tracked_change_at_a_declared_path_is_still_refused(
    home: Path, tmp_path: Path
) -> None:
    """The bound narrows the read; it does not stop reporting a declared path."""
    repository = _make_repository(tmp_path)
    run_tree = _add_run_worktree(repository, tmp_path)

    run_id = "r-tracked-declared"
    _write_baseline(repository, run_tree, run_id)
    (repository / "allowed.txt").write_text("stray\n", encoding="utf-8")
    record = _record(repository, run_tree, run_id, write_paths=["allowed.txt"])

    with pytest.raises(CrewError) as refusal:
        promotion._require_repository_tree_boundary(run_id, record)

    assert "allowed.txt" in str(refusal.value)


def test_a_change_outside_every_declared_root_is_not_refused(
    home: Path, tmp_path: Path
) -> None:
    """An undeclared path was never chargeable, and is still not charged."""
    repository = _make_repository(tmp_path)
    run_tree = _add_run_worktree(repository, tmp_path)

    run_id = "r-undeclared-stray"
    _write_baseline(repository, run_tree, run_id)
    (repository / "scratch.txt").write_text("stray\n", encoding="utf-8")
    record = _record(repository, run_tree, run_id, write_paths=["allowed.txt"])

    assert promotion._repository_tree_boundary_violations(run_id, record) == []
