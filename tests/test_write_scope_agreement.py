"""The write-time and promotion-time write-scope tests agree on one contract.

Two surfaces judge a changed path against a node's declared write paths: the
audit of a manifest at the moment a worker writes it, and the containment test
promotion applies hours later. A dispatch that grants a directory must read the
same at both. Where the earlier check judged exact membership while the later
one judged containment, a worker whose manifest was already right was sent to
repair it — a stricter contract at the earlier surface, which is the drift this
module's pair of entry points exists to catch.

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


def _write_time_accepts(changed: str, declared: list[str]) -> bool:
    """Enter through the write-time audit; True when it finds nothing stray."""
    audit = audit_manifest(_manifest(changed), _node(declared))
    return bool(audit["ok"])


def _promotion_accepts(changed: str, declared: list[str], tree: Path) -> bool:
    """Enter through the promotion-time test; True when nothing falls outside."""
    outside = _outside_declared_scope(
        [changed], declared, record={"repo": str(tree)}, tree=tree
    )
    return not outside


def test_a_declared_path_is_accepted_by_both(tmp_path: Path) -> None:
    assert _write_time_accepts(FILE_DECLARATION, [FILE_DECLARATION]) is True
    assert _promotion_accepts(FILE_DECLARATION, [FILE_DECLARATION], tmp_path) is True


def test_a_path_under_a_declared_directory_is_accepted_by_both(
    tmp_path: Path,
) -> None:
    changed = f"{DIRECTORY_DECLARATION}/scope.svg"

    assert _write_time_accepts(changed, [DIRECTORY_DECLARATION]) is True
    assert _promotion_accepts(changed, [DIRECTORY_DECLARATION], tmp_path) is True


def test_a_path_under_no_declared_path_is_rejected_by_both(tmp_path: Path) -> None:
    changed = "reckon/crew/promotion.py"

    assert _write_time_accepts(changed, [FILE_DECLARATION]) is False
    assert _promotion_accepts(changed, [FILE_DECLARATION], tmp_path) is False


@pytest.mark.parametrize(("changed", "declared"), SCOPE_PAIRS)
def test_the_two_surfaces_agree(
    changed: str, declared: list[str], tmp_path: Path
) -> None:
    assert _write_time_accepts(changed, declared) == _promotion_accepts(
        changed, declared, tmp_path
    )
