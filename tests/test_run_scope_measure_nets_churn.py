"""A promotion's scope is measured from the content its citations carry.

A run's work can reach the integration branch inside a merge, and a coordinator
then presents that merge as the run's evidence. The merge is measured against
its first parent — the content the merge brought — so the row charges that
content instead of recording a run that changed nothing. The changed-line count
is one net diff over the cited commits, so a line an earlier cited commit
introduced and a later cited commit removed counts nothing.

Every expectation below is the synthesised repository's own git reading of the
diff the citation claims, never a restatement of the synthesised run's input,
so a case cannot pass by echoing what it passed in.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _numstat(directory: Path, base: str, head: str, *paths: str) -> dict[str, int]:
    """The fixture's own reading of one diff, taken from git rather than restated."""
    output = _git(directory, "diff", "--numstat", base, head, "--", *paths)
    added = removed = files = 0
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        files += 1
        added += int(fields[0]) if fields[0].isdigit() else 0
        removed += int(fields[1]) if fields[1].isdigit() else 0
    return {"added": added, "removed": removed, "files": files}


def _real_pointer_dir() -> Path:
    return Path.home() / ".config" / "reckon" / "crew" / "live"


def _assert_real_home_carries_no_pointer(run_id: str) -> None:
    """The synthesised run must never reach the operator's own pointer directory."""
    assert not (_real_pointer_dir() / f"{run_id}.json").exists()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "in_scope.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "in_scope.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_tree: Path,
    run_id: str,
    base: str,
    *,
    write_paths: tuple[str, ...],
) -> None:
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(run_tree),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-09-25T12:00:00Z",
        "node": {
            "id": "scope-counting",
            "plan": "fixture",
            "section": "guard",
            "time_budget": "25m",
            "write_paths": list(write_paths),
        },
    }
    _write_json(pointer_path(run_id), pointer)


@pytest.fixture()
def run_merged_into_main(repository: Path, tmp_path: Path) -> tuple[Path, str, str]:
    """The run's own commit, merged into main and cited through that merge.

    Returns the run worktree, its run id, and the merge main now carries. The
    run's content reached main inside the merge, so the merge's first parent is
    the revision the run's work descended from and the first-parent diff is
    exactly the content the run brought.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-merge-borne-content"
    _pointer(repository, run_tree, run_id, base, write_paths=("in_scope.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    own = _git(run_tree, "rev-parse", "HEAD")

    _git(repository, "merge", "-q", "--no-ff", own, "-m", "Merge the run's change")
    merge = _git(repository, "rev-parse", "HEAD")
    return run_tree, run_id, merge


def test_a_merge_only_citation_charges_the_content_the_merge_brought(
    repository: Path, run_merged_into_main: tuple[Path, str, str]
) -> None:
    """The run's work reached the record inside the merge, so the merge is read.

    The merge's first-parent diff is the run's own content: it must be what the
    row records. The fixture proves the merge brought a line, and that reading
    is the positive control: a zero here would be a broken instrument rather
    than an absence of content.
    """
    _, run_id, merge = run_merged_into_main
    first_parent = _numstat(repository, f"{merge}^1", merge)
    assert first_parent["added"] > 0

    stored = crew.complete(run_id, gate="passed", commits=[merge], root=repository)[
        "record"
    ]

    assert stored["changed_lines"] == first_parent
    assert stored["changed_lines"]["added"] + stored["changed_lines"]["removed"] > 0
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_merge_only_citation_charges_the_paths_the_merge_brought(
    repository: Path, tmp_path: Path
) -> None:
    """The paths charged are the merge's first-parent paths, so the fence sees them.

    The merged content changes generated state, a class git cannot reconcile, so
    the path arrives at the boundary guard as the run's own and is named in the
    refusal while the run declares an unrelated path. A reading that skipped a
    merge-only citation entirely refuses with its own message instead, which
    does not name the path.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "scoped-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-merge-borne-path"
    _pointer(repository, run_tree, run_id, base, write_paths=("elsewhere.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    state = run_tree / "docs" / "state" / PROJECT / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"seed": true}\n', encoding="utf-8")
    _git(run_tree, "add", str(state.relative_to(run_tree)))
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    own = _git(run_tree, "rev-parse", "HEAD")
    _git(repository, "merge", "-q", "--no-ff", own, "-m", "Merge the run's change")
    merge = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[merge], root=repository)

    assert "docs/state/sample/state.json" in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_merge_borne_source_path_needs_no_acceptance(
    repository: Path, tmp_path: Path
) -> None:
    """A declared write path is a hint, so a merge-borne source path lands.

    The run declares a path the merged content does not touch, and the merged
    path is one a merge reconciles: the promotion records the merge's own
    reading of the change rather than holding the row for a path a reviewer
    never touched.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "hinted-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-merge-borne-source"
    _pointer(repository, run_tree, run_id, base, write_paths=("elsewhere.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    own = _git(run_tree, "rev-parse", "HEAD")
    _git(repository, "merge", "-q", "--no-ff", own, "-m", "Merge the run's change")
    merge = _git(repository, "rev-parse", "HEAD")

    stored = crew.complete(run_id, gate="passed", commits=[merge], root=repository)[
        "record"
    ]

    assert stored["changed_lines"] == _numstat(repository, f"{merge}^1", merge)
    assert not stored.get("scope_acceptances")


def test_a_later_commit_on_the_runs_branch_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A descendant that is not the merge carrying the run is another writer's work.

    The citation descends from the run's head but is not a merge naming it as a
    parent, so its first-parent diff carries content the run never touched. The
    guard refuses it rather than charging that content to the run, and the run's
    pointer and the ledger stay untouched.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "citing-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-later-descendant"
    _pointer(repository, run_tree, run_id, base, write_paths=("in_scope.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    own = _git(run_tree, "rev-parse", "HEAD")

    later_tree = tmp_path / "later-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(later_tree), own)
    (later_tree / "later.txt").write_text("another writer\n", encoding="utf-8")
    _git(later_tree, "add", "later.txt")
    _git(later_tree, "commit", "-q", "-m", "feat: a later commit")
    later = _git(later_tree, "rev-parse", "HEAD")
    assert later != own

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[later], root=repository)

    assert "not a commit this run made" in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_line_added_and_later_removed_counts_nothing(
    repository: Path, tmp_path: Path
) -> None:
    """One net diff over the cited commits, so self-cancelled churn nets out.

    The first cited commit adds a line and the second removes it again, so the
    net reading is zero while the per-commit sum is two: the case asserts the
    two readings differ, so it cannot pass by measuring nothing, and the
    expectation is the fixture's own git numstat of the span.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "netted-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-netted-to-zero"
    _pointer(repository, run_tree, run_id, base, write_paths=("in_scope.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nonly for now\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: add a line")
    first = _git(run_tree, "rev-parse", "HEAD")

    (run_tree / "in_scope.txt").write_text("seed\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: remove the line again")
    tip = _git(run_tree, "rev-parse", "HEAD")

    net = _numstat(repository, base, tip, "in_scope.txt")
    added_then_removed = _numstat(repository, base, first, "in_scope.txt")
    assert net == {"added": 0, "removed": 0, "files": 0}
    assert added_then_removed["added"] > 0

    stored = crew.complete(
        run_id, gate="passed", commits=[first, tip], root=repository
    )["record"]

    assert stored["changed_lines"]["added"] == net["added"]
    assert stored["changed_lines"]["removed"] == net["removed"]
    assert added_then_removed["added"] != net["added"]
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)
