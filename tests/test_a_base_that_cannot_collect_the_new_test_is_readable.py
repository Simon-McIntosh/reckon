"""A base arm that cannot collect the node's new test is a reading, not a gap.

A node whose new test imports a symbol the node adds cannot have that test
collected at the base revision: pytest exits 2 and names the file on an
``ERROR`` line. That is the expected base result, and the lander must read it
as the base failing that id rather than refuse with "the base arm did not run".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from reckon.crew import landing_measure
from reckon.crew.landing_measure import Arm

NEW_TEST = "tests/test_new_symbol.py"
COMMAND = "PYTHONPATH=. .venv/bin/python -m pytest -q {node_test_files}"

COLLECTION_ERROR = (
    "==================================== ERRORS ====================================\n"
    f"_______ ERROR collecting {NEW_TEST} ________\n"
    "E   AttributeError: module 'package' has no attribute 'NEW_SYMBOL'\n"
    "=========================== short test summary info ============================\n"
    f"ERROR {NEW_TEST} - AttributeError: module...\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
)


def test_an_exit_two_arm_that_names_its_ids_is_readable() -> None:
    assert Arm(("pytest",), 2, (NEW_TEST,)).readable


def test_an_exit_two_arm_that_names_no_id_is_not_readable() -> None:
    assert not Arm(("pytest",), 2, ()).readable


def test_an_arm_with_another_status_is_not_readable_even_with_ids() -> None:
    assert not Arm(("pytest",), 4, (NEW_TEST,)).readable


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    head = tmp_path / "head"
    base = tmp_path / "base"
    for tree in (head, base):
        (tree / "tests").mkdir(parents=True)
    (head / NEW_TEST).write_text("def test_new():\n    assert True\n", encoding="utf-8")
    return base, head


def test_a_base_collection_error_on_the_new_test_measures_cleanly(
    tmp_path: Path,
) -> None:
    base_tree, head_tree = _trees(tmp_path)

    def run(argv, *, cwd, **_kwargs):
        if Path(cwd) == base_tree:
            return subprocess.CompletedProcess(argv, 2, COLLECTION_ERROR, "")
        return subprocess.CompletedProcess(argv, 0, "1 passed in 0.01s\n", "")

    measured = landing_measure.measure_run(
        command=COMMAND,
        tree=head_tree,
        base_sha="base",
        head_sha="head",
        base_tree=base_tree,
        logs_dir=tmp_path / "logs",
        test_files=[NEW_TEST],
        run=run,
        changed=[],
    )

    assert measured.unreadable == ""
    assert measured.suite["base"]["failure_ids"] == [NEW_TEST]
    assert measured.real == ()


def test_a_head_collection_error_is_still_judged_as_a_failure(tmp_path: Path) -> None:
    base_tree, head_tree = _trees(tmp_path)

    def run(argv, *, cwd, **_kwargs):
        if Path(cwd) == head_tree:
            return subprocess.CompletedProcess(argv, 2, COLLECTION_ERROR, "")
        return subprocess.CompletedProcess(argv, 0, "1 passed in 0.01s\n", "")

    measured = landing_measure.measure_run(
        command=COMMAND,
        tree=head_tree,
        base_sha="base",
        head_sha="head",
        base_tree=base_tree,
        logs_dir=tmp_path / "logs",
        test_files=[NEW_TEST],
        run=run,
        changed=[],
        repeats=1,
    )

    assert measured.unreadable == ""
    assert measured.real == (NEW_TEST,)
