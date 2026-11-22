"""A run that adds a test file still has its control mutation measured.

The control mutation is a node's own tests run over the tree its repair is
absent from: the node's tests are carried into a checkout of the run's base
revision, so a case that measures the repaired guard fails where the guard is
missing. A test file the node adds does not exist at that base revision, and
the partitioned measurement tree is built from a revision rather than from the
local checkout, so the carry is made from the revision that holds the file.

These tests drive the real measure against a temporary repository, with the
submitted job's script run locally through an ``srun`` shim. Each case checks
the arm's own log, which carries the revision it ran at, the command it ran
and a closing ``EXIT=`` line.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from reckon.crew import landing_measure

COMMAND = f"{sys.executable} -m pytest -q {{node_test_files}}"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository whose head adds one test file the base revision lacks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "worker")
    (repo / "sample.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_existing.py").write_text(
        "from sample import value\n\n\ndef test_value():\n    assert value() == 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", "sample.py", "tests/test_existing.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_added.py").write_text(
        "from sample import value\n\n\ndef test_mutated():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/test_added.py")
    _git(repo, "commit", "-q", "-m", "adds the declared test")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def _scheduler(tmp_path: Path):
    """A scheduler that runs the submitted job's script on this host.

    The script the measurement builds is the same one a batch job runs; a shim
    standing where ``srun`` does lets it execute here, so the tree the measure
    builds is the tree it would have built on the partition.
    """
    shim = tmp_path / "bin"
    shim.mkdir()
    srun = shim / "srun"
    srun.write_text(
        '#!/bin/sh\nwhile [ "$1" != bash ]; do shift; done\nexec "$@"\n',
        encoding="utf-8",
    )
    srun.chmod(0o700)

    def scheduler(argv, **options):
        wrap = argv[argv.index("--wrap") + 1]
        environment = dict(os.environ)
        environment["PATH"] = f"{shim}:{environment['PATH']}"
        finished = subprocess.run(
            ["bash", "-c", wrap],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        return subprocess.CompletedProcess(
            argv, finished.returncode, finished.stdout, finished.stderr
        )

    return scheduler


def _control(
    tmp_path: Path,
    repo: Path,
    base_sha: str,
    test_files: list[str],
) -> dict[str, Any]:
    logs = tmp_path / "run" / "landing-measure" / "control"
    with landing_measure.base_checkout(repo, base_sha, 120) as base_tree:
        return landing_measure.control_mutation_observation(
            command=COMMAND,
            tree=repo,
            base_tree=base_tree,
            base_sha=base_sha,
            logs_dir=logs,
            test_files=test_files,
            run=_scheduler(tmp_path),
            timeout=120,
            config={"landing": {"measure_partition": "all_debug"}},
        )


def _log(observation: dict[str, Any]) -> tuple[str, Path]:
    suite = observation["control_suite"]
    path = Path(suite["log_path"])
    return path.read_text(encoding="utf-8"), path


def test_an_added_test_files_mutation_is_measured_and_reads_red(tmp_path):
    """The added test file is carried from the head and fails over the base."""
    repo, base_sha, _head = _repository(tmp_path)
    observation = _control(tmp_path, repo, base_sha, ["tests/test_added.py"])
    assert "unreadable" not in observation
    suite = observation["control_suite"]
    assert suite["revision"] == base_sha
    assert suite["completed"] is True
    assert suite["exit_status"] == 1
    assert "tests/test_added.py::test_mutated" in suite["failure_ids"]

    text, path = _log(observation)
    assert path.is_file()
    assert f"# revision: {base_sha}" in text
    assert "# command:" in text
    assert "tests/test_added.py" in text
    assert re.findall(r"^EXIT=(\d+)$", text, re.MULTILINE)[-1] == "1"


def test_a_run_editing_only_existing_tests_is_measured_as_before(tmp_path):
    """The base revision's own copy of an existing test file is what runs."""
    repo, base_sha, _head = _repository(tmp_path)
    # The head edits the existing test to pass, so a control that ran the
    # head's copy would read green; the control must read the base's copy.
    (repo / "tests" / "test_existing.py").write_text(
        "from sample import value\n\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/test_existing.py")
    _git(repo, "commit", "-q", "-m", "edits the existing test")
    edited_head = _git(repo, "rev-parse", "HEAD")
    observation = _control(tmp_path, repo, base_sha, ["tests/test_existing.py"])
    assert "unreadable" not in observation
    suite = observation["control_suite"]
    assert suite["revision"] == base_sha
    assert suite["exit_status"] == 1
    assert "tests/test_existing.py::test_value" in suite["failure_ids"]

    text, _ = _log(observation)
    assert f"# revision: {base_sha}" in text
    assert re.findall(r"^EXIT=(\d+)$", text, re.MULTILINE)[-1] == "1"
    assert edited_head not in text


def test_a_tree_that_cannot_be_built_names_the_path_and_revision(tmp_path):
    """A path neither revision holds refuses naming both facts."""
    repo, base_sha, head_sha = _repository(tmp_path)
    observation = _control(tmp_path, repo, base_sha, ["tests/test_missing.py"])
    assert "unreadable" in observation
    reason = observation["unreadable"]
    assert "tests/test_missing.py" in reason
    assert head_sha in reason

    logs = list((tmp_path / "run" / "landing-measure" / "control").glob("*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "tests/test_missing.py" in text
    assert head_sha in text
    assert re.findall(r"^EXIT=(\d+)$", text, re.MULTILINE)[-1] == "4"
