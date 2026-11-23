"""A partitioned arm judges a test file's presence against its own revision.

A partitioned arm builds its tree inside its job on the compute node and has no
local checkout to read presence from. Reading presence from a base tree that is
not there keeps every file, so a partitioned base arm named a test the base
revision does not hold: pytest stopped on the unmatched path with no failure id
and the landing read as an unreadable measurement of a file that was simply new.
These cases drive the measurement's own selection and read what each arm's
command names.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from reckon.crew import landing_measure

KEPT = "tests/test_kept.py"
ADDED = "tests/test_added.py"
COMMAND = ".venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, str, str]:
    """A base carrying one node test file, and a head that adds another."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_kept.py").write_text(
        "def test_kept():\n    assert True\n", encoding="utf-8"
    )
    _git(repo.parent, "init", "-q", str(repo))
    base = _commit(repo, "test: prepare base")
    (repo / "tests" / "test_added.py").write_text(
        "def test_added():\n    assert True\n", encoding="utf-8"
    )
    head = _commit(repo, "test: prepare head")
    return repo, base, head


def _runner(calls: list) -> object:
    """A runner that answers every arm as a passing pytest process.

    A partitioned arm's exit is read from a log it writes on its own node, so
    the stand-in writes that log with the receipt the measure reads. An
    in-place arm's process output is its log, which is where its receipt goes.
    """

    def run(argv, **options):
        calls.append((argv, options))
        if Path(argv[0]).name == "sbatch":
            step = argv[argv.index("--wrap") + 1]
            tokens = shlex.split(step)
            log = Path(tokens[tokens.index(">>") + 1])
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as stream:
                stream.write("1 passed\nEXIT=0\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    return run


def _measure(
    tmp_path: Path,
    project: tuple[Path, str, str],
    *,
    test_files: list[str],
    partitioned: bool,
):
    repo, base, head = project
    calls: list = []
    config = {"landing": {"measure_partition": "all_debug"}} if partitioned else {}
    logs = tmp_path / ("partitioned" if partitioned else "in-place") / "landing-measure"
    options = {
        "command": COMMAND,
        "tree": repo,
        "base_sha": base,
        "head_sha": head,
        "logs_dir": logs,
        "test_files": test_files,
        "changed": [],
        "config": config,
        "run": _runner(calls),
    }
    if partitioned:
        # What a partitioned measure's base tree is: a name the arms never read,
        # because each builds its own checkout from its own revision.
        base_tree = tmp_path / f"landing-base-ref-{base}"
        assert not base_tree.exists()
        measured = landing_measure.measure_run(base_tree=base_tree, **options)
        return measured, calls
    with landing_measure.base_checkout(repo, base) as base_tree:
        measured = landing_measure.measure_run(base_tree=base_tree, **options)
    return measured, calls


def _named(arm: dict) -> list[str]:
    """The test files one arm's recorded command names."""
    return [token for token in arm["command"] if token.startswith("tests/")]


def test_a_new_file_is_left_out_of_a_partitioned_base_arm_and_named(
    tmp_path: Path, project: tuple[Path, str, str]
) -> None:
    measured, _calls = _measure(
        tmp_path, project, test_files=[KEPT, ADDED], partitioned=True
    )

    assert not measured.unreadable, measured.unreadable
    baseline = measured.suite["base"]
    after = measured.suite["head"]
    assert _named(baseline) == [KEPT], baseline
    assert _named(after) == [KEPT, ADDED], after
    assert measured.suite["left_out"] == {ADDED: ["base"]}, measured.suite["left_out"]
    # The head adds no failure, so the pair lands.
    assert measured.real == (), measured.real


def test_a_partitioned_arm_with_only_a_new_file_records_an_empty_baseline(
    tmp_path: Path, project: tuple[Path, str, str]
) -> None:
    measured, _calls = _measure(tmp_path, project, test_files=[ADDED], partitioned=True)

    assert not measured.unreadable, measured.unreadable
    baseline = measured.suite["base"]
    after = measured.suite["head"]
    # No local base tree, and no file the base revision holds: the baseline is
    # empty, which adds no base ids rather than reading as an arm that did not
    # run.
    assert _named(baseline) == [], baseline
    assert baseline["exit_status"] == 0, baseline
    assert baseline["failure_ids"] == [], baseline
    assert _named(after) == [ADDED], after
    assert measured.suite["left_out"] == {ADDED: ["base"]}, measured.suite["left_out"]
    assert measured.real == (), measured.real


def test_an_unpartitioned_arm_selects_files_from_its_own_trees(
    tmp_path: Path, project: tuple[Path, str, str]
) -> None:
    measured, _calls = _measure(
        tmp_path, project, test_files=[KEPT, ADDED], partitioned=False
    )

    assert not measured.unreadable, measured.unreadable
    baseline = measured.suite["base"]
    after = measured.suite["head"]
    assert _named(baseline) == [KEPT], baseline
    assert _named(after) == [KEPT, ADDED], after
    assert measured.suite["left_out"] == {ADDED: ["base"]}, measured.suite["left_out"]
