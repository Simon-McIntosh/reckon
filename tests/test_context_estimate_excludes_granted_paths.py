"""Context estimates charge a declared read in full and an exclusion as a reference.

A node's landing scope is its own fragment, so the estimator exempts the
fragment dispatch grants and nothing else. The plan's shared landing paths — the
plan HTML, the cumulative evidence record and the plan-wide figure topic — are
no longer dispatcher-owned, so naming one is an ordinary read declaration and is
charged like any other. A clause that excludes a path from the declared inputs
is the reverse of a read: it declares no load, because the sentence tells the
worker not to open the file. The path is still named, so it is charged one flat
reference cost rather than its size — and an exclusion governs only the paths
its own verb names, so one clause may declare a path at full and exclude another
at the reference.
"""

from __future__ import annotations

import math
from pathlib import Path

from reckon import crew
from reckon.crew.dispatch import _grant_landing_write_paths
from reckon.crew.routing import _REFERENCE_COST_TOKENS, _context_file_inputs

PROJECT = "sample"
PLAN = "context-accounting"
NODE = "context-accounting"
PLAN_PATH = f"docs/plans/{PLAN}.html"
EVIDENCE_PATH = f"docs/evidence/archive/{PLAN}-landed.html"
FRAGMENT_PATH = f"docs/evidence/fragments/{PLAN}/{NODE}.html"
FIGURE_PATH = f"docs/figures/{PLAN}/{NODE}"
LARGE_INPUT_PATH = "package/large.py"
WRITE_PATH = "package/target.py"
DECLARED_INPUT_PATH = "package/a.py"
FIRST_EXCLUDED_PATH = "package/b.py"
SECOND_EXCLUDED_PATH = "package/c.py"
BYTES_PER_TOKEN = 3.5


def _node(*, write_paths: list[str], done_when: str = "") -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE,
        goal="measure a declared context input",
        plan=PLAN,
        section="s4",
        role="implement",
        spec_level="exact",
        done_when=done_when or "the context estimate identifies every charged input",
        write_paths=write_paths,
        time_budget="8m",
    )


def _authority(repo: Path) -> dict[str, object]:
    docs = repo / "docs"
    return {
        "plan": {
            "project": PROJECT,
            "repository": str(repo),
            "docs": str(docs),
        }
    }


def _seed_plan_and_evidence(repo: Path) -> tuple[Path, Path]:
    plan = repo / PLAN_PATH
    evidence = repo / EVIDENCE_PATH
    plan.parent.mkdir(parents=True)
    evidence.parent.mkdir(parents=True)
    plan.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '</head><body><h2 id="s4">Context</h2></body></html>',
        encoding="utf-8",
    )
    evidence.write_bytes(b"e" * 4_000_000)
    return plan, evidence


def _records_by_path(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["declared"]): record for record in records}


def test_the_granted_set_is_the_fragment_and_never_a_shared_landing_path(
    tmp_path: Path,
) -> None:
    """The estimator exempts the fragment it grants, and no shared record."""
    plan, _evidence = _seed_plan_and_evidence(tmp_path)
    node = _node(write_paths=[])
    authority = _authority(tmp_path)

    _grant_landing_write_paths(node, project=PROJECT, authority=authority, warnings=[])
    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["write_paths"])

    assert FRAGMENT_PATH in node.write_paths
    assert FIGURE_PATH in node.write_paths
    assert records[FRAGMENT_PATH]["provenance"] == "granted"
    assert records[FIGURE_PATH]["provenance"] == "granted"
    assert records[FRAGMENT_PATH]["counted"] is False
    assert tokens == 0
    # The retired shared landing paths are no longer dispatcher-owned reads.
    assert PLAN_PATH not in node.write_paths
    assert EVIDENCE_PATH not in node.write_paths
    assert plan.stat().st_size > 0


