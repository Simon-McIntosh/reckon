"""Every derived manifest names work beyond a worker's scope as outside_scope.

Three places write or list the manifest field from a list reckon composes
itself rather than from a worker's own report: recovery's fabricated manifest,
the NEEDS-HELP report a failed peer wait writes, and the census fixture's own
key set. All three name the current field, and a manifest either writer emits
parses to an empty section rather than to an unrecognised key.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import dispatch_peer, recovery_watch
from reckon.crew.reports import parse_manifest
from reckon.crew.runs import _write_json, pointer_path

REPO_ROOT = Path(__file__).parents[1]
RETIRED = "follow_ons"
CURRENT = "outside_scope"


def _load_snapshot_assertions() -> Any:
    script = REPO_ROOT / "scripts" / "check_snapshot_assertions.py"
    name = f"check_snapshot_assertions_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_a_derived_manifest_names_outside_scope_and_parses_empty(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.md"
    record = {
        "node": {"id": "node-a"},
        "phase": "complete",
        "manifest_path": str(manifest),
        "final_message": "the worker ended without writing its manifest",
    }

    recovery_watch._derive_missing_manifest(record, config=None)

    text = manifest.read_text(encoding="utf-8")
    assert f"{CURRENT}: none" in text
    assert RETIRED not in text

    parsed = parse_manifest(text, path=str(manifest))
    assert parsed[CURRENT] == []
    assert RETIRED not in parsed


def test_a_needs_help_report_names_outside_scope_and_parses_empty(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-20260101T000000000000-derived-peer"
    manifest = tmp_path / "manifest.md"
    _write_json(
        pointer_path(run_id),
        {"run_id": run_id, "node": {"id": "node-a"}, "manifest_path": str(manifest)},
    )
    event = {
        "id": "q-1",
        "to_node": "peer-node",
        "question": "which interface should the caller use?",
    }

    dispatch_peer._needs_help_for_question(run_id, event, 0.5)

    text = manifest.read_text(encoding="utf-8")
    assert f"{CURRENT}: none" in text
    assert RETIRED not in text

    parsed = parse_manifest(text, path=str(manifest))
    assert parsed[CURRENT] == []
    assert RETIRED not in parsed


def test_the_snapshot_field_list_names_outside_scope() -> None:
    module = _load_snapshot_assertions()

    assert CURRENT in module.CREW_ROW_KEYS
    assert RETIRED not in module.CREW_ROW_KEYS


def test_no_ledger_docstring_carries_the_retired_spellings() -> None:
    source = (REPO_ROOT / "reckon" / "ledger.py").read_text(encoding="utf-8")
    banned = (RETIRED, "follow_on", "follow-on")

    found: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        docstring = ast.get_docstring(node, clean=False) or ""
        found.extend(
            (spelling, docstring) for spelling in banned if spelling in docstring
        )

    assert [spelling for spelling, _ in found] == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
