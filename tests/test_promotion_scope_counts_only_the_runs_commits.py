"""Promotion measures a run's scope from the commits it cites.

Measured 2026-09-25 by the s23 coordinator: run
r-20260925T092209664940-active-sprint-follows-the-live-crew reported 152 paths
outside its fence for a run whose own three commits touched 7, because the
boundary check diffed the tree span from the first cited commit's parent to the
run's tip — and a run that merged main into its head carries every path main
changed in that span. The scope a promotion charges a run is what its own cited
commits changed: reachable-from-main and merge-commit content belongs to the
branch the content came from, not to the run that merged it.
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


def _real_pointer_dir() -> Path:
    return Path.home() / ".config" / "reckon" / "crew" / "live"


def _assert_real_home_carries_no_pointer(run_id: str) -> None:
    """The synthesised run must never reach the operator's own config home.

    A reader can only be wrong; a writer makes someone else wrong, and a test
    that writes its live pointer into the real pointer directory collides with
    concurrent runs. This asserts the write direction, not just the read.
    """
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
    _write_json(
        pointer_path(run_id),
        {
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
        },
    )


@pytest.fixture()
def run_that_merges_main(
    repository: Path, tmp_path: Path
) -> tuple[Path, str, list[str]]:
    """A run that commits in its fence, merges a peer's main advance, commits again.

    Returns the run worktree, its run id, and the commits it cites. The merge is
    cited too, because a coordinator presenting a run's work presents everything
    the run did; the peer's path reaches the run's tree only through that merge.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-run-merges-main"
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("in_scope.txt", "second.txt"),
    )
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    first = _git(run_tree, "rev-parse", "HEAD")

    (repository / "peer.txt").write_text("peer\n", encoding="utf-8")
    _git(repository, "add", "peer.txt")
    _git(repository, "commit", "-q", "-m", "docs: a peer's own change")

    _git(run_tree, "merge", "-q", "--no-ff", "main", "-m", "Merge main into the run")
    merge = _git(run_tree, "rev-parse", "HEAD")

    (run_tree / "second.txt").write_text("second\n", encoding="utf-8")
    _git(run_tree, "add", "second.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's second change")
    tip = _git(run_tree, "rev-parse", "HEAD")

    return run_tree, run_id, [first, merge, tip]


def test_a_merge_from_main_charges_only_the_run_s_own_paths(
    repository: Path, run_that_merges_main: tuple[Path, str, list[str]]
) -> None:
    """The peer path main changed is not the run's work and must not be counted.

    Under a span diff the run is refused for a path it never touched, and
    --accept-path cannot get round it when the path is live-claimed by the peer
    that does own it.
    """
    _, run_id, cited = run_that_merges_main

    stored = crew.complete(run_id, gate="passed", commits=cited, root=repository)[
        "record"
    ]

    assert stored["commits"] == cited
    assert stored["changed_lines"] == {"added": 2, "removed": 0, "files": 2}
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)


def test_the_run_s_own_path_outside_its_fence_is_still_refused(
    repository: Path, run_that_merges_main: tuple[Path, str, list[str]]
) -> None:
    """Counting the run's own commits must keep the fence it exists for.

    The run's last commit writes a path no one declared: the refusal names that
    path and not the peer's, which reached the tree through the merge alone.
    """
    run_tree, run_id, cited = run_that_merges_main
    (run_tree / "outside.txt").write_text("undeclared\n", encoding="utf-8")
    _git(run_tree, "add", "outside.txt")
    _git(run_tree, "commit", "-q", "-m", "chore: reach past the fence")
    tip = _git(run_tree, "rev-parse", "HEAD")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[*cited, tip], root=repository)

    assert "outside.txt" in str(refusal.value)
    assert "peer.txt" not in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)