def test_a_named_shared_record_remains_a_chargeable_read(tmp_path: Path) -> None:
    """A declared shared record is a read declaration, not a granted target."""
    _plan, evidence = _seed_plan_and_evidence(tmp_path)
    node = _node(
        write_paths=[EVIDENCE_PATH],
        done_when=f"the estimate reads {EVIDENCE_PATH} as a declared evidence input",
    )
    authority = _authority(tmp_path)
    warnings: list[str] = []

    _grant_landing_write_paths(
        node, project=PROJECT, authority=authority, warnings=warnings
    )
    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["write_paths"])
    named_records = _records_by_path(inputs["named_files"])

    assert FRAGMENT_PATH in node.write_paths
    assert EVIDENCE_PATH in node.write_paths
    assert any("reintroduces the merge conflict" in warning for warning in warnings)
    assert records[EVIDENCE_PATH]["provenance"] == "declared"
    assert records[EVIDENCE_PATH]["provenance"] != "granted"
    assert named_records[EVIDENCE_PATH]["provenance"] == "declared"
    assert tokens == records[EVIDENCE_PATH]["estimated_tokens"]
    assert evidence.stat().st_size > 1_000_000
    assert tokens > 1_000_000


def _seed_named_inputs(repo: Path) -> None:
    """A large file a brief may exclude, beside the small write path it edits."""
    package = repo / "package"
    package.mkdir(parents=True)
    (repo / WRITE_PATH).write_bytes(b"v" * 10)
    (repo / LARGE_INPUT_PATH).write_bytes(b"a" * 200_007)
    (repo / DECLARED_INPUT_PATH).write_bytes(b"a" * 21)
    (repo / FIRST_EXCLUDED_PATH).write_bytes(b"b" * 200_000)
    (repo / SECOND_EXCLUDED_PATH).write_bytes(b"c" * 100_000)


def _estimated_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / BYTES_PER_TOKEN)


def test_a_plain_exclusion_clause_charges_a_reference(tmp_path: Path) -> None:
    """A clause telling the worker not to read a file buys one reference, not its size."""
    _seed_named_inputs(tmp_path)
    authority = _authority(tmp_path)
    node = _node(
        write_paths=[WRITE_PATH],
        done_when=f"Exclude {LARGE_INPUT_PATH}; do not read it",
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["named_files"])

    # The named file is real and large, so the charge is a decision about the
    # exclusion rather than about a missing file: the reference, plus the small
    # write path, and nothing like the file's own size.
    assert (tmp_path / LARGE_INPUT_PATH).stat().st_size > 200_000
    assert records[LARGE_INPUT_PATH]["provenance"] == "reference"
    assert records[LARGE_INPUT_PATH]["estimated_tokens"] == _REFERENCE_COST_TOKENS
    assert (
        _estimated_tokens((tmp_path / LARGE_INPUT_PATH).stat().st_size)
        > _REFERENCE_COST_TOKENS
    )
    assert tokens == _REFERENCE_COST_TOKENS + _estimated_tokens(
        (tmp_path / WRITE_PATH).stat().st_size
    )


def test_an_excluded_path_is_charged_a_reference_when_the_clause_says_declared_inputs(
    tmp_path: Path,
) -> None:
    """Naming a path in an exclusion declares no read; it pays the reference."""
    _seed_named_inputs(tmp_path)
    authority = _authority(tmp_path)
    node = _node(
        write_paths=[WRITE_PATH],
        done_when=f"Exclude {LARGE_INPUT_PATH} from declared inputs",
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["named_files"])

    assert records[LARGE_INPUT_PATH]["provenance"] == "reference"
    assert records[LARGE_INPUT_PATH]["counted"] is True
    assert tokens == _REFERENCE_COST_TOKENS + _estimated_tokens(
        (tmp_path / WRITE_PATH).stat().st_size
    )


