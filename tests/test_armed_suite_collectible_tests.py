"""An armed suite command names only the files pytest can collect.

A node's fenced write scope names fixtures, helpers and conftest modules under
``tests/`` beside the checks themselves, because all of them are part of the
check it wrote. The suite command hands every named path to pytest, so a
fixture in that list makes pytest exit on a usage error and the arm then reads
as one that collected nothing, while a command narrowed by hand is refused as
not matching the command the run was armed with.

One predicate decides which paths are collectible, and every consumer that
turns write or changed paths into pytest arguments reads it: the resolver that
arms the command, the landing's fallback over a delivery's changed paths, and
the worker-facing sentence a worker resolves its own command from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from reckon.crew.dispatch import _suite_gate_prompt_body
from reckon.crew.node import is_collectible_test_path, is_test_path
from reckon.crew.promotion_records import (
    _node_test_files,
    _scoped_suite_command,
)
from reckon.crew.queue_landing import _landing_test_files

SCOPED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
FIXTURE = "tests/fixtures/data.jsonl"
MIXED_SCOPE = [
    "tests/test_x.py",
    FIXTURE,
    "tests/conftest.py",
    "tests/helpers.py",
    "tests/x.test.py",
]


def _committed_tree(tmp_path: Path, *files: str) -> tuple[Path, str]:
    """A git tree holding ``files`` at its only revision."""
    tree = tmp_path / "repo"
    for name in files:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("add", "tests"),
    ):
        subprocess.run(["git", "-C", str(tree), *arguments], check=True)
    subprocess.run(["git", "-C", str(tree), "commit", "-q", "-m", "seed"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tree, head.stdout.strip()


def _mixed_record(tree: str) -> dict[str, object]:
    return {
        "worktree": tree,
        "suite_command": SCOPED,
        "node": {"write_paths": MIXED_SCOPE},
    }


def test_a_mixed_scope_names_only_the_test_pytest_collects() -> None:
    """The fixture, the conftest and the helper are named by the scope, not run."""
    record = _mixed_record("/repo")

    assert _node_test_files(record) == ["tests/test_x.py"]
    assert _scoped_suite_command(record) == SCOPED.replace(
        "{node_test_files}", "tests/test_x.py"
    )


def test_a_fixture_only_scope_arms_no_node_test_file() -> None:
    """A fixture is still part of a check, but it is nothing pytest can run."""
    record = {
        "worktree": "/repo",
        "suite_command": SCOPED,
        "node": {"write_paths": [FIXTURE]},
    }

    assert _node_test_files(record) == []
    assert _scoped_suite_command(record) == ""


def test_a_fixture_only_scope_still_measures_the_declared_census() -> None:
    """A product-scope node runs the census even with no test of its own."""
    record = {
        "worktree": "/repo",
        "suite_command": SCOPED,
        "census_tests": ["tests/test_inventory.py"],
        "node": {"write_paths": [FIXTURE]},
    }

    command = _scoped_suite_command(record)

    assert command.endswith("tests/test_inventory.py")
    assert FIXTURE not in command


def test_the_armed_command_and_the_compared_command_agree(tmp_path: Path) -> None:
    """Promotion compares each arm with the template resolved over its tree."""
    tree, head = _committed_tree(tmp_path, "tests/test_x.py", FIXTURE)
    record = _mixed_record(str(tree))

    armed = _scoped_suite_command(record, revision=head)
    compared = _scoped_suite_command(record)

    assert armed == compared
    assert armed == SCOPED.replace("{node_test_files}", "tests/test_x.py")
    assert FIXTURE not in armed


def test_a_landing_that_changed_only_the_fixture_names_nothing(
    tmp_path: Path,
) -> None:
    """The landing's fallback must not re-admit what the resolver narrowed out."""
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / FIXTURE).write_text("", encoding="utf-8")
    pointer = {"worktree": str(tmp_path), "node": {"write_paths": [FIXTURE]}}

    assert _landing_test_files(pointer, [FIXTURE], tmp_path) == []


def test_a_landing_names_the_collected_test_beside_the_fixture(
    tmp_path: Path,
) -> None:
    """A delivery touching both measures its test and leaves the fixture out."""
    for name in (FIXTURE, "tests/test_y.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    pointer = {"worktree": str(tmp_path), "node": {"write_paths": ["reckon/thing.py"]}}

    files = _landing_test_files(
        pointer, ["reckon/thing.py", FIXTURE, "tests/test_y.py"], tmp_path
    )

    assert files == ["tests/test_y.py"]


def test_the_dispatcher_still_reads_the_fixture_as_part_of_a_check() -> None:
    """The negative-control requirement is unchanged: a fixture is still a check."""
    assert is_test_path(FIXTURE) is True

    assert is_collectible_test_path(FIXTURE) is False
    assert is_collectible_test_path("tests/conftest.py") is False
    assert is_collectible_test_path("tests/helpers.py") is False
    assert is_collectible_test_path("tests/x.test.py") is False
    assert is_collectible_test_path("tests/test_x.py") is True
    assert is_collectible_test_path("tests/body_test.py") is True


def test_the_worker_facing_rule_states_the_collection_narrowing() -> None:
    """A worker resolves the placeholder from this sentence, so it must agree."""
    resolved = SCOPED.replace("{node_test_files}", "tests/test_x.py")

    body = _suite_gate_prompt_body(SCOPED, resolved)

    assert "pytest collects" in body
    assert "`test_*.py`" in body
    assert "`*_test.py`" in body
    assert resolved in body
