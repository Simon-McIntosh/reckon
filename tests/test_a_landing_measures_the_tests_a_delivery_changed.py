"""A landing whose node names no test file measures the tests it delivered.

A batch entry takes its write scope from section prose, which often omits the
test the done-when asks for. The worker still writes that test, but the lander
read only the fenced scope, found no test file and refused with "the armed
command measures the node's own test files, and the node names none". The
delivery's own changed test files are measured instead.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.queue_landing import _landing_test_files


def _pointer(write_paths: list[str], tree: Path) -> dict:
    return {"worktree": str(tree), "node": {"write_paths": write_paths}}


def _tree(tmp_path: Path, *files: str) -> Path:
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


def test_the_node_s_named_tests_are_measured_when_it_names_them(
    tmp_path: Path,
) -> None:
    tree = _tree(tmp_path, "tests/test_named.py", "tests/test_other.py")
    pointer = _pointer(["reckon/thing.py", "tests/test_named.py"], tree)

    files = _landing_test_files(
        pointer, ["reckon/thing.py", "tests/test_other.py"], tree
    )

    assert files == ["tests/test_named.py"]


def test_a_node_naming_no_test_measures_the_tests_it_changed(tmp_path: Path) -> None:
    tree = _tree(tmp_path, "tests/test_delivered.py")
    pointer = _pointer(["reckon/thing.py"], tree)

    files = _landing_test_files(
        pointer, ["reckon/thing.py", "tests/test_delivered.py", "docs/note.html"], tree
    )

    assert files == ["tests/test_delivered.py"]


def test_a_deleted_test_is_not_passed_to_the_runner(tmp_path: Path) -> None:
    tree = _tree(tmp_path, "tests/test_kept.py")
    pointer = _pointer(["reckon/thing.py"], tree)

    files = _landing_test_files(
        pointer, ["tests/test_kept.py", "tests/test_removed.py"], tree
    )

    assert files == ["tests/test_kept.py"]
