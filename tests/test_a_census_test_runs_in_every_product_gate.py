"""A census test runs in every product node's scoped gate.

A scoped suite command is drawn from a node's own fenced test paths, so it can
never reach a check over the whole tree. A node that adds a staged writer runs
only the tests its own paths cover and the census turns red at the next node
that runs it. A project therefore declares its census tests in its flight file,
and a node writing product source measures them beside its own tests, at both
arms.

A node writing only documents and plans measures no census, and a project
declaring none resolves exactly as it did before census tests existed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion_records
from reckon.crew.promotion_records import _scoped_suite_command

SCOPED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
CENSUS = "tests/test_staged_write_inventory.py"
STAGED_WRITER_FAILURE = (
    "tests/test_staged_write_inventory.py::test_every_staged_write_reaches_the_owner"
)


def _resolved(*files: str) -> str:
    return SCOPED.replace("{node_test_files}", " ".join(files))


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _two_commit_tree(root: Path, paths: tuple[str, ...]) -> tuple[str, str]:
    """A repository whose base and head both hold ``paths``."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    _git(root, "add", *paths)
    _git(root, "commit", "-q", "-m", "base tree")
    base = _git(root, "rev-parse", "HEAD")
    marker = root / "README"
    marker.write_text("head\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "head tree")
    head = _git(root, "rev-parse", "HEAD")
    return base, head


def test_a_product_node_measures_the_census_at_base_and_head(tmp_path: Path) -> None:
    """Case (a): the census test is in the resolved command at both arms.

    The census file exists in both trees because neither arm adds it; the node
    adds a staged writer, and that is exactly the change the census must see.
    """
    repository = tmp_path / "repo"
    base, head = _two_commit_tree(repository, (CENSUS, "reckon/crew/example.py"))
    record = {
        "suite_command": SCOPED,
        "census_tests": [CENSUS],
        "worktree": str(repository),
        "node": {"write_paths": ["reckon/crew/example.py"]},
    }

    for revision in (base, head):
        resolved = _scoped_suite_command(record, revision=revision)
        assert resolved == _resolved(CENSUS), revision


def test_a_docs_only_node_measures_no_census(tmp_path: Path) -> None:
    """Case (b): a node writing only documents and plans runs no census."""
    repository = tmp_path / "repo"
    _two_commit_tree(repository, (CENSUS, "docs/evidence/fragments/plan/node.html"))
    record = {
        "suite_command": SCOPED,
        "census_tests": [CENSUS],
        "worktree": str(repository),
        "node": {"write_paths": ["docs/evidence/fragments/plan/node.html"]},
    }

    assert _scoped_suite_command(record) == ""


def test_a_project_declaring_no_census_resolves_as_before() -> None:
    """Case (c): no declaration leaves the scoped resolution untouched."""
    product_only = {
        "suite_command": SCOPED,
        "worktree": "/repo",
        "node": {"write_paths": ["reckon/crew/example.py"]},
    }
    assert _scoped_suite_command(product_only) == ""

    with_test = {
        "suite_command": SCOPED,
        "worktree": "/repo",
        "node": {"write_paths": ["reckon/crew/example.py", "tests/test_one.py"]},
    }
    assert _scoped_suite_command(with_test) == _resolved("tests/test_one.py")


def _observation(revision: str, command: str, failure_ids: list[str]):
    return {
        "revision": revision,
        "command": command,
        "exit_status": 1 if failure_ids else 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _armed_record(tmp_path: Path, baseline, after) -> dict[str, object]:
    manifest = tmp_path / "manifest.md"
    lines = [
        "node: node-a",
        "status: complete",
        "commits: abc123",
        "tests: done",
        "baseline_suite: " + json.dumps(baseline),
        "after_suite: " + json.dumps(after),
    ]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "run_id": "r-census-arm",
        "suite_command": SCOPED,
        "census_tests": [CENSUS],
        "worktree": None,
        "base_sha": "base-abc",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {"write_paths": ["reckon/crew/example.py"]},
    }


def test_a_head_that_adds_an_unlisted_writer_is_refused(tmp_path: Path) -> None:
    """Case (d), added half: the census fails only at the head."""
    command = _resolved(CENSUS)
    record = _armed_record(
        tmp_path,
        _observation("base-abc", command, []),
        _observation("after-abc", command, [STAGED_WRITER_FAILURE]),
    )

    with pytest.raises(promotion_records.ledger.SuiteDeltaError) as raised:
        promotion_records._evaluate_suite_delta(
            "r-census-arm", record, waiver_reason="", dry_run=True
        )

    assert raised.value.added_failure_ids == [STAGED_WRITER_FAILURE]


def test_a_census_already_red_at_base_adds_no_failure(tmp_path: Path) -> None:
    """Case (d), unchanged half: a red base and the same head is no delta."""
    command = _resolved(CENSUS)
    record = _armed_record(
        tmp_path,
        _observation("base-abc", command, [STAGED_WRITER_FAILURE]),
        _observation("after-abc", command, [STAGED_WRITER_FAILURE]),
    )

    delta = promotion_records._evaluate_suite_delta(
        "r-census-arm", record, waiver_reason="", dry_run=True
    )

    assert delta is not None
    assert delta["added_failure_ids"] == []
    assert delta["status"] == "clean"
