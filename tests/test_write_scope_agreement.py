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
