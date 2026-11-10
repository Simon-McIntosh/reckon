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

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from reckon.crew import promotion_records
from reckon.crew.promotion_records import _scoped_suite_command

SCOPED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
CENSUS = "tests/test_staged_write_inventory.py"
CENSUS_DATA = "docs/research/data/crew-pattern-review/code-depth"
STAGED_WRITER_FAILURE = (
    "tests/test_staged_write_inventory.py::test_every_staged_write_reaches_the_owner"
)
UNLISTED_WRITER = "reckon/crew/unlisted_writer.py"
UNLISTED_WRITER_NAME = "publish_report"
UNLISTED_WRITER_SOURCE = f'''"""A staged writer the census expected inventory does not list."""

import os


def {UNLISTED_WRITER_NAME}(destination, payload):
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, destination)
'''

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _armed_record(tmp_path: Path, worktree: Path, baseline, after) -> dict[str, object]:
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
        "worktree": str(worktree),
        "base_sha": "",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {"write_paths": [UNLISTED_WRITER]},
    }


def _census_tree(root: Path) -> tuple[str, str, str]:
    """A scratch repository the census runs against, and its three revisions.

    Only the modules the census's own expected inventory lists are committed,
    so the base revision is green whatever the working repository's inventory
    currently holds: the fixture reproduces the state a node's census measures
    against rather than depending on this node's own landing. The head then
    adds a staged writer that inventory does not list, and ``later`` moves the
    head past it without touching a module — the base a red tree still owes.
    """
    for relative in (
        "reckon",
        "tests/conftest.py",
        CENSUS,
        CENSUS_DATA,
        "pyproject.toml",
    ):
        source = REPO_ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source, destination, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copy2(source, destination)

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    listed = sorted({path for path, _name in _expected_inventory()})
    _git(
        root, "add", *listed, "tests/conftest.py", CENSUS, CENSUS_DATA, "pyproject.toml"
    )
    _git(root, "commit", "-q", "-m", "base tree")
    base = _git(root, "rev-parse", "HEAD")

    (root / UNLISTED_WRITER).write_text(UNLISTED_WRITER_SOURCE, encoding="utf-8")
    _git(root, "add", UNLISTED_WRITER)
    _git(root, "commit", "-q", "-m", "head adds an unlisted staged writer")
    head = _git(root, "rev-parse", "HEAD")

    (root / "README").write_text("later\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "later tree")
    later = _git(root, "rev-parse", "HEAD")
    return base, head, later


def _expected_inventory() -> set[tuple[str, str]]:
    """The staged-write inventory the census asserts, read from the assertion."""
    tree = ast.parse((REPO_ROOT / CENSUS).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Compare) and isinstance(node.comparators[0], ast.Set)
        ):
            continue
        pairs = set()
        for element in node.comparators[0].elts:
            if isinstance(element, ast.Tuple) and len(element.elts) == 2:
                pairs.add(tuple(ast.literal_eval(entry) for entry in element.elts))
        if pairs:
            return pairs
    raise AssertionError("the census states no expected inventory")


def _run_census(root: Path) -> tuple[list[str], str]:
    """What the real census reports over ``root``: its failure ids and output."""
    environment = dict(os.environ)
    environment.pop("RECKON_INVENTORY_REVISION", None)
    environment["PYTHONPATH"] = "."
    environment["TMPDIR"] = str(root / "tmp")
    (root / "tmp").mkdir(exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--tb=line",
            CENSUS,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = result.stdout + result.stderr
    return sorted(set(re.findall(r"^FAILED (\S+)", output, re.MULTILINE))), output


@dataclass(frozen=True)
class CensusRuns:
    root: Path
    base: str
    head: str
    later: str
    base_failures: list[str]
    head_failures: list[str]
    later_failures: list[str]
    head_output: str


@pytest.fixture(scope="module")
def census_runs(tmp_path_factory: pytest.TempPathFactory) -> CensusRuns:
    root = tmp_path_factory.mktemp("census") / "tree"
    root.mkdir()

    base, head, later = _census_tree(root)
    # The base census is measured before the unlisted writer exists, so the two
    # arms are the real trees rather than a label on one tree's result.
    base_failures, _ = _run_census_at(root, base)
    head_failures, head_output = _run_census_at(root, head)
    later_failures, _ = _run_census_at(root, later)
    return CensusRuns(
        root=root,
        base=base,
        head=head,
        later=later,
        base_failures=base_failures,
        head_failures=head_failures,
        later_failures=later_failures,
        head_output=head_output,
    )


def _run_census_at(root: Path, revision: str) -> tuple[list[str], str]:
    """Run the census over the working tree checked out at ``revision``."""
    _git(root, "checkout", "-q", revision)
    return _run_census(root)


def test_a_head_that_adds_an_unlisted_writer_is_refused(
    tmp_path: Path, census_runs: CensusRuns
) -> None:
    """Case (d), added half: the real census fails only at the head."""
    assert census_runs.base_failures == []
    assert census_runs.head_failures == [STAGED_WRITER_FAILURE]
    assert UNLISTED_WRITER_NAME in census_runs.head_output

    record = _armed_record(
        tmp_path,
        census_runs.root,
        _observation(census_runs.base, _resolved(CENSUS), census_runs.base_failures),
        _observation(census_runs.later, _resolved(CENSUS), census_runs.later_failures),
    )
    record["base_sha"] = census_runs.base

    with pytest.raises(promotion_records.ledger.SuiteDeltaError) as raised:
        promotion_records._evaluate_suite_delta(
            "r-census-arm", record, waiver_reason="", dry_run=True
        )

    assert raised.value.added_failure_ids == [STAGED_WRITER_FAILURE]


def test_a_census_already_red_at_base_adds_no_failure(
    tmp_path: Path, census_runs: CensusRuns
) -> None:
    """Case (d), unchanged half: a red base and a red head is no delta."""
    assert census_runs.head_failures == [STAGED_WRITER_FAILURE]
    assert census_runs.later_failures == census_runs.head_failures

    record = _armed_record(
        tmp_path,
        census_runs.root,
        _observation(census_runs.head, _resolved(CENSUS), census_runs.head_failures),
        _observation(census_runs.later, _resolved(CENSUS), census_runs.later_failures),
    )
    record["base_sha"] = census_runs.head

    delta = promotion_records._evaluate_suite_delta(
        "r-census-arm", record, waiver_reason="", dry_run=True
    )

    assert delta is not None
    assert delta["added_failure_ids"] == []
    assert delta["status"] == "clean"
