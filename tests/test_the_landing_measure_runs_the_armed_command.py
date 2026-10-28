"""The landing measure runs the suite command the run armed.

The armed command is written for a shell: it opens with environment
assignments, names the project's interpreter by a relative path, and carries
``{node_test_files}`` in place of the node's own test files. A measurement that
executed that text as an argv failed on its own first token, and the base arm
reported nothing measured. These cases drive ``measure_run`` over a
synthesised repository with a real subprocess runner, so what runs is the
command a promotion would run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import landing_measure

COMMAND = "PYTHONPATH=. .venv/bin/python -m pytest -q {node_test_files}"

#: The node's own fenced test files, as the dispatcher resolves them.
FILES = ["tests/test_carried.py", "tests/test_landed.py"]

CARRIED_ID = "tests/test_carried.py::test_carried_fails"
LANDED_ID = "tests/test_landed.py::test_landed"


def _git(repo: Path, *arguments: str) -> str:
    finished = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return finished.stdout.strip()


def _environment(repo: Path) -> Path:
    """Link the repository's own environment, as a provisioned worktree has it.

    The armed command names ``.venv/bin/python`` relative to the tree it runs
    in, so the fixture repository carries the same link a real checkout does.
    """
    environment = repo / ".venv"
    environment.symlink_to(Path(sys.prefix))
    return environment


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
    # A child pytest that inherits an outfitting option runs a different suite.
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    environment = _environment(repo)
    tests = repo / "tests"
    tests.mkdir()
    package = repo / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_landed.py").write_text(
        "def test_landed():\n    assert True\n", encoding="utf-8"
    )
    (tests / "test_imports_value.py").write_text(
        "from package.value import VALUE\n\n\ndef test_value():\n    assert VALUE\n",
        encoding="utf-8",
    )
    _git(repo, "add", "package", "tests")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")

    # The delivered head changes a module, adds one test file and breaks
    # another: the added file is what the base tree does not carry, and the
    # broken one is the id that must be re-run alone.
    (package / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tests / "test_landed.py").write_text(
        "def test_landed():\n    assert False\n", encoding="utf-8"
    )
    (tests / "test_carried.py").write_text(
        "def test_carried_fails():\n    assert False\n", encoding="utf-8"
    )
    _git(repo, "add", "package", "tests")
    _git(repo, "commit", "-q", "-m", "feat: change the module and add a test")
    head = _git(repo, "rev-parse", "HEAD")
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "environment": environment,
        "logs": tmp_path / "logs",
    }


def _recording(calls: list[dict]):
    """A real subprocess runner that records what each arm was handed."""

    def run(
        argv,
        *,
        cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=None,
        env=None,
    ):
        calls.append({"argv": list(argv), "cwd": str(cwd), "env": dict(env or {})})
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            env=env,
        )

    return run


def test_an_armed_command_runs_its_substituted_files_under_its_environment(
    delivery,
) -> None:
    calls: list[dict] = []
    repo = delivery["repo"]
    with landing_measure.base_checkout(
        repo, delivery["base"], timeout=120
    ) as base_tree:
        measured = landing_measure.measure_run(
            command=COMMAND,
            tree=repo,
            base_sha=delivery["base"],
            head_sha=delivery["head"],
            base_tree=base_tree,
            logs_dir=delivery["logs"],
            test_files=FILES,
            run=_recording(calls),
            repeats=2,
            timeout=180,
        )
        # The base tree carries the provisioned link, so the relative
        # interpreter in the armed command can start there.
        link = base_tree / ".venv"
        assert link.is_symlink(), list(base_tree.iterdir())
        assert link.resolve() == delivery["environment"].resolve()
        assert os.access(link / "bin" / "python", os.X_OK)

    assert measured.unreadable == "", measured.as_dict()
    base_arm = measured.suite["base"]
    head_arm = measured.suite["head"]
    assert base_arm["exit_status"] is not None, base_arm
    assert head_arm["exit_status"] is not None, head_arm

    suite_calls = [
        call
        for call in calls
        if "-n" not in call["argv"] and "::" not in call["argv"][-1]
    ]
    assert {Path(call["cwd"]) for call in suite_calls} == {
        Path(base_tree),
        Path(repo),
    }, suite_calls
    for call in suite_calls:
        # The placeholder is gone and the node's files stand in its place; the
        # leading assignment is not the program.
        assert call["argv"][:5] == [
            ".venv/bin/python",
            "-m",
            "pytest",
            "-q",
            FILES[0],
        ], call
        assert call["argv"][5] == FILES[1], call
        assert not any(token.startswith("PYTHONPATH=") for token in call["argv"]), call
        assert call["env"].get("PYTHONPATH") == ".", call

    # The file the node added is absent at the base revision, so it was carried
    # into the base tree and its failure counted there rather than stopping the
    # arm on a missing file.
    assert base_arm["failure_ids"] == [CARRIED_ID], base_arm
    assert set(head_arm["failure_ids"]) == {CARRIED_ID, LANDED_ID}, head_arm
    # The head-only id was re-run alone, at both revisions, and reads as real.
    assert measured.real == (LANDED_ID,), measured.as_dict()

    population = [call for call in calls if "-n" in call["argv"]]
    assert len(population) == 2, population
    isolated = [call for call in calls if call["argv"][-1] == LANDED_ID]
    assert len(isolated) == 4, isolated
    for call in [*population, *isolated]:
        assert not any(token.startswith("PYTHONPATH=") for token in call["argv"]), call
        assert call["env"].get("PYTHONPATH") == ".", call
