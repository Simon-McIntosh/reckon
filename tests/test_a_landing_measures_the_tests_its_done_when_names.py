"""A landing measures what a source-only run's done-when names.

A node can be dispatched to change one module so that an existing test passes,
naming that test in its done-when and writing no test file of its own. Its
fenced scope then holds no test, so a landing resolved only from the scope
measured nothing and refused with "the armed command measures the node's own
test files, and the node names none", although the change touched a module the
named test imports and the done-when named that very test.

The measure now tries two more sources in order: the test files the done-when
names, and failing those, the test files importing the modules the change
touched. The last case is the refusal: a run whose done-when names no test in
the tree and whose change no test imports names both sources it tried.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import delivery_scope, landing_measure

DONE_WHEN_NAMES_THE_TEST = (
    "The new sentinel is returned, and `tests/test_thing.py` passes."
)
DONE_WHEN_NAMES_NO_TEST = "The sentinel is returned and the suite is unchanged."

THING_TEST = (
    "from reckon.thing import value\n\n\ndef test_value():\n    assert value() == 2\n"
)


@pytest.fixture(autouse=True)
def _no_outfitting_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child pytest that inherits an outfitting option runs another suite."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, *tests: tuple[str, str]) -> tuple[Path, str, str]:
    """A repository whose head commit changes only ``reckon/thing.py``.

    The base commit holds the module and the tests; the head commit changes the
    module and nothing else, which is the shape of a run that writes no test
    file. The tests assert the head's value, so the pair is green at both
    revisions once the change is present and red at the base when it is not.
    """
    repo = tmp_path / "repo"
    (repo / "reckon").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "reckon" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "reckon" / "thing.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8"
    )
    for name, body in tests:
        (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "worker")
    _git(repo, "add", "reckon", "tests")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "reckon" / "thing.py").write_text(
        "def value():\n    return 2\n", encoding="utf-8"
    )
    _git(repo, "add", "reckon/thing.py")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def _pointer(repo: Path, base: str, done_when: str) -> dict:
    return {
        "worktree": str(repo),
        "repo": str(repo),
        "base_sha": base,
        "node": {"write_paths": ["reckon/thing.py"], "done_when": done_when},
        "suite_command": (
            f"{sys.executable} -m pytest -p no:cacheprovider -q {{node_test_files}}"
        ),
    }


def _arm_files(observation: dict) -> list[str]:
    """The test files one arm's recorded command names."""
    return [
        word
        for word in shlex.split(observation["command"])
        if word.startswith("tests/")
    ]


def _measure(pointer: dict, repo: Path, base: str, head: str, test_files=()) -> dict:
    """Drive the landing measure the way a landing does, over a real tree."""
    return landing_measure.armed_observations(
        command=pointer["suite_command"],
        tree=repo,
        base_tree=repo,
        base_sha=base,
        head_sha=head,
        logs_dir=repo.parent / "logs",
        test_files=test_files,
        timeout=180,
        config={},
    )


def _derived(pointer: dict, repo: Path, head: str) -> dict:
    return delivery_scope.derive(
        pointer, changed=["reckon/thing.py"], tree=repo, revision=head
    )


def test_a_source_only_run_is_measured_by_its_done_when_test(tmp_path: Path) -> None:
    repo, base, head = _repository(tmp_path, ("tests/test_thing.py", THING_TEST))
    pointer = _pointer(repo, base, DONE_WHEN_NAMES_THE_TEST)

    derived = _derived(pointer, repo, head)
    measured = _measure(pointer, repo, base, head, derived["test_files"])

    assert "unreadable" not in measured, measured
    assert _arm_files(measured["after_suite"]) == ["tests/test_thing.py"], measured
    assert measured["after_suite"]["exit_status"] == 0, measured
    assert derived["test_files"] == ["tests/test_thing.py"], derived


def test_the_measure_reads_the_done_when_when_the_scope_names_no_test(
    tmp_path: Path,
) -> None:
    """The measure itself reads the done-when, for a caller that resolved none."""
    repo, base, head = _repository(tmp_path, ("tests/test_thing.py", THING_TEST))
    pointer = _pointer(repo, base, DONE_WHEN_NAMES_THE_TEST)

    measured = landing_measure.armed_observations(
        command=pointer["suite_command"],
        tree=repo,
        base_tree=repo,
        base_sha=base,
        head_sha=head,
        logs_dir=repo.parent / "logs",
        test_files=(),
        done_when=str(pointer["node"]["done_when"]),
        timeout=180,
        config={},
    )

    assert "unreadable" not in measured, measured
    assert _arm_files(measured["after_suite"]) == ["tests/test_thing.py"], measured
    assert measured["after_suite"]["exit_status"] == 0, measured


def test_a_done_when_naming_no_test_measures_the_importer_tests(
    tmp_path: Path,
) -> None:
    repo, base, head = _repository(
        tmp_path,
        ("tests/test_other.py", "def test_nothing():\n    assert True\n"),
        ("tests/test_other_name.py", "def test_nothing():\n    assert True\n"),
        ("tests/test_uses_thing.py", THING_TEST),
    )
    pointer = _pointer(repo, base, DONE_WHEN_NAMES_NO_TEST)

    derived = _derived(pointer, repo, head)
    measured = _measure(pointer, repo, base, head, derived["test_files"])

    assert derived["test_files"] == ["tests/test_uses_thing.py"], derived
    assert "unreadable" not in measured, measured
    assert _arm_files(measured["after_suite"]) == ["tests/test_uses_thing.py"], measured
    assert measured["after_suite"]["exit_status"] == 0, measured


def test_a_run_with_neither_source_is_refused_naming_both(tmp_path: Path) -> None:
    repo, base, head = _repository(
        tmp_path,
        ("tests/test_other.py", "def test_nothing():\n    assert True\n"),
    )
    pointer = _pointer(repo, base, DONE_WHEN_NAMES_NO_TEST)

    derived = _derived(pointer, repo, head)
    measured = _measure(pointer, repo, base, head, derived["test_files"])

    assert derived["test_files"] == [], derived
    assert "unreadable" in measured, measured
    refusal = measured["unreadable"]
    assert "the node names none" in refusal, refusal
    assert "done-when names no test file" in refusal, refusal
    assert "imports a module the change touched" in refusal, refusal
