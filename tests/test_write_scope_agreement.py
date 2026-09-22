"""The write-time and promotion-time write-scope tests agree on one contract.

Two surfaces judge a changed path against a node's declared write paths: the
audit of a manifest at the moment a worker writes it, and the containment test
promotion applies hours later. A dispatch that grants a directory must read the
same at both. Where the earlier check judged exact membership while the later
one judged containment, a worker whose manifest was already right was sent to
repair it — a stricter contract at the earlier surface, which is the drift this
module's pair of entry points exists to catch.

A declaration may also be written as an absolute path on disk rather than
relative to the repository. Resolving it before comparing is what lets an
absolute declaration naming a directory inside the repository accept the same
changed paths promotion accepts, and what keeps an absolute declaration naming
a location outside the repository rejecting a repository path at both surfaces.

Both surfaces are entered here for the same inputs, so a change to either one
that moves the contract shows up as a disagreement rather than as a worker
repairing a manifest promotion would have accepted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.promotion import _outside_declared_scope
from reckon.crew.reports import audit_manifest

FILE_DECLARATION = "reckon/crew/reports.py"
DIRECTORY_DECLARATION = "docs/figures/work-does-not-fall-to-the-coordinator"
OUTSIDE_DIRECTORY = "declared-outside-the-repository"

SCOPE_PAIRS = [
    # (changed path, declared write paths) — file declarations and directory
    # declarations, bare and trailing-slashed, plus the prefix case a
    # string-prefix test would wrongly accept.
    (FILE_DECLARATION, [FILE_DECLARATION]),
    (FILE_DECLARATION, [FILE_DECLARATION, "tests/test_write_scope_agreement.py"]),
    (f"{DIRECTORY_DECLARATION}/scope.svg", [DIRECTORY_DECLARATION]),
    (f"{DIRECTORY_DECLARATION}/panels/agreement.svg", [f"{DIRECTORY_DECLARATION}/"]),
    ("tests/test_write_scope_agreement.py", ["tests"]),
    (f"{FILE_DECLARATION}.bak", [FILE_DECLARATION]),
    ("reckon/crew/other.py", [FILE_DECLARATION, DIRECTORY_DECLARATION]),
]

# Absolute declarations that name a location inside the repository, so a
# changed path beneath them is in scope. Each is a case a comparison that does
# not resolve the declaration first would refuse while promotion accepts it.
ABSOLUTE_DECLARATIONS = (
    "inside-directory",
    "inside-directory-nested",
    "inside-directory-trailing-slash",
    "inside-file",
    "inside-tests-directory",
)


def _manifest(changed: str) -> str:
    return "\n".join(
        [
            "status: complete",
            "commits: 1a2b3c4",
            "tests: pytest -q tests/test_write_scope_agreement.py (passed)",
            f"changed_paths: {changed}",
        ]
    )


def _node(declared: list[str]) -> TaskNode:
    return TaskNode(
        id="node-a",
        goal="judge one changed path against the declared write scope",
        plan="plan-a",
        write_paths=list(declared),
    )


def _write_time_accepts(changed: str, declared: list[str], tree: Path) -> bool:
    """Enter through the write-time audit; True when it finds nothing stray."""
    audit = audit_manifest(
        _manifest(changed), _node(declared), worktree=tree, repository=tree
    )
    return bool(audit["ok"])


def _promotion_accepts(changed: str, declared: list[str], tree: Path) -> bool:
    """Enter through the promotion-time test; True when nothing falls outside."""
    outside = _outside_declared_scope(
        [changed], declared, record={"repo": str(tree)}, tree=tree
    )
    return not outside


def _absolute_pair(case: str, tree: Path) -> tuple[str, list[str]]:
    """Build a changed path paired with an absolute declaration on one tree."""
    inside = tree / DIRECTORY_DECLARATION
    return {
        "inside-directory": (f"{DIRECTORY_DECLARATION}/scope.svg", [str(inside)]),
        "inside-directory-nested": (
            f"{DIRECTORY_DECLARATION}/panels/agreement.svg",
            [str(inside)],
        ),
        "inside-directory-trailing-slash": (
            f"{DIRECTORY_DECLARATION}/panels/agreement.svg",
            [f"{inside}/"],
        ),
        "inside-file": (FILE_DECLARATION, [str(tree / FILE_DECLARATION)]),
        "inside-tests-directory": (
            "tests/test_write_scope_agreement.py",
            [str(tree / "tests")],
        ),
    }[case]


def test_a_declared_path_is_accepted_by_both(tmp_path: Path) -> None:
    assert _write_time_accepts(FILE_DECLARATION, [FILE_DECLARATION], tmp_path) is True
    assert _promotion_accepts(FILE_DECLARATION, [FILE_DECLARATION], tmp_path) is True


def test_a_path_under_a_declared_directory_is_accepted_by_both(
    tmp_path: Path,
) -> None:
    changed = f"{DIRECTORY_DECLARATION}/scope.svg"

    assert _write_time_accepts(changed, [DIRECTORY_DECLARATION], tmp_path) is True
    assert _promotion_accepts(changed, [DIRECTORY_DECLARATION], tmp_path) is True


def test_a_path_under_no_declared_path_is_rejected_by_both(tmp_path: Path) -> None:
    changed = "reckon/crew/promotion.py"

    assert _write_time_accepts(changed, [FILE_DECLARATION], tmp_path) is False
    assert _promotion_accepts(changed, [FILE_DECLARATION], tmp_path) is False


def test_a_path_under_an_absolute_inside_declaration_is_accepted_by_both(
    tmp_path: Path,
) -> None:
    changed = f"{DIRECTORY_DECLARATION}/scope.svg"
    declared = [str(tmp_path / DIRECTORY_DECLARATION)]

    assert _write_time_accepts(changed, declared, tmp_path) is True
    assert _promotion_accepts(changed, declared, tmp_path) is True


def test_an_absolute_outside_declaration_rejects_a_repository_path_in_both(
    tmp_path: Path,
) -> None:
    declared = [str(tmp_path.parent / OUTSIDE_DIRECTORY)]

    assert _write_time_accepts(FILE_DECLARATION, declared, tmp_path) is False
    assert _promotion_accepts(FILE_DECLARATION, declared, tmp_path) is False


@pytest.mark.parametrize(("changed", "declared"), SCOPE_PAIRS)
def test_the_two_surfaces_agree(
    changed: str, declared: list[str], tmp_path: Path
) -> None:
    assert _write_time_accepts(changed, declared, tmp_path) == _promotion_accepts(
        changed, declared, tmp_path
    )


@pytest.mark.parametrize("case", ABSOLUTE_DECLARATIONS)
def test_the_two_surfaces_agree_on_an_absolute_declaration(
    case: str, tmp_path: Path
) -> None:
    changed, declared = _absolute_pair(case, tmp_path)

    assert _write_time_accepts(changed, declared, tmp_path) == _promotion_accepts(
        changed, declared, tmp_path
    )


# A reclaimed run: its worktree directory was removed after the worker
# committed, so promotion reads the repository instead of the worktree. The
# recorded worktree path survives on the run record, which is what the
# write-time check resolves a declaration against — so promotion must resolve
# it there too, or an absolute grant under the vanished worktree reads as in
# scope at the check and as stray at promotion.
RECLAIMED_CASES = (
    "declared-directory",
    "declared-directory-nested",
    "declared-directory-trailing-slash",
    "declared-file",
    "declared-tests-directory",
    "undeclared-path",
)


def _reclaimed_run(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Build a run whose recorded worktree no longer exists on disk."""
    worktree = tmp_path / "worktree"
    repository = tmp_path / "repo"
    repository.mkdir()
    assert not worktree.exists()
    return worktree, repository, {"repo": str(repository), "worktree": str(worktree)}


