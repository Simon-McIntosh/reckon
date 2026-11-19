"""A scope refusal names the paths it parsed.

A manifest's list fields are split on commas and newlines, and a guarded list
field — ``changed_paths`` among them — is then read entry by entry: a line whose
whitespace-separated tokens are each entry-shaped is read as those several
entries. A worker that writes several ``changed_paths`` entries separated by
spaces therefore gets the several paths it named, and the write-scope audit
judges each of them against the declared roots.

The scope finding names what it judged: a stray path is quoted, so a single path
can never read as several. A stray path of a class a merge cannot reconcile
still draws that finding, while a companion source path draws none — the
declaration is a walk hint rather than a fence, because the worktree isolates
the edit and the merge reconciles it.

The accepted forms are asserted beside the refusals: the same three paths
written space-separated and comma-separated are read as the same three entries
and raise no finding, and a merge-unsafe path outside the declaration is
reported as the stray it is, quoted.

The separation finding in ``_write_scope_findings`` — the one that names a
joined item and the comma rule — is unreachable from ``changed_paths`` for that
reason, which the last case pins.
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

# A path the node never declared, and so never wrote. It is a source file, and a
# source file is advisory, so it draws no scope finding at all.
STRAY_PATH = "src/power_convertor/switched_circuit.py"

# A path whose class a merge cannot reconcile, so an undeclared one is still a
# stray the audit reports. A figure topic is replaced wholesale.
MERGE_UNSAFE_STRAY = "docs/figures/another-topic/plot.svg"

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


def test_a_space_separated_changed_paths_reads_as_the_paths_it_names(
    tmp_path: Path,
) -> None:
    # The three declared paths written on one line separated by spaces: the entry
    # reader splits a line of entry-shaped tokens, so the value is read as the
    # three in-scope entries it names and draws no finding at all.
    audit = _audit(SPACE_SEPARATED, tmp_path)

    assert audit["manifest"]["changed_paths"] == list(DECLARED_PATHS)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == []
    assert audit["ok"] is True


def test_the_same_paths_comma_separated_draw_no_finding(tmp_path: Path) -> None:
    # Written the way the parser reads a list, the three paths are three in-scope
    # entries, so neither a separation finding nor a scope finding is raised.
    audit = _audit(COMMA_SEPARATED, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == []


def test_a_merge_unsafe_stray_path_is_reported_quoted(tmp_path: Path) -> None:
    # An undeclared path whose class a merge cannot reconcile is a stray, and the
    # scope finding quotes it, so a single path can never read as several.
    audit = _audit(MERGE_UNSAFE_STRAY, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == [
        f'{SCOPE_PREFIX} "{MERGE_UNSAFE_STRAY}"'
    ]


def test_a_companion_source_path_draws_no_scope_finding(tmp_path: Path) -> None:
    # A declaration is a hint. A source file beside the declared ones is not a
    # stray: the worktree isolates the edit and the merge reconciles it.
    audit = _audit(STRAY_PATH, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == []


def test_a_joined_item_naming_companion_source_paths_draws_no_scope_finding(
    tmp_path: Path,
) -> None:
    # The parser splits a space-separated value on whitespace, so the audit never
    # receives one joined item; its parts are both source paths here, so neither
    # is a stray and the audit reports nothing. The space-separated branch of
    # _write_scope_findings is therefore unreachable from changed_paths.
    joined = f"{DECLARED_PATHS[0]} {STRAY_PATH}"
    audit = _audit(joined, tmp_path)
    assert _separation_findings(audit["findings"]) == []
    assert _scope_findings(audit["findings"]) == []
