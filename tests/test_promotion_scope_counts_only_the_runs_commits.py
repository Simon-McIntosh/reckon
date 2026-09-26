"""Promotion measures a run's scope from the commits it cites.

Measured 2026-09-25: run
r-20260925T092209664940-active-sprint-follows-the-live-crew reported 152 paths
outside its fence for a run whose own three commits touched 7, because the
boundary check diffed the tree span from the first cited commit's parent to the
run's tip — and a run that merged main into its head carries every path main
changed in that span. The scope a promotion charges a run is what its own cited
commits changed: reachable-from-main and merge-commit content belongs to the
branch the content came from, not to the run that merged it.

Per-commit diffs answer which paths the run touched, but not how much it
changed: summing each cited commit's own numstat counts churn the run netted
out for itself, so a path rewritten across two cited commits contributes both
revisions. The counts are therefore one net diff over the run's own commits,
restricted to the paths those commits touched. A citation list that measures no
path at all — a merge cited alone, whose own diff belongs to the branch it
brought — is refused rather than recorded as a run that changed nothing. Each
case's expectation is derived from the fixture repository rather than echoed
from the citation list it passed in. The boundary guard for stray peer edits at
declared paths exempts the paths the project publishes as shareable, leaving
the refusal for every path off that list.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing
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
    guard: bool = False,
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
    if guard:
        pointer["repository_tree_snapshot"] = routing._repository_tree_snapshot(
            repository
        )
    _write_json(pointer_path(run_id), pointer)


def _declare_shared_write_paths(repository: Path, *paths: str) -> None:
    """Declare repository-relative files the project admits concurrent claims on."""
    (repository / "docs" / "state" / PROJECT / "shared-write-paths.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "paths": [
                    {"path": path, "reason": "concurrent editors touch one file"}
                    for path in paths
                ],
            }
        ),
        encoding="utf-8",
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

    assert stored["commits"] == [
        _git(repository, "rev-parse", revision) for revision in cited
    ]
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


def test_a_merges_only_citation_is_refused_naming_the_merge(
    repository: Path, run_that_merges_main: tuple[Path, str, list[str]]
) -> None:
    """A citation list of merges measures no path, so nothing can be recorded.

    The run above lands its content in its own two commits, but a coordinator
    who cites only the merge it made presents a list from which nothing about
    the run can be measured: a merge's own diff belongs to the branch it
    brought. Before this refusal the promotion completed and recorded
    0 added / 0 removed / 0 files, so the row read as a measured run that
    changed nothing. The refusal names the commit it refuses, leaves no row
    and keeps the pointer so the run can be re-promoted with its own commits.
    """
    _, run_id, cited = run_that_merges_main
    merge = cited[1]

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[merge], root=repository)

    assert merge in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_two_citations_on_one_path_record_the_run_s_net_churn(
    repository: Path, tmp_path: Path
) -> None:
    """One path changed twice is charged once, as the run's own net diff reads.

    The first commit writes two lines and the second removes one of them, so
    the per-commit sum is 2 added / 1 removed where the net diff over the run's
    commits is 1 added / 0 removed. The expectation is the fixture's own
    ``git diff --numstat <base> <tip> -- in_scope.txt``, not a restatement of
    what the two commits did, and the case asserts the two readings differ so
    it cannot pass by measuring nothing.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "net-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-netted-churn"
    _pointer(repository, run_tree, run_id, base, write_paths=("in_scope.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nalpha\nalpha too\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: a first pass over the path")
    first = _git(run_tree, "rev-parse", "HEAD")

    (run_tree / "in_scope.txt").write_text("seed\nalpha\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: trim the second line again")
    tip = _git(run_tree, "rev-parse", "HEAD")

    stored = crew.complete(
        run_id, gate="passed", commits=[first, tip], root=repository
    )["record"]

    net = _numstat(repository, base, tip, "in_scope.txt")
    assert net == {"added": 1, "removed": 0, "files": 1}
    first_pass = _numstat(repository, base, first, "in_scope.txt")
    second_pass = _numstat(repository, first, tip, "in_scope.txt")
    assert first_pass["added"] + second_pass["added"] != net["added"]
    assert stored["changed_lines"] == net
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)


