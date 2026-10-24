"""No reckon source teaches the retired deferred-work field.

Work found beyond a node becomes a plan section, or an instruction in the next
dispatch's prompt. The field that used to hold such work — ``follow_ons`` on a
manifest, ``follow_on_paths`` on a ledger row — is retired, and no comment,
docstring or string literal in ``reckon/`` or ``scripts/`` names it.

Four sites keep the retired spelling, because they are the map that lets a
record written before the rename still read. Each is a module-level assignment:

- ``RETIRED_OUTSIDE_SCOPE`` (``reckon/crew/reports.py``) — the manifest field;
- ``RETIRED_OUTSIDE_SCOPE_PATHS`` (``reckon/run_record.py``) — the ledger key;
- ``RETIRED_FINDING_SEVERITIES`` (``reckon/crew/review.py``) — the severity word;
- ``_DEFERRED_WORK_TERM`` (``reckon/doccheck.py``) — the plan-prose refusal.

The first three are named ``RETIRED_`` so the exemption is a rule rather than a
path list; the fourth is the pattern that refuses the term in a plan, which
cannot be written without carrying it.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNED_DIRECTORIES = ("reckon", "scripts")

RETIRED_TERM = re.compile(r"follow[-_]ons?", re.IGNORECASE)

# The names that make an assignment exempt. A ``RETIRED_`` constant maps a
# record written before the rename on the way in; ``_DEFERRED_WORK_TERM`` is the
# pattern that refuses the term in authored plan prose.
EXEMPT_NAME_PREFIX = "RETIRED_"
EXEMPT_NAMES = {"_DEFERRED_WORK_TERM"}

EXEMPT_SITES = (
    (Path("reckon/crew/reports.py"), "RETIRED_OUTSIDE_SCOPE"),
    (Path("reckon/run_record.py"), "RETIRED_OUTSIDE_SCOPE_PATHS"),
    (Path("reckon/crew/review.py"), "RETIRED_FINDING_SEVERITIES"),
    (Path("reckon/doccheck.py"), "_DEFERRED_WORK_TERM"),
)


def _assignment_targets(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names = [node.target.id]
    else:
        names = []
    return names


def _is_exempt_name(name: str) -> bool:
    return name.startswith(EXEMPT_NAME_PREFIX) or name in EXEMPT_NAMES


def retired_sites(source: str) -> list[int]:
    """Line numbers naming the retired term in prose, a docstring or a literal.

    A string inside a module-level assignment to an exempt name is the mapping
    that keeps an old record readable, so it is skipped; everything else is a
    site that still teaches the field. Identifiers are not scanned, because the
    exempt constants have to be spelled to be used.
    """
    tree = ast.parse(source)
    exempt_ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        if any(_is_exempt_name(name) for name in _assignment_targets(node)):
            exempt_ranges.append((node.lineno, node.end_lineno or node.lineno))

    def exempt(line: int) -> bool:
        return any(first <= line <= last for first, last in exempt_ranges)

    hits: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and RETIRED_TERM.search(node.value)
            and not exempt(node.lineno)
        ):
            hits.add(node.lineno)

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        if RETIRED_TERM.search(token.string) and not exempt(token.start[0]):
            hits.add(token.start[0])

    return sorted(hits)


def test_no_reckon_source_names_the_retired_field() -> None:
    offenders: list[str] = []
    for directory in SCANNED_DIRECTORIES:
        for path in sorted((ROOT / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            for line in retired_sites(source):
                text = source.splitlines()[line - 1].strip()
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {text}")
    assert offenders == [], "retired deferred-work field named:\n" + "\n".join(
        offenders
    )


def test_the_gate_sees_a_planted_term_and_allows_the_mapped_one() -> None:
    """A guard that reports nothing must be shown to see something present.

    The scan is asserted against a source that carries the term in a comment and
    in a literal, and against the two shapes that map an old record on the way
    in, so an empty result over ``reckon/`` is a measured absence.
    """
    planted = 'x = 1  # the follow-on list\nLABEL = "follow_ons"\n'
    assert retired_sites(planted) == [1, 2]

    mapped = 'RETIRED_OUTSIDE_SCOPE = "follow_ons"\n'
    assert retired_sites(mapped) == []

    pattern = '_DEFERRED_WORK_TERM = re.compile(r"\\bfollow-ons?\\b")\n'
    assert retired_sites(pattern) == []


def test_each_exempt_site_is_a_real_assignment() -> None:
    """The four exemptions name something that exists, not a phantom allowance."""
    for relative, name in EXEMPT_SITES:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        targets = {target for node in tree.body for target in _assignment_targets(node)}
        assert name in targets, f"{relative} has no module-level assignment {name}"
