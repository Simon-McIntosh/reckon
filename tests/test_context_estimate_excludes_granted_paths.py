"""Context estimates charge declared reads, not dispatcher-owned landing targets.

A node's landing scope is its own fragment, so the estimator exempts the
fragment dispatch grants and nothing else. The plan's shared landing paths — the
plan HTML, the cumulative evidence record and the plan-wide figure topic — are
no longer dispatcher-owned, so naming one is an ordinary read declaration and is
charged like any other.
"""

from __future__ import annotations

from pathlib import Path

from reckon import crew
from reckon.crew.dispatch import _grant_landing_write_paths
from reckon.crew.routing import _context_file_inputs

PROJECT = "sample"
PLAN = "context-accounting"
NODE = "context-accounting"
PLAN_PATH = f"docs/plans/{PLAN}.html"
EVIDENCE_PATH = f"docs/evidence/archive/{PLAN}-landed.html"
FRAGMENT_PATH = f"docs/evidence/fragments/{PLAN}/{NODE}.html"
FIGURE_PATH = f"docs/figures/{PLAN}/{NODE}"


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