def test_the_declaration_form_of_the_same_clause_charges_the_file(
    tmp_path: Path,
) -> None:
    """Only the verb moves between the exclusion and the declaration, and the
    charge returns: the fixture's bytes put the large file at 57,145 tokens and
    the write path at 3, so the declared form totals 57,148."""
    _seed_named_inputs(tmp_path)
    authority = _authority(tmp_path)
    node = _node(
        write_paths=[WRITE_PATH],
        done_when=f"Treat {LARGE_INPUT_PATH} as a declared input",
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["named_files"])
    expected = _estimated_tokens(
        (tmp_path / LARGE_INPUT_PATH).stat().st_size
    ) + _estimated_tokens((tmp_path / WRITE_PATH).stat().st_size)

    assert expected == 57_148
    assert tokens == expected
    assert records[LARGE_INPUT_PATH]["counted"] is True
    assert records[LARGE_INPUT_PATH]["estimated_tokens"] == _estimated_tokens(
        (tmp_path / LARGE_INPUT_PATH).stat().st_size
    )


def test_a_mixed_clause_keeps_its_declared_path_charged(tmp_path: Path) -> None:
    """A clause declaring one path and excluding another charges each by its verb.

    The exclusion verb governs the path it names, not the whole clause, so the
    declared path stays a full read while the excluded one pays the reference.
    """
    _seed_named_inputs(tmp_path)
    authority = _authority(tmp_path)
    node = _node(
        write_paths=[],
        done_when=(
            f"Declare {DECLARED_INPUT_PATH} as an input and avoid {FIRST_EXCLUDED_PATH}"
        ),
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["named_files"])

    # Both files exist, so the two rates are decisions about the verbs rather
    # than about what is present.
    assert (tmp_path / FIRST_EXCLUDED_PATH).stat().st_size > 100_000
    assert set(records) == {DECLARED_INPUT_PATH, FIRST_EXCLUDED_PATH}
    assert records[DECLARED_INPUT_PATH]["provenance"] == "declared"
    assert records[DECLARED_INPUT_PATH]["counted"] is True
    assert records[FIRST_EXCLUDED_PATH]["provenance"] == "reference"
    assert records[FIRST_EXCLUDED_PATH]["estimated_tokens"] == _REFERENCE_COST_TOKENS
    assert (
        tokens
        == _estimated_tokens((tmp_path / DECLARED_INPUT_PATH).stat().st_size)
        + _REFERENCE_COST_TOKENS
    )


def test_the_excluded_first_order_of_a_mixed_clause_reads_the_same_way(
    tmp_path: Path,
) -> None:
    """Reversing the two phrases moves no charge between them: the declared path
    stays full and the excluded one stays a reference."""
    _seed_named_inputs(tmp_path)
    authority = _authority(tmp_path)
    node = _node(
        write_paths=[],
        done_when=(
            f"Avoid {FIRST_EXCLUDED_PATH} and declare {DECLARED_INPUT_PATH} as an input"
        ),
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["named_files"])

    assert set(records) == {DECLARED_INPUT_PATH, FIRST_EXCLUDED_PATH}
    assert records[DECLARED_INPUT_PATH]["provenance"] == "declared"
    assert records[DECLARED_INPUT_PATH]["counted"] is True
    assert records[FIRST_EXCLUDED_PATH]["provenance"] == "reference"
    assert (
        tokens
        == _estimated_tokens((tmp_path / DECLARED_INPUT_PATH).stat().st_size)
        + _REFERENCE_COST_TOKENS
    )


def test_one_exclusion_verb_governs_every_path_it_lists(tmp_path: Path) -> None:
    """A second path joined to an exclusion list pays the reference with the first."""
    _seed_named_inputs(tmp_path)
    authority = _authority(tmp_path)
    node = _node(
        write_paths=[],
        done_when=(
            f"Exclude {FIRST_EXCLUDED_PATH} and {SECOND_EXCLUDED_PATH} "
            "from the declared inputs"
        ),
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, authority)
    records = _records_by_path(inputs["named_files"])

    assert (tmp_path / SECOND_EXCLUDED_PATH).stat().st_size > 50_000
    assert set(records) == {FIRST_EXCLUDED_PATH, SECOND_EXCLUDED_PATH}
    assert all(record["provenance"] == "reference" for record in records.values())
    assert tokens == 2 * _REFERENCE_COST_TOKENS
