"""A suite arm matches the armed command when it runs the same thing.

An arm is recorded as the shell string a worker ran, and workers spell the
same run in more than one way: the test files in a different order, the
interpreter as an absolute path where the armed template is relative, and a
leading ``cd`` into the tree the arm ran in. Those arms ran the suite they were
armed with, so a comparison on spelling alone refused evidence that was in
fact complete, at the cost of a resume round and a failed promotion.

The comparison is therefore made on what the command runs: the test files as a
set, the interpreter as one resolved path, and the tree the arm changed into.
A different file, flag or interpreter still refuses, because that command runs
a different suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew.promotion_records import _arm_missing_fields

TEMPLATE = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
ALPHA = "tests/test_alpha.py"
BETA = "tests/test_beta.py"
WRITE_PATHS = [ALPHA, BETA]


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A tree holding two collectible tests and one environment interpreter."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / ALPHA).write_text("def test_alpha():\n    assert True\n")
    (root / BETA).write_text("def test_beta():\n    assert True\n")
    interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "worker@example.invalid"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Worker"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "tests"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed base"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def _revision(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _record(root: Path) -> dict[str, object]:
    return {
        "suite_command": TEMPLATE,
        "worktree": str(root),
        "node": {"write_paths": list(WRITE_PATHS)},
    }


def _missing(root: Path, command: str) -> list[str]:
    observation = {
        "revision": _revision(root),
        "command": command,
        "exit_status": 0,
        "failure_count": 0,
        "failure_ids": [],
    }
    return _arm_missing_fields(_record(root), "after_suite", observation)


def test_the_same_files_in_another_order_match(repository: Path) -> None:
    """The armed command sorts its files; an arm may hand them over reversed."""
    command = (
        f"PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {BETA} {ALPHA}"
    )

    assert _missing(repository, command) == []


def test_the_absolute_interpreter_of_the_runs_environment_matches(
    repository: Path,
) -> None:
    """``.venv/bin/python`` and its absolute path are the one interpreter."""
    command = (
        f"PYTHONPATH=. {repository / '.venv' / 'bin' / 'python'} -m pytest "
        f"-p no:cacheprovider -q {ALPHA} {BETA}"
    )

    assert _missing(repository, command) == []


def test_a_leading_cd_into_the_arms_own_tree_matches(repository: Path) -> None:
    """Changing into the arm's own tree changes nothing the arm runs."""
    command = (
        f"cd {repository} && PYTHONPATH=. .venv/bin/python -m pytest "
        f"-p no:cacheprovider -q {ALPHA} {BETA}"
    )

    assert _missing(repository, command) == []


def test_a_missing_test_file_refuses(repository: Path) -> None:
    """A command naming only one of the armed files runs a smaller suite."""
    command = f"PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {ALPHA}"

    assert "after_suite.command_matches_suite_command" in _missing(repository, command)


def test_an_extra_pytest_flag_refuses(repository: Path) -> None:
    """A flag the armed command does not carry runs the suite differently."""
    command = (
        "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q -x "
        f"{ALPHA} {BETA}"
    )

    assert "after_suite.command_matches_suite_command" in _missing(repository, command)
