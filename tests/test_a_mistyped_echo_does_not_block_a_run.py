"""Orientation echoes are repaired only after git proves the run's scope."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew.dispatch_sessions import _apply_orientation_check
from reckon.crew.recovery_classification import classify_pointer


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _commit(tree: Path, path: str) -> str:
    (tree / path).write_text("delivered\n")
    _git(tree, "add", path)
    _git(tree, "commit", "-m", "fix: deliver content\n\nRecord the scoped content.")
    return _git(tree, "rev-parse", "HEAD")


@pytest.fixture
def delivered(tmp_path: Path) -> tuple[dict, Path]:
    tree = tmp_path / "repository"
    tree.mkdir()
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "test@example.invalid")
    _git(tree, "config", "user.name", "Test")
    base = _commit(tree, "seed.txt")
    head = _commit(tree, "allowed.txt")
    manifest = tmp_path / "manifest.md"
    record = {
        "run_id": "delivered-run",
        "project": "fixture",
        "launch": "in-harness",
        "task": "completed-task",
        "phase": "complete",
        "measure_only": True,
        "process_alive": False,
        "repo": str(tree),
        "worktree": str(tree),
        "base_sha": base,
        "node": {
            "id": "scoped-delivery",
            "role": "implement",
            "write_paths": ["allowed.txt", "another.txt"],
        },
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
    }
    _report(record, manifest, commits=[head])
    return record, manifest


def _report(record: dict, manifest: Path, **overrides: object) -> None:
    fields = {
        "orientation_worktree": record["worktree"],
        "orientation_base_sha": record["base_sha"],
        "orientation_write_paths": record["node"]["write_paths"],
        "status": "complete",
        **overrides,
    }
    manifest.write_text(
        "".join(
            f"{key}: {json.dumps(value) if isinstance(value, list) else value}\n"
            for key, value in fields.items()
        )
    )


@pytest.mark.parametrize("field", ["base_sha", "write_paths", "worktree"])
def test_an_echo_difference_is_repaired_from_the_run(delivered, field):
    record, manifest = delivered
    values = {
        "base_sha": record["base_sha"][:-1] + "z",
        "write_paths": ["allowed.txt"],
        "worktree": str(manifest.parent / "mistyped-tree"),
    }
    _report(record, manifest, **{f"orientation_{field}": values[field]})
    original = copy.deepcopy(record)
    _apply_orientation_check(record, manifest)
    assert record["phase"] == "complete", record
    assert record["orientation_check"]["matched"] is True
    repair = record["orientation_check"]["repaired_echo"][field]
    assert repair["reported"] == values[field]
    expected = (
        original["node"]["write_paths"] if field == "write_paths" else original[field]
    )
    assert repair["expected"] == expected
    before_classification = copy.deepcopy(original)
    classified = classify_pointer(original)
    assert classified["classification"] == "promotable", classified
    assert classified["orientation_check"]["repaired_echo"][field] == repair
    assert original == before_classification, (
        "classification must not mutate the pointer"
    )


def test_an_outside_commit_stays_blocked_and_names_its_path(delivered):
    record, manifest = delivered
    head = _commit(Path(record["worktree"]), "outside.txt")
    _report(record, manifest, commits=[head], orientation_write_paths=["allowed.txt"])
    original = copy.deepcopy(record)
    _apply_orientation_check(record, manifest)
    assert record["phase"] == "blocked", record
    assert "outside.txt" in record["detail"]
    classified = classify_pointer(original)
    assert classified["classification"] == "blocked", classified
    assert "outside.txt" in classified["detail"]


def test_a_nonancestor_pointer_base_stays_blocked(delivered):
    record, manifest = delivered
    tree = Path(record["worktree"])
    unrelated = _git(tree, "commit-tree", "HEAD^{tree}", "-m", "unrelated root")
    record["base_sha"] = unrelated
    _report(record, manifest)
    _apply_orientation_check(record, manifest)
    assert record["phase"] == "blocked", record
    assert "ancestor" in record["detail"]


def test_a_commit_from_another_worktree_stays_blocked(delivered, tmp_path):
    record, manifest = delivered
    tree = Path(record["worktree"])
    foreign = tmp_path / "foreign-tree"
    _git(tree, "worktree", "add", "--detach", str(foreign), record["base_sha"])
    try:
        head = _commit(foreign, "allowed.txt")
        # Make the foreign object distinct from the run's own commit.
        _git(foreign, "commit", "--allow-empty", "-m", "foreign work")
        head = _git(foreign, "rev-parse", "HEAD")
        _report(record, manifest, commits=[head], orientation_worktree=str(foreign))
        _apply_orientation_check(record, manifest)
        assert record["phase"] == "blocked", record
        assert "worktree" in record["detail"]
        assert head in record["detail"]
    finally:
        _git(tree, "worktree", "remove", str(foreign))


def test_a_stored_echo_refusal_is_rechecked(delivered):
    record, manifest = delivered
    record.update(
        phase="blocked",
        detail="orientation mismatch: base_sha",
        orientation_check={"matched": False, "mismatches": ["base_sha"]},
    )
    _report(record, manifest, orientation_base_sha="mistyped")
    _apply_orientation_check(record, manifest)
    assert record["phase"] == "complete", record
    assert "base_sha" in record["orientation_check"]["repaired_echo"]


def test_a_matching_echo_does_not_hide_a_later_scope_escape(delivered):
    record, manifest = delivered
    _apply_orientation_check(record, manifest)
    assert record["orientation_check"]["matched"] is True
    _commit(Path(record["worktree"]), "outside.txt")
    _apply_orientation_check(record, manifest)
    assert record["phase"] == "blocked", record
    assert "outside.txt" in record["detail"]


def test_an_outside_path_added_then_deleted_still_counts(delivered):
    record, manifest = delivered
    tree = Path(record["worktree"])
    _commit(tree, "outside.txt")
    _git(tree, "rm", "outside.txt")
    _git(tree, "commit", "-m", "fix: remove temporary content\n\nRemove the content.")
    _apply_orientation_check(record, manifest)
    assert record["phase"] == "blocked", record
    assert "outside.txt" in record["detail"]
