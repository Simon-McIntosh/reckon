"""A scope refusal names the paths it parsed.

A manifest's list fields are split on commas and newlines. A worker that writes
several ``changed_paths`` entries separated by spaces gets a value read as a
*single* item holding internal whitespace, and the write-scope audit then finds
that one item outside every declared root. Printed among the strays, one joined
item reads as the several paths it names, and the only sign the audit parsed one
item is the ``", "`` it joins them with — so the worker reasons the checker must
be comparing against some other record and goes looking for it.

The audit names the reading instead: when a stray item's own whitespace-separated
parts each lie within the declared write scope, it is a space-separated list, and
one finding names the field, the single item parsed and the comma rule, in place
of a scope finding for that item. The parse is unchanged, so the space-separated
value stays a defect the worker fixes. A genuinely stray path still draws a scope
finding, and every stray path is quoted, so a single path can never read as
several.

The accepted forms are asserted beside the refusal: the same three paths written
comma-separated raise no scope finding, and a path outside the declaration that
is not a joined list is reported as the stray it is.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.node import TaskNode
from reckon.crew.reports import audit_manifest

NODE_ID = "a-scope-refusal-names-the-paths-it-parsed"

# The three paths a worker declares and writes. Each lies within the write scope
# on its own, so a value that joins them is a list to split rather than a path to
# refuse.
DECLARED_PATHS = (
    "reckon/crew/reports.py",
    "tests/test_a_scope_refusal_names_the_paths_it_parsed.py",
    (
        "docs/evidence/fragments/a-worker-cannot-write-your-home/"
        "a-scope-refusal-names-the-paths-it-parsed.html"
    ),
)

SPACE_SEPARATED = " ".join(DECLARED_PATHS)
COMMA_SEPARATED = ", ".join(DECLARED_PATHS)

# A path the node never declared, and so never wrote.
STRAY_PATH = "src/power_convertor/switched_circuit.py"

SCOPE_PREFIX = "changed paths outside the write scope:"


def _manifest(changed_paths: str) -> str:
    return "\n".join(
        [
            f"node: {NODE_ID}",
            "status: complete",
            "commits: 1a2b3c4",
            "tests: pytest -q tests/x.py (passed)",
            f"changed_paths: {changed_paths}",
        ]
    )


def _node() -> TaskNode:
    return TaskNode(
        id=NODE_ID,
        goal="name the paths a scope refusal parsed",
        plan="a-worker-cannot-write-your-home",
        write_paths=list(DECLARED_PATHS),
    )


def _audit(changed_paths: str, tree: Path) -> dict:
    return audit_manifest(
        _manifest(changed_paths), _node(), worktree=tree, repository=tree
    )


def _separation_findings(findings: list[str]) -> list[str]:
    return [line for line in findings if "changed_paths" in line and "comma" in line]


def _scope_findings(findings: list[str]) -> list[str]:
    return [line for line in findings if line.startswith(SCOPE_PREFIX)]


def test_a_space_separated_changed_paths_is_reported_as_one_joined_item(
    tmp_path: Path,
) -> None:
    # The three declared paths written on one line separated by spaces: the value
    # arrives as one item whose parts are each in scope, so the audit names the
    # reading rather than raising a scope finding for the item.
    audit = _audit(SPACE_SEPARATED, tmp_path)
    findings = audit["findings"]

    separation = _separation_findings(findings)
    assert len(separation) == 1, findings
    assert SPACE_SEPARATED in separation[0]
    assert _scope_findings(findings) == []
    assert audit["ok"] is False


def test_the_same_paths_comma_separated_draw_no_finding(tmp_path: Path) -> None:
    # Written the way the parser reads a list, the three paths are three in-scope
    # entries, so neither a separation finding nor a scope finding is raised.
    audit = _audit(COMMA_SEPARATED, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == []


def test_a_genuinely_stray_path_is_reported_quoted(tmp_path: Path) -> None:
    # A path outside the declaration is a stray, and the scope finding quotes it,
    # so a single path can never read as several.
    audit = _audit(STRAY_PATH, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == [f'{SCOPE_PREFIX} "{STRAY_PATH}"']


def test_a_joined_item_naming_a_path_outside_the_scope_stays_a_stray(
    tmp_path: Path,
) -> None:
    # A joined value whose parts are not all in scope is not a space-separated
    # list of declared paths, so it is refused as the stray item it is, quoted
    # whole.
    joined = f"{DECLARED_PATHS[0]} {STRAY_PATH}"
    audit = _audit(joined, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == [f'{SCOPE_PREFIX} "{joined}"']
