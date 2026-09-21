"""Context estimates charge declared reads, not dispatcher-owned landing targets."""

from __future__ import annotations

from pathlib import Path

from reckon import crew
from reckon.crew.dispatch import _grant_landing_write_paths
from reckon.crew.routing import _context_file_inputs

PROJECT = "sample"
PLAN = "context-accounting"
PLAN_PATH = f"docs/plans/{PLAN}.html"
EVIDENCE_PATH = f"docs/evidence/archive/{PLAN}-landed.html"


def _node(*, write_paths: list[str], done_when: str = "") -> crew.TaskNode:
    return crew.TaskNode(
        id="context-accounting",
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


def test_granted_landing_targets_remain_writable_but_are_not_read_context(
    tmp_path: Path,
) -> None:
    """Large evidence can be appended without becoming worker read context."""
    plan, evidence = _seed_plan_and_evidence(tmp_path)
    node = _node(write_paths=[])

    _grant_landing_write_paths(node, project=PROJECT, authority=_authority(tmp_path))
    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))
    records = _records_by_path(inputs["write_paths"])

    assert PLAN_PATH in node.write_paths
    assert EVIDENCE_PATH in node.write_paths
    assert tokens == 0
    assert records[PLAN_PATH]["provenance"] == "granted"
    assert records[EVIDENCE_PATH]["provenance"] == "granted"
    assert records[PLAN_PATH]["counted"] is False
    assert records[EVIDENCE_PATH]["counted"] is False
    assert records[PLAN_PATH]["bytes"] == plan.stat().st_size
    assert records[EVIDENCE_PATH]["bytes"] == evidence.stat().st_size


def test_explicit_landing_input_remains_chargeable_after_the_other_grants(
    tmp_path: Path,
) -> None:
    """A caller-owned evidence path is a read declaration, even when it is a landing target."""
    _plan, _evidence = _seed_plan_and_evidence(tmp_path)
    node = _node(
        write_paths=[EVIDENCE_PATH],
        done_when=f"the estimate reads {EVIDENCE_PATH} as a declared evidence input",
    )

    _grant_landing_write_paths(node, project=PROJECT, authority=_authority(tmp_path))
    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))
    records = _records_by_path(inputs["write_paths"])
    named_records = _records_by_path(inputs["named_files"])

    assert PLAN_PATH in node.write_paths
    assert EVIDENCE_PATH in node.write_paths
    assert records[PLAN_PATH]["provenance"] == "granted"
    assert records[EVIDENCE_PATH]["provenance"] == "granted"
    assert records[PLAN_PATH]["counted"] is False
    assert records[EVIDENCE_PATH]["counted"] is False
    assert named_records[EVIDENCE_PATH]["provenance"] == "declared"
    assert named_records[EVIDENCE_PATH]["counted"] is True
    assert tokens == named_records[EVIDENCE_PATH]["estimated_tokens"]
    assert tokens > 1_000_000