def _reclaimed_pair(case: str, worktree: Path) -> tuple[str, list[str]]:
    """Return a changed path and an absolute declaration under the worktree."""
    inside = worktree / DIRECTORY_DECLARATION
    return {
        "declared-directory": (f"{DIRECTORY_DECLARATION}/scope.svg", [str(inside)]),
        "declared-directory-nested": (
            f"{DIRECTORY_DECLARATION}/panels/agreement.svg",
            [str(inside)],
        ),
        "declared-directory-trailing-slash": (
            f"{DIRECTORY_DECLARATION}/panels/agreement.svg",
            [f"{inside}/"],
        ),
        "declared-file": (FILE_DECLARATION, [str(worktree / FILE_DECLARATION)]),
        "declared-tests-directory": (
            "tests/test_write_scope_agreement.py",
            [str(worktree / "tests")],
        ),
        "undeclared-path": ("reckon/crew/other.py", [str(inside)]),
    }[case]


def _reclaimed_write_time_accepts(
    changed: str, declared: str | list[str], worktree: Path, repository: Path
) -> bool:
    audit = audit_manifest(
        _manifest(changed),
        _node(list(declared)),
        worktree=worktree,
        repository=repository,
    )
    return bool(audit["ok"])


def _reclaimed_promotion_accepts(
    changed: str,
    declared: str | list[str],
    worktree: Path,
    repository: Path,
    record: dict[str, str],
) -> bool:
    tree = worktree if worktree.is_dir() else repository
    outside = _outside_declared_scope(
        [changed], list(declared), record=record, tree=tree
    )
    return not outside


def test_a_reclaimed_run_accepts_a_path_under_a_declared_directory_at_both(
    tmp_path: Path,
) -> None:
    worktree, repository, record = _reclaimed_run(tmp_path)
    changed, declared = _reclaimed_pair("declared-directory", worktree)

    assert (
        _reclaimed_write_time_accepts(changed, declared, worktree, repository) is True
    )
    assert (
        _reclaimed_promotion_accepts(changed, declared, worktree, repository, record)
        is True
    )


def test_a_reclaimed_run_rejects_a_path_under_no_declaration_at_both(
    tmp_path: Path,
) -> None:
    worktree, repository, record = _reclaimed_run(tmp_path)
    changed, declared = _reclaimed_pair("undeclared-path", worktree)

    assert (
        _reclaimed_write_time_accepts(changed, declared, worktree, repository) is False
    )
    assert (
        _reclaimed_promotion_accepts(changed, declared, worktree, repository, record)
        is False
    )


def test_a_run_whose_worktree_still_exists_is_unaffected(tmp_path: Path) -> None:
    worktree, repository, record = _reclaimed_run(tmp_path)
    worktree.mkdir()
    changed, declared = _reclaimed_pair("declared-directory", worktree)

    assert worktree.is_dir() is True
    assert (
        _reclaimed_write_time_accepts(changed, declared, worktree, repository) is True
    )
    assert (
        _reclaimed_promotion_accepts(changed, declared, worktree, repository, record)
        is True
    )


@pytest.mark.parametrize("case", RECLAIMED_CASES)
def test_the_two_surfaces_agree_on_a_reclaimed_worktree(
    case: str, tmp_path: Path
) -> None:
    worktree, repository, record = _reclaimed_run(tmp_path)
    changed, declared = _reclaimed_pair(case, worktree)

    assert _reclaimed_write_time_accepts(
        changed, declared, worktree, repository
    ) == _reclaimed_promotion_accepts(changed, declared, worktree, repository, record)