def test_abbreviated_citations_are_recorded_as_canonical_object_ids(
    repository: Path, run_that_merges_main: tuple[Path, str, list[str]]
) -> None:
    """The row carries what the repository resolves, not what the caller typed.

    The expectation is derived from the fixture repository, so the case pins
    the canonicalisation rather than echoing its own input: each abbreviation is
    a strict prefix of the id the row must hold, so an assertion that compared
    the row to the citation list it was given would fail on this case.
    """
    _, run_id, cited = run_that_merges_main
    abbreviated = [revision[:10] for revision in cited]
    assert abbreviated != cited

    stored = crew.complete(run_id, gate="passed", commits=abbreviated, root=repository)[
        "record"
    ]

    canonical = [_git(repository, "rev-parse", revision) for revision in abbreviated]
    assert stored["commits"] == canonical
    assert all(len(commit) == 40 for commit in stored["commits"])
    assert stored["commits"] != abbreviated
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_trailing_merge_does_not_charge_the_content_it_brought(
    repository: Path, tmp_path: Path
) -> None:
    """The run's net diff is headed at its own last commit, not at its tip.

    The run writes two lines, main rewrites the same path, and the merge that
    follows resolves the conflict to main's three lines. The run's own churn is
    its own commit's two added lines; heading the net diff at the run's tip
    reads main's three instead, which reaches the row through the merge.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "trailing-merge-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-trailing-merge"
    _pointer(repository, run_tree, run_id, base, write_paths=("in_scope.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nalpha\nbeta\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: two lines of the run's own")
    own = _git(run_tree, "rev-parse", "HEAD")

    (repository / "in_scope.txt").write_text(
        "peer one\npeer two\npeer three\n", encoding="utf-8"
    )
    _git(repository, "add", "in_scope.txt")
    _git(repository, "commit", "-q", "-m", "docs: a peer's rewrite of the path")

    subprocess.run(
        ["git", "merge", "main"],
        cwd=run_tree,
        capture_output=True,
        text=True,
        check=False,
    )
    (run_tree / "in_scope.txt").write_text(
        "peer one\npeer two\npeer three\n", encoding="utf-8"
    )
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "--no-edit")
    merge = _git(run_tree, "rev-parse", "HEAD")

    stored = crew.complete(
        run_id, gate="passed", commits=[own, merge], root=repository
    )["record"]

    own_churn = _numstat(repository, base, own, "in_scope.txt")
    at_tip = _numstat(repository, base, merge, "in_scope.txt")
    assert own_churn == {"added": 2, "removed": 0, "files": 1}
    assert at_tip["added"] != own_churn["added"]
    assert stored["changed_lines"] == own_churn
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_citation_list_that_changes_no_path_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A commit that changes nothing measures no path, so it is refused.

    The merge skip reaches the unmeasurable state by skipping; a citation list
    can also reach it directly. Recording a row of zero added, zero removed and
    zero files would put a measured-looking row in the ledger for a run whose
    cited commits carry no work at all. The refusal names the commit, leaves no
    row and keeps the pointer, so the run can be re-promoted from the commit
    that does carry its work.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "empty-citation-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-empty-citation"
    _pointer(repository, run_tree, run_id, base, write_paths=("in_scope.txt",))
    _assert_real_home_carries_no_pointer(run_id)

    _git(run_tree, "commit", "-q", "--allow-empty", "-m", "chore: no diff at all")
    empty = _git(run_tree, "rev-parse", "HEAD")
    assert _git(repository, "diff", "--numstat", f"{empty}^", empty) == ""

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[empty], root=repository)

    assert empty in str(refusal.value)
    assert "measures no path" in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_peer_edit_on_a_declared_shared_path_does_not_refuse_promotion(
    repository: Path, tmp_path: Path
) -> None:
    """A path the project publishes as shareable admits a concurrent editor.

    Dispatch admits a second claim on every path on that list, so a peer's
    uncommitted edit there says nothing about this run's boundary. Charging it
    refuses promotions for work that does not collide, while the run below
    lands its own change in its own fence. The peer tree keeps the edit
    throughout, so the case fails whenever the shared list is not consulted.
    """
    (repository / "shared.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "shared.txt")
    _git(repository, "commit", "-q", "-m", "chore: seed the shared path")
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    peer_tree = tmp_path / "peer-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    _git(repository, "worktree", "add", "-q", "--detach", str(peer_tree), "HEAD")
    run_id = "r-shared-peer-edit"
    _declare_shared_write_paths(repository, "shared.txt")
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("in_scope.txt", "shared.txt"),
        guard=True,
    )
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    commit = _git(run_tree, "rev-parse", "HEAD")

    (peer_tree / "shared.txt").write_text("peer in flight\n", encoding="utf-8")
    assert "shared.txt" in _git(peer_tree, "status", "--porcelain")

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]
    assert not pointer_path(run_id).exists()
    _assert_real_home_carries_no_pointer(run_id)


def test_a_peer_edit_on_a_declared_unshared_path_still_refuses_promotion(
    repository: Path, tmp_path: Path
) -> None:
    """A declared path the shared list does not name is still a boundary edit.

    The list is present and names the peer's other declared path; the peer
    dirties only ``in_scope.txt``, which it does not name. The refusal names
    that path, so the exemption is not a blanket one and a declared path off
    the list keeps the guard. No row is written and the pointer is kept.
    """
    (repository / "shared.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "shared.txt")
    _git(repository, "commit", "-q", "-m", "chore: seed the shared path")
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    peer_tree = tmp_path / "peer-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    _git(repository, "worktree", "add", "-q", "--detach", str(peer_tree), "HEAD")
    run_id = "r-unshared-peer-edit"
    _declare_shared_write_paths(repository, "shared.txt")
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("in_scope.txt", "shared.txt"),
        guard=True,
    )
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    commit = _git(run_tree, "rev-parse", "HEAD")

    (peer_tree / "in_scope.txt").write_text("peer stray\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "in_scope.txt" in message
    assert f"peer worktree {peer_tree}" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)


def test_an_unshared_claim_is_charged_even_beside_a_shared_one(
    repository: Path, tmp_path: Path
) -> None:
    """The exemption is per path, so one shared claim cannot mask an unshared one.

    The peer holds uncommitted edits on both declared paths at once: the file
    the list names and ``in_scope.txt``, which it does not. A tree-level
    exemption would let the shared edit carry the unshared one through, so the
    case pins the charged set: the refusal names the unshared path and does not
    name the shared one. No row is written and the pointer is kept.
    """
    (repository / "shared.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "shared.txt")
    _git(repository, "commit", "-q", "-m", "chore: seed the shared path")
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    peer_tree = tmp_path / "peer-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    _git(repository, "worktree", "add", "-q", "--detach", str(peer_tree), "HEAD")
    run_id = "r-beside-peer-edit"
    _declare_shared_write_paths(repository, "shared.txt")
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("in_scope.txt", "shared.txt"),
        guard=True,
    )
    _assert_real_home_carries_no_pointer(run_id)

    (run_tree / "in_scope.txt").write_text("seed\nrun\n", encoding="utf-8")
    _git(run_tree, "add", "in_scope.txt")
    _git(run_tree, "commit", "-q", "-m", "feat: the run's own change")
    commit = _git(run_tree, "rev-parse", "HEAD")

    (peer_tree / "shared.txt").write_text("peer in flight\n", encoding="utf-8")
    (peer_tree / "in_scope.txt").write_text("peer stray\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "in_scope.txt" in message
    assert "shared.txt" not in message
    assert f"peer worktree {peer_tree}" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    _assert_real_home_carries_no_pointer(run_id)
