"""The six recovery timestamp readers reproduce their recorded behaviour.

Each timestamp reader in ``reckon/crew/recovery.py`` -- ``_budget_timing``,
``_worker_launched_after_manifest``, ``_attempt_started_seconds``,
``_manifest_wait``, ``_run_chain_manifest_freshness`` and
``_manifest_wait``/``_seconds_since_dispatch`` -- resolves through
``reckon._timestamps.parse_utc``. The table below records what the BASE
revision's functions produced for each reader over one shared six-input list --
a ``Z`` stamp, an offset stamp, a naive stamp, an empty string, a malformed
string and a non-string value -- and asserts each reader still produces exactly
that at head.

The base revision was read from a scratch tree (``git archive HEAD``) and the
table captured by driving those functions directly; all 36 cells agree between
base and head. Every reader keeps the ``str`` coercion and naive-as-UTC rule it
already had, so the only substitution is the parser itself.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _timestamps

recovery = importlib.import_module("reckon.crew.recovery")
READING_MODULES = (
    Path(recovery.__file__).with_name("recovery_liveness.py"),
    Path(recovery.__file__).with_name("recovery_stream.py"),
)

FIXED_MOMENT = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
MANIFEST_MTIME = datetime(2026, 9, 29, tzinfo=UTC).timestamp()
BASELINE_NS = 10**19

INPUTS: dict[str, object] = {
    "z_suffix": "2026-09-29T01:02:03Z",
    "offset": "2026-09-29T01:02:58+02:00",
    "naive": "2026-09-29T01:02:03",
    "empty": "",
    "malformed": "not a timestamp",
    "non_string": 1709620028,
}


def canon(value: object) -> object:
    if isinstance(value, datetime):
        return {
            "kind": "datetime",
            "iso": value.isoformat(),
            "aware": value.tzinfo is not None,
        }
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, dict):
        return {"kind": "dict", "json": {k: canon(v) for k, v in sorted(value.items())}}
    if isinstance(value, (list, tuple)):
        return {"kind": "list", "items": [canon(v) for v in value]}
    if isinstance(value, (int, float)):
        return {"kind": "number", "value": value}
    if value is None:
        return {"kind": "literal", "value": None}
    if isinstance(value, str):
        return {"kind": "literal", "value": value}
    return {"kind": "literal", "value": repr(value)}


def _fresh_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "manifest.md"
    manifest.write_text("x")
    os.utime(manifest, (MANIFEST_MTIME, MANIFEST_MTIME))
    return manifest


def _budget_timing(value: object, tmp_path: Path) -> object:
    return recovery._budget_timing(
        {"attempt_budget_seconds": 3600, "created_at": value},
        now_seconds=FIXED_MOMENT,
    )


def _worker_launched_after_manifest(value: object, tmp_path: Path) -> object:
    (tmp_path / "worker.json").write_text(json.dumps({"launched_at": value}))
    manifest = _fresh_manifest(tmp_path)
    return recovery._worker_launched_after_manifest(
        {"log_path": str(tmp_path / "stream.log")}, manifest
    )


def _attempt_started_seconds(value: object, tmp_path: Path) -> object:
    return recovery._attempt_started_seconds(
        {"log_path": str(tmp_path / "stream.log"), "attempt_started_at": value}
    )


def _manifest_wait(value: object, tmp_path: Path) -> object:
    manifest = _fresh_manifest(tmp_path)
    data = {
        "status": "waiting",
        "wait_condition": "the job leaves the queue",
        "wait_probe": ["squeue", "-h", "-j", "1"],
        "resume_brief": "continue",
        "wait_started_at": value,
    }
    return recovery._manifest_wait(
        data, manifest, now_seconds=FIXED_MOMENT, stale_after_seconds=600
    )


def _run_chain_manifest_freshness(value: object, tmp_path: Path) -> object:
    manifest = _fresh_manifest(tmp_path)
    return recovery._run_chain_manifest_freshness(
        {
            "attempt": 2,
            "manifest_path": str(manifest),
            "manifest_baseline_mtime_ns": BASELINE_NS,
            "created_at": value,
        }
    )


def _seconds_since_dispatch(value: object, tmp_path: Path) -> object:
    return recovery._seconds_since_dispatch({"created_at": value}, FIXED_MOMENT)


CALLERS = {
    "budget_timing": _budget_timing,
    "worker_launched_after_manifest": _worker_launched_after_manifest,
    "attempt_started_seconds": _attempt_started_seconds,
    "manifest_wait": _manifest_wait,
    "run_chain_manifest_freshness": _run_chain_manifest_freshness,
    "seconds_since_dispatch": _seconds_since_dispatch,
}


def observe(name: str, value: object, tmp_path: Path) -> object:
    try:
        return canon(CALLERS[name](value, tmp_path))
    except Exception as exc:  # noqa: BLE001 - a raised exception is the output
        return {"kind": "raised", "error": f"{type(exc).__name__}: {exc}"}


@pytest.mark.parametrize(
    ("name", "key"), [(name, key) for name in CALLERS for key in INPUTS]
)
def test_reader_reproduces_recorded_output(name: str, key: str, tmp_path: Path) -> None:
    assert observe(name, INPUTS[key], tmp_path) == EXPECTED[name][key]


def test_recorded_table_covers_every_reader_and_input() -> None:
    assert set(EXPECTED) == set(CALLERS)
    for name, row in EXPECTED.items():
        assert set(row) == set(INPUTS), name


def _fromisoformat_enclosing_functions() -> list[str]:
    return [
        node.name
        for path in READING_MODULES
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef)
        and any(_calls_fromisoformat(sub) for sub in ast.walk(node))
    ]


def _calls_fromisoformat(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fromisoformat"
    )


def test_no_fromisoformat_call_remains() -> None:
    assert _fromisoformat_enclosing_functions() == []


def test_recovery_binds_the_shared_parser() -> None:
    for path in READING_MODULES:
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _timestamps.__name__:
                imported.update(alias.name for alias in node.names)
        assert "parse_utc" in imported, path


EXPECTED: dict[str, dict[str, dict[str, object]]] = json.loads(
    '{"attempt_started_seconds": {"empty": {"kind": "literal", "value": null}, "malformed": {"kind": "literal", "value": null}, "naive": {"kind": "number", "value": 1790643723.0}, "non_string": {"kind": "literal", "value": null}, "offset": {"kind": "number", "value": 1790636578.0}, "z_suffix": {"kind": "number", "value": 1790643723.0}}, "budget_timing": {"empty": {"json": {"budget_overrun": {"kind": "bool", "value": false}, "budget_overrun_seconds": {"kind": "number", "value": 0}, "budget_seconds": {"kind": "literal", "value": null}, "elapsed_seconds": {"kind": "literal", "value": null}}, "kind": "dict"}, "malformed": {"json": {"budget_overrun": {"kind": "bool", "value": false}, "budget_overrun_seconds": {"kind": "number", "value": 0}, "budget_seconds": {"kind": "literal", "value": null}, "elapsed_seconds": {"kind": "literal", "value": null}}, "kind": "dict"}, "naive": {"json": {"budget_overrun": {"kind": "bool", "value": true}, "budget_overrun_seconds": {"kind": "number", "value": 79077}, "budget_seconds": {"kind": "number", "value": 3600}, "elapsed_seconds": {"kind": "number", "value": 82677}}, "kind": "dict"}, "non_string": {"json": {"budget_overrun": {"kind": "bool", "value": false}, "budget_overrun_seconds": {"kind": "number", "value": 0}, "budget_seconds": {"kind": "literal", "value": null}, "elapsed_seconds": {"kind": "literal", "value": null}}, "kind": "dict"}, "offset": {"json": {"budget_overrun": {"kind": "bool", "value": true}, "budget_overrun_seconds": {"kind": "number", "value": 86222}, "budget_seconds": {"kind": "number", "value": 3600}, "elapsed_seconds": {"kind": "number", "value": 89822}}, "kind": "dict"}, "z_suffix": {"json": {"budget_overrun": {"kind": "bool", "value": true}, "budget_overrun_seconds": {"kind": "number", "value": 79077}, "budget_seconds": {"kind": "number", "value": 3600}, "elapsed_seconds": {"kind": "number", "value": 82677}}, "kind": "dict"}}, "manifest_wait": {"empty": {"json": {"age_seconds": {"kind": "number", "value": 86400}, "condition": {"kind": "literal", "value": "the job leaves the queue"}, "error": {"kind": "literal", "value": "missing or invalid wait_terminal"}, "expected_horizon_seconds": {"kind": "number", "value": 600}, "files": {"items": [], "kind": "list"}, "overdue": {"kind": "bool", "value": true}, "probe": {"items": [{"kind": "literal", "value": "squeue"}, {"kind": "literal", "value": "-h"}, {"kind": "literal", "value": "-j"}, {"kind": "literal", "value": "1"}], "kind": "list"}, "resume_brief": {"kind": "literal", "value": "continue"}, "signature": {"kind": "literal", "value": "wait:6a1e11133a0fd2e9"}, "started_at": {"kind": "literal", "value": "2026-09-29T00:00:00Z"}, "terminal": {"items": [], "kind": "list"}, "valid": {"kind": "bool", "value": false}, "wait_key_defect": {"kind": "literal", "value": ""}}, "kind": "dict"}, "malformed": {"json": {"age_seconds": {"kind": "number", "value": 86400}, "condition": {"kind": "literal", "value": "the job leaves the queue"}, "error": {"kind": "literal", "value": "missing or invalid wait_terminal, readable wait_started_at"}, "expected_horizon_seconds": {"kind": "number", "value": 600}, "files": {"items": [], "kind": "list"}, "overdue": {"kind": "bool", "value": true}, "probe": {"items": [{"kind": "literal", "value": "squeue"}, {"kind": "literal", "value": "-h"}, {"kind": "literal", "value": "-j"}, {"kind": "literal", "value": "1"}], "kind": "list"}, "resume_brief": {"kind": "literal", "value": "continue"}, "signature": {"kind": "literal", "value": "wait:6a1e11133a0fd2e9"}, "started_at": {"kind": "literal", "value": "not a timestamp"}, "terminal": {"items": [], "kind": "list"}, "valid": {"kind": "bool", "value": false}, "wait_key_defect": {"kind": "literal", "value": ""}}, "kind": "dict"}, "naive": {"json": {"age_seconds": {"kind": "number", "value": 82677}, "condition": {"kind": "literal", "value": "the job leaves the queue"}, "error": {"kind": "literal", "value": "missing or invalid wait_terminal"}, "expected_horizon_seconds": {"kind": "number", "value": 600}, "files": {"items": [], "kind": "list"}, "overdue": {"kind": "bool", "value": true}, "probe": {"items": [{"kind": "literal", "value": "squeue"}, {"kind": "literal", "value": "-h"}, {"kind": "literal", "value": "-j"}, {"kind": "literal", "value": "1"}], "kind": "list"}, "resume_brief": {"kind": "literal", "value": "continue"}, "signature": {"kind": "literal", "value": "wait:6a1e11133a0fd2e9"}, "started_at": {"kind": "literal", "value": "2026-09-29T01:02:03"}, "terminal": {"items": [], "kind": "list"}, "valid": {"kind": "bool", "value": false}, "wait_key_defect": {"kind": "literal", "value": ""}}, "kind": "dict"}, "non_string": {"json": {"age_seconds": {"kind": "number", "value": 86400}, "condition": {"kind": "literal", "value": "the job leaves the queue"}, "error": {"kind": "literal", "value": "missing or invalid wait_terminal, readable wait_started_at"}, "expected_horizon_seconds": {"kind": "number", "value": 600}, "files": {"items": [], "kind": "list"}, "overdue": {"kind": "bool", "value": true}, "probe": {"items": [{"kind": "literal", "value": "squeue"}, {"kind": "literal", "value": "-h"}, {"kind": "literal", "value": "-j"}, {"kind": "literal", "value": "1"}], "kind": "list"}, "resume_brief": {"kind": "literal", "value": "continue"}, "signature": {"kind": "literal", "value": "wait:6a1e11133a0fd2e9"}, "started_at": {"kind": "literal", "value": "1709620028"}, "terminal": {"items": [], "kind": "list"}, "valid": {"kind": "bool", "value": false}, "wait_key_defect": {"kind": "literal", "value": ""}}, "kind": "dict"}, "offset": {"json": {"age_seconds": {"kind": "number", "value": 89822}, "condition": {"kind": "literal", "value": "the job leaves the queue"}, "error": {"kind": "literal", "value": "missing or invalid wait_terminal"}, "expected_horizon_seconds": {"kind": "number", "value": 600}, "files": {"items": [], "kind": "list"}, "overdue": {"kind": "bool", "value": true}, "probe": {"items": [{"kind": "literal", "value": "squeue"}, {"kind": "literal", "value": "-h"}, {"kind": "literal", "value": "-j"}, {"kind": "literal", "value": "1"}], "kind": "list"}, "resume_brief": {"kind": "literal", "value": "continue"}, "signature": {"kind": "literal", "value": "wait:6a1e11133a0fd2e9"}, "started_at": {"kind": "literal", "value": "2026-09-29T01:02:58+02:00"}, "terminal": {"items": [], "kind": "list"}, "valid": {"kind": "bool", "value": false}, "wait_key_defect": {"kind": "literal", "value": ""}}, "kind": "dict"}, "z_suffix": {"json": {"age_seconds": {"kind": "number", "value": 82677}, "condition": {"kind": "literal", "value": "the job leaves the queue"}, "error": {"kind": "literal", "value": "missing or invalid wait_terminal"}, "expected_horizon_seconds": {"kind": "number", "value": 600}, "files": {"items": [], "kind": "list"}, "overdue": {"kind": "bool", "value": true}, "probe": {"items": [{"kind": "literal", "value": "squeue"}, {"kind": "literal", "value": "-h"}, {"kind": "literal", "value": "-j"}, {"kind": "literal", "value": "1"}], "kind": "list"}, "resume_brief": {"kind": "literal", "value": "continue"}, "signature": {"kind": "literal", "value": "wait:6a1e11133a0fd2e9"}, "started_at": {"kind": "literal", "value": "2026-09-29T01:02:03Z"}, "terminal": {"items": [], "kind": "list"}, "valid": {"kind": "bool", "value": false}, "wait_key_defect": {"kind": "literal", "value": ""}}, "kind": "dict"}}, "run_chain_manifest_freshness": {"empty": {"items": [{"kind": "bool", "value": true}, {"kind": "bool", "value": false}], "kind": "list"}, "malformed": {"items": [{"kind": "bool", "value": true}, {"kind": "bool", "value": false}], "kind": "list"}, "naive": {"items": [{"kind": "bool", "value": true}, {"kind": "bool", "value": false}], "kind": "list"}, "non_string": {"items": [{"kind": "bool", "value": true}, {"kind": "bool", "value": false}], "kind": "list"}, "offset": {"items": [{"kind": "bool", "value": true}, {"kind": "bool", "value": true}], "kind": "list"}, "z_suffix": {"items": [{"kind": "bool", "value": true}, {"kind": "bool", "value": false}], "kind": "list"}}, "seconds_since_dispatch": {"empty": {"kind": "literal", "value": null}, "malformed": {"kind": "literal", "value": null}, "naive": {"kind": "number", "value": 82677.0}, "non_string": {"kind": "literal", "value": null}, "offset": {"kind": "number", "value": 89822.0}, "z_suffix": {"kind": "number", "value": 82677.0}}, "worker_launched_after_manifest": {"empty": {"kind": "bool", "value": false}, "malformed": {"kind": "bool", "value": false}, "naive": {"kind": "bool", "value": true}, "non_string": {"kind": "bool", "value": false}, "offset": {"kind": "bool", "value": false}, "z_suffix": {"kind": "bool", "value": true}}}'
)
