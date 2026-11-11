"""An after suite arm recorded behind the run's head is refused by name.

The after arm is evidence about the tree the run delivers, so the revision it
records must be the run's head. A run that measures halfway through its own work
and then commits again leaves an arm about a tree nobody ships, and refusing it
with the bare line "missing after_suite.command_matches_suite_command" told the
worker neither the revision it had recorded nor the one it should have, costing
a resume round to work out.

The refusal therefore names both revisions and prints the armed command resolved
at the head, so the worker reruns the one thing it has to rerun.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew.promotion_records import _evaluate_suite_delta

TEMPLATE = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
TEST_FILE = "tests/test_alpha.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A run's worktree whose head has moved past its base, and both revisions.

    The worktree is a linked one, so releasing it leaves the repository that
    holds its commits behind — the state a promotion meets after a run's
    working tree has been reclaimed.
    """
    main = tmp_path / "main"
    (main / "tests").mkdir(parents=True)
    (main / TEST_FILE).write_text("def test_alpha():\n    assert True\n")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "worker@example.invalid")
    _git(main, "config", "user.name", "Worker")
    _git(main, "add", "tests")
    _git(main, "commit", "-q", "-m", "base")
    base = _git(main, "rev-parse", "HEAD")
    worktree = tmp_path / "run-worktree"
    _git(main, "worktree", "add", "-q", "--detach", str(worktree), base)
    (worktree / "delivered.txt").write_text("the run's own work\n")
    _git(worktree, "add", "delivered.txt")
    _git(worktree, "commit", "-q", "-m", "head")
    head = _git(worktree, "rev-parse", "HEAD")
    return worktree, main, base, head


def _observation(revision: str, command: str) -> dict[str, object]:
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _record(
    tmp_path: Path,
    worktree: Path,
    repo: Path,
    base: str,
    base_revision: str,
    after_revision: str,
    *,
    commits: list[str] | None = None,
) -> dict[str, object]:
    command = TEMPLATE.replace("{node_test_files}", TEST_FILE)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "\n".join(
            [
                "node: node-a",
                "status: complete",
                "commits: " + ",".join(commits or [base]),
                "baseline_suite: " + json.dumps(_observation(base_revision, command)),
                "after_suite: " + json.dumps(_observation(after_revision, command)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "run_id": "r-trailing-arm",
        "suite_command": TEMPLATE,
        "worktree": str(worktree),
        "repo": str(repo),
        "base_sha": base,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {"write_paths": [TEST_FILE]},
    }


def _refusal(record: dict[str, object]) -> ledger.SuiteDeltaError:
    with pytest.raises(ledger.SuiteDeltaError) as raised:
        _evaluate_suite_delta("r-trailing-arm", record, waiver_reason="", dry_run=True)
    return raised.value


def test_an_after_arm_at_the_base_is_refused_naming_both_revisions(
    tmp_path: Path, tree: tuple[Path, Path, str, str]
) -> None:
    """The trailing arm names what it recorded and the head it should record."""
    worktree, repo, base, head = tree
    record = _record(tmp_path, worktree, repo, base, base, base)

    raised = _refusal(record)

    assert raised.missing_fields == ["after_suite.revision_matches_head"]
    message = str(raised)
    assert base in message
    assert head in message
    assert TEMPLATE.replace("{node_test_files}", TEST_FILE) in message


def test_an_after_arm_at_the_head_is_no_revision_refusal(
    tmp_path: Path, tree: tuple[Path, Path, str, str]
) -> None:
    """An arm recorded at the head is evidence about the delivered tree."""
    worktree, repo, base, head = tree
    record = _record(tmp_path, worktree, repo, base, base, head)

    delta = _evaluate_suite_delta(
        "r-trailing-arm", record, waiver_reason="", dry_run=True
    )

    assert delta is not None
    assert delta["status"] == "clean"


def test_a_released_worktree_reads_its_head_from_the_cited_commits(
    tmp_path: Path, tree: tuple[Path, Path, str, str]
) -> None:
    """With no worktree the head is the newest cited commit by ancestry."""
    worktree, repo, base, head = tree
    record = _record(tmp_path, worktree, repo, base, base, base, commits=[base, head])
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        check=True,
        capture_output=True,
    )
    assert not worktree.is_dir()

    raised = _refusal(record)

    assert raised.missing_fields == ["after_suite.revision_matches_head"]
    assert head in str(raised)


def test_a_newest_first_citation_list_names_the_true_head(
    tmp_path: Path, tree: tuple[Path, Path, str, str]
) -> None:
    """The cited commits carry no order, so the head is the newest by ancestry.

    A worker may write ``commits:`` newest-first. Its last entry is then the
    oldest commit the run cites, and a reader that takes that entry as the head
    accepts an after arm measured at the base as if the run had ended at its
    base.
    """
    worktree, repo, base, head = tree
    record = _record(tmp_path, worktree, repo, base, base, base, commits=[head, base])
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        check=True,
        capture_output=True,
    )
    assert not worktree.is_dir()

    raised = _refusal(record)

    assert raised.missing_fields == ["after_suite.revision_matches_head"]
    assert head in str(raised)
