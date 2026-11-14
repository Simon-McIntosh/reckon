"""A run's armed command, importer population and review scope come from its diff.

A run dispatched with no write paths used to have no prediction of what it
would change, so its armed command resolved to nothing, its importer population
named no caller and the code-review neighbourhood a plan build would consult
did not contain it. The diff every delivered run carries answers all three, and
a declared run whose diff fills its scope must derive exactly what the run
without a prediction derives.

The negative control below states the mutation that matters: deriving the
armed command from the record's own write-path list (empty for a run that
declared none) arms nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from reckon.crew import delivery_scope

SUITE = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
PRODUCT = "reckon/x.py"
NEW_TEST = "tests/test_z.py"
IMPORTER = "tests/test_x.py"


def _run(tree: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(tree), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _git(tree: Path, *arguments: str) -> str:
    return _run(tree, *arguments).stdout.strip()


def _seed_repository(tmp_path: Path) -> Path:
    """A repository with product source, one importer test and a base revision."""
    tree = tmp_path / "repo"
    files = {
        "reckon/__init__.py": "",
        PRODUCT: "VALUE = 1\n",
        "reckon/y.py": "from reckon import x\n\nDOUBLE = x.VALUE * 2\n",
        IMPORTER: "from reckon import x\n\n\ndef test_value():\n    assert x.VALUE\n",
        "tests/test_other.py": "def test_other():\n    assert True\n",
    }
    for name, text in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run(tree, "init", "-q", "-b", "main")
    _run(tree, "config", "user.email", "worker@example.invalid")
    _run(tree, "config", "user.name", "Worker")
    _run(tree, "add", "reckon", "tests")
    _run(tree, "commit", "-q", "-m", "seed")
    return tree


def _deliver(tree: Path, path: str, text: str, message: str) -> str:
    """Commit one change on the current branch and return the new head."""
    target = tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _run(tree, "add", path)
    _run(tree, "commit", "-q", "-m", message)
    return _git(tree, "rev-parse", "HEAD")


def _record(tree: Path, head: str, write_paths: list[str], base: str) -> dict:
    return {
        "worktree": str(tree),
        "base_sha": base,
        "head": head,
        "suite_command": SUITE,
        "census_tests": [],
        "node": {"write_paths": write_paths},
    }


def test_a_no_write_paths_run_derives_what_a_declared_run_with_the_same_diff_does(
    tmp_path: Path,
) -> None:
    tree = _seed_repository(tmp_path)
    base = _git(tree, "rev-list", "--max-parents=0", "HEAD")
    _deliver(tree, PRODUCT, "VALUE = 2\n", "change the value")
    head = _deliver(
        tree, NEW_TEST, "def test_added():\n    assert True\n", "add a test"
    )

    changed = [PRODUCT, NEW_TEST]
    declared = _record(tree, head, changed, base)
    no_paths = _record(tree, head, [], base)

    derived_declared = delivery_scope.derive(
        declared, changed=changed, tree=tree, revision=head
    )
    derived_none = delivery_scope.derive(
        no_paths, changed=changed, tree=tree, revision=head
    )

    assert derived_none == derived_declared
    assert derived_none["armed_command"] == SUITE.replace("{node_test_files}", NEW_TEST)
    assert derived_none["test_files"] == [NEW_TEST]
    assert derived_none["importer_population"] == [IMPORTER]
    assert derived_none["review_neighbourhood"]["paths"] == [PRODUCT]
    assert "reckon.y" in derived_none["review_neighbourhood"]["modules"]


def test_two_no_write_paths_runs_deliver_and_meet_at_the_merge(
    tmp_path: Path,
) -> None:
    tree = _seed_repository(tmp_path)
    base = _git(tree, "rev-list", "--max-parents=0", "HEAD")

    first = _deliver(tree, PRODUCT, "VALUE = 3\n", "first run")
    first_scope = delivery_scope.derive(
        _record(tree, first, [], base), changed=[PRODUCT], tree=tree, revision=first
    )

    _run(tree, "checkout", "-q", "-b", "second", base)
    second = _deliver(tree, "reckon/z.py", "EXTRA = 1\n", "second run")
    second_scope = delivery_scope.derive(
        _record(tree, second, [], base),
        changed=["reckon/z.py"],
        tree=tree,
        revision=second,
    )

    assert first_scope["review_neighbourhood"]["paths"] == [PRODUCT]
    assert second_scope["review_neighbourhood"]["paths"] == ["reckon/z.py"]

    from reckon.crew import merge

    merged = merge.build_merge_commit(
        tree, base, first, "merge: first delivery", "Combine the first delivery."
    )
    merge.build_merge_commit(
        tree,
        merged,
        second,
        "merge: second delivery",
        "Combine the second delivery.",
    )

    assert _git(tree, "show", f"{merged}:{PRODUCT}") == "VALUE = 3"


def test_an_empty_write_path_list_arms_nothing(tmp_path: Path) -> None:
    """The negative control: no diff and no declared scope is no command."""
    tree = _seed_repository(tmp_path)
    head = _git(tree, "rev-parse", "HEAD")

    derived = delivery_scope.derive(
        _record(tree, head, [], head), changed=[], tree=tree, revision=head
    )

    assert derived["armed_command"] == ""
    assert derived["test_files"] == []
    assert derived["importer_population"] == []
    assert derived["review_neighbourhood"] == {"paths": [], "modules": []}
