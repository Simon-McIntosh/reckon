"""Promoted-ledger row fields are schema-complete and explicitly declared."""

from __future__ import annotations

from typing import Mapping

import pytest

from reckon import flight, ledger


def _promoted_ledger_key_union() -> tuple[set[str], list[str], list[str]]:
    """Collect every key in promoted rows across mounted ledgers."""
    mount_paths: list[str] = []
    record_keys: set[str] = set()
    for project, docs in sorted(flight.mounted_project_docs().items()):
        docs_root = docs.parent
        mount_paths.append(str(ledger.ledger_path(project, docs_root)))
        data, _ = ledger.load(project, docs_root)
        for record in data.get("runs", []):
            if isinstance(record, Mapping):
                record_keys.update(str(key) for key in record.keys())
    return record_keys, mount_paths


def test_promoted_record_fields_are_all_declared() -> None:
    """Collect keys from every mounted promoted row and assert the schema covers them."""
    declared: set[str] = set(ledger.RECORD_FIELDS)
    observed, mount_paths = _promoted_ledger_key_union()
    if not observed:
        pytest.skip(
            "no promoted ledger rows were read; checked mounted ledger paths: "
            + ", ".join(mount_paths)
        )

    assert "execution_fit" in declared
    assert "attempt_kind" in declared
    assert "attempt" in declared
    assert "throughput" in declared
    assert "worktree_retention" in declared
    assert "no_commit" in declared
    assert "dispute_count" in declared
    assert "follow_on_paths" in declared
    assert "scope_acceptances" in declared
    assert "resume_waiver" in declared
    assert "watch_override" in declared
    assert "predecessor_run" in declared
    assert "boundary_waiver" in declared
    assert "commit_resolution" in declared

    assert len(ledger.RECORD_FIELDS) == len(set(ledger.RECORD_FIELDS))
    assert all(isinstance(field, str) and field for field in ledger.RECORD_FIELDS)
    assert len(ledger.RECORD_FIELDS) >= len(observed)
    assert len(observed) >= 50, (
        f"expected at least fifty declared keys in the mounted union; found {len(observed)}"
    )

    undeclared = sorted(observed - declared)
    assert not undeclared, (
        "promoted ledger rows carry undeclared keys: " + ", ".join(undeclared)
    )
