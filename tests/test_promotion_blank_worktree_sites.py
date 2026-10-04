"""Every remaining blank-worktree idiom in promotion reads no ambient repository.

Seven readers in ``reckon/crew/promotion.py`` still built their tree as
``Path(str(record.get("worktree") or ""))`` with no guard that an empty field is
absent, and ``Path("")`` is ``Path(".")``: a record whose worktree field was
blank resolved through whatever directory the promotion happened to start in,
and answered about a repository the run was never dispatched for.

The readers that name a run's own tree — the commit directory, the worktree
change and unchanged-since-base measurements, the manifest's citation check,
the commitless gate guard's worktree reading and the boundary scan's own-tree
exclusion — now resolve through the shared record reader, which returns no tree
for a record that names no readable directory, or guard the blank field where
the reader is deliberately worktree-only. A shadow's patch reader refuses, as
it already intended to, rather than diffing the current directory.

Each resolved reader is exercised from inside an unrelated git repository whose
facts are adversarial — its head is both the recorded base and the cited
commit, or the very file a declaration covers is dirty — so a reader that fell
back to the cwd answers with that repository's facts and fails here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion
from reckon.crew.node import CrewError
from reckon.crew.runs import run_dir


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _seed_repository(root: Path, marker: str) -> str:
    """Two commits whose content differs per repository, so heads never collide."""
    root.mkdir(parents=True)
    (root / "file.txt").write_text(f"seed {marker}\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", f"seed {marker}")
    (root / "file.txt").write_text(f"seed {marker}\nwork {marker}\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", f"work {marker}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def unrelated_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """A git repository the run was never dispatched for, as the cwd.

    The crew home is a temporary directory too, so a reader that reaches for a
    run directory or the live fleet reads an empty store rather than the
    workstation's own state.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "unrelated"
    head = _seed_repository(root, "unrelated")
    monkeypatch.chdir(root)
    return root, head


def test_run_commit_directory_names_no_tree_for_a_blank_field(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A blank field names no directory at all, not the ambient checkout.

    The caller reads the answer through ``is_dir`` to decide whether a citation
    can be measured; ``Path("")`` is a directory, so the old spelling had the
    caller measure in whatever repository the promotion started in.
    """
    root, _head = unrelated_repository

    assert promotion._run_commit_directory({"worktree": "", "repo": ""}) is None
    assert promotion._run_commit_directory({"worktree": str(root), "repo": ""}) == root


def test_worktree_repository_changes_ignores_the_ambient_repository(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A blank field measures no change; the dirty ambient file is not reported.

    The unrelated repository holds an uncommitted modification, which a reader
    that fell back to the cwd would return as the run's repository change.
    """
    root, head = unrelated_repository
    (root / "file.txt").write_text("changed in the unrelated repository\n")
    record = {"worktree": "", "repo": "", "base_sha": head}

    assert promotion._worktree_repository_changes(record) == ()


def test_manifest_declares_no_change_ignores_the_ambient_repository(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A citation cannot be settled against the ambient repository's history.

    The unrelated repository's head is both the recorded base and the cited
    commit, the shape that reads as proved, and it is admitted only in the
    run's own tree: with none named the claim is not settled at all.
    """
    _root, head = unrelated_repository
    record = {"role": "review", "worktree": "", "repo": "", "base_sha": head}
    manifest_text = (
        f"changed_paths: none - the review delivers a report\ncommits: [{head}]\n"
    )
    manifest = {"changed_paths": [], "commits": [head]}

    assert (
        promotion._manifest_declares_no_change(record, manifest, manifest_text) is False
    )


def test_worktree_unchanged_since_base_ignores_the_ambient_repository(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A clean ambient checkout at the recorded base does not prove the run clean.

    The unrelated repository sits exactly on the recorded base with no change,
    so a reader that fell back to the cwd would grant the unchanged-run
    exemption; the same facts measured in a named tree still grant it.
    """
    root, head = unrelated_repository
    blank = {"worktree": "", "repo": "", "base_sha": head}
    named = {"worktree": str(root), "repo": "", "base_sha": head}

    assert promotion._worktree_unchanged_since_base(blank) is False
    assert promotion._worktree_unchanged_since_base(named) is True


def test_require_gate_evidence_stays_silent_for_a_blank_worktree(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A blank worktree field asks nothing, rather than asking the ambient HEAD.

    The recorded base is the unrelated repository's first commit, so a reader
    that read the cwd's HEAD would refuse this truthful commitless promotion
    naming a tip from a tree the run never worked in.
    """
    root, head = unrelated_repository
    base = _git(root, "rev-list", "--max-parents=0", "HEAD")
    record = {"role": "implement", "worktree": "", "repo": "", "base_sha": base}

    assert head != base
    assert (
        promotion._require_gate_evidence(
            "r-blank-worktree",
            record,
            verdict="passed",
            commits=(),
            no_commit_reason="",
        )
        is None
    )


def test_boundary_violations_read_no_ambient_own_tree(
    unrelated_repository: tuple[Path, str],
) -> None:
    """An uncommitted edit in the main checkout is reported, not excluded as own.

    The record names no worktree, and the promotion runs from the main checkout
    itself: a reader that resolved the blank field to the cwd excluded that tree
    from the walk, so an edit at a declared path there went unreported. The
    baseline snapshot is the record's own, so the edit is seen against it.
    """
    root, head = unrelated_repository
    (root / "file.txt").write_text("an uncommitted edit in the main checkout\n")
    run_id = "r-blank-boundary"
    record = {
        "project": "blank-worktree-fixture",
        "repo": str(root),
        "worktree": "",
        "base_sha": head,
        "node": {"write_paths": ["file.txt"]},
        "repository_tree_snapshot": {
            "trees": [
                {
                    "path": str(root),
                    "available": True,
                    "status_digest": "the digest before the edit",
                    "status_entries": [],
                }
            ]
        },
    }

    violations = promotion._repository_tree_boundary_violations(run_id, record)

    assert any("file.txt" in violation for violation in violations)
    assert any("main checkout" in violation for violation in violations)


def test_write_shadow_patch_refuses_a_blank_worktree(
    unrelated_repository: tuple[Path, str],
) -> None:
    """A shadow with no named worktree refuses instead of diffing the cwd.

    The unrelated repository's head is the shadow's recorded base, so a reader
    that resolved the blank field to the current directory would produce a
    patch of the ambient checkout and preserve it as the shadow's work.
    """
    _root, head = unrelated_repository
    run_id = "r-blank-shadow"
    record = {
        "run_id": run_id,
        "worktree": "",
        "repo": "",
        "base_sha": head,
        "lineage": {"kind": "shadow"},
    }

    with pytest.raises(CrewError, match="no readable worktree"):
        promotion._write_shadow_patch(record)

    assert not (run_dir(run_id) / "shadow.patch").exists()
