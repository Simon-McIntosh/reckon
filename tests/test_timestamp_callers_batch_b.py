"""Batch B timestamp callers reproduce their recorded observable behaviour.

The recorded table below was produced at base a15e11cb over a shared nine-input
list: an aware ISO string, a Z-suffixed string, a naive string, a date-only
string, an epoch number, an empty string, None, a non-string object and garbage
text. A raised exception is recorded by its type and message. Every migrated
caller must produce the same output for every input, except the cells named in
CHANGED, where the base behaviour was machine dependent or rejected a value the
shared parser accepts; each changed cell names its base output and the reason
the unified rule replaces it.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from reckon import _timestamps, doccheck, project_state, schedule, sprint_liveness
from reckon.hooks import worker_stop

FIXED_TODAY = date(2024, 3, 10)
MANIFEST_MTIME = datetime(2024, 1, 1, tzinfo=UTC).timestamp()


class _Probe:
    """A non-string object whose repr carries no parsable timestamp."""

    def __repr__(self) -> str:
        return "<probe-object>"


INPUTS: dict[str, object] = {
    "aware_iso": "2024-03-05T06:07:08+02:00",
    "z_suffix": "2024-03-05T06:07:08Z",
    "naive": "2024-03-05T06:07:08",
    "date_only": "2024-03-05",
    "epoch": 1709620028,
    "empty": "",
    "none": None,
    "object": _Probe(),
    "garbage": "not a timestamp",
}

EXPECTED: dict[str, dict[str, object]] = json.loads(
    '{\n  "doccheck.modified_age_days": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 5\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 5\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "literal",\n      "value": null\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 5\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 5\n    }\n  },\n  "project_state._review_date": {\n    "aware_iso": {\n      "error": "ValueError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "date_only": {\n      "kind": "literal",\n      "value": "2024-03-05"\n    },\n    "empty": {\n      "error": "ValueError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "epoch": {\n      "error": "TypeError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "garbage": {\n      "error": "ValueError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "naive": {\n      "error": "ValueError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "none": {\n      "error": "TypeError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "object": {\n      "error": "TypeError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    },\n    "z_suffix": {\n      "error": "ValueError: reviewed_at must be a YYYY-MM-DD date",\n      "kind": "raised"\n    }\n  },\n  "project_state._validate_review": {\n    "aware_iso": {\n      "kind": "literal",\n      "value": "2024-03-05T06:07:08+02:00"\n    },\n    "date_only": {\n      "kind": "literal",\n      "value": "2024-03-05"\n    },\n    "empty": {\n      "error": "ValueError: findings[0].resolved_at is required for resolution fields",\n      "kind": "raised"\n    },\n    "epoch": {\n      "error": "ValueError: findings[0].resolved_at must be an ISO date or datetime",\n      "kind": "raised"\n    },\n    "garbage": {\n      "error": "ValueError: findings[0].resolved_at must be an ISO date or datetime",\n      "kind": "raised"\n    },\n    "naive": {\n      "kind": "literal",\n      "value": "2024-03-05T06:07:08"\n    },\n    "none": {\n      "error": "ValueError: findings[0].resolved_at is required for resolution fields",\n      "kind": "raised"\n    },\n    "object": {\n      "error": "ValueError: findings[0].resolved_at must be an ISO date or datetime",\n      "kind": "raised"\n    },\n    "z_suffix": {\n      "kind": "literal",\n      "value": "2024-03-05T06:07:08Z"\n    }\n  },\n  "schedule._stamp_millis": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 1709611628000.0\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 1709596800000.0\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "literal",\n      "value": null\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 1709618828000.0\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 1709618828000.0\n    }\n  },\n  "sprint_liveness._observed_seconds": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 1709611628.0\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 1709593200.0\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "error": "TypeError: fromisoformat: argument must be str",\n      "kind": "raised"\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 1709615228.0\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "error": "TypeError: fromisoformat: argument must be str",\n      "kind": "raised"\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 1709618828.0\n    }\n  },\n  "worker_stop._manifest_predates_attempt": {\n    "aware_iso": {\n      "kind": "bool",\n      "value": true\n    },\n    "date_only": {\n      "kind": "bool",\n      "value": true\n    },\n    "empty": {\n      "kind": "bool",\n      "value": false\n    },\n    "epoch": {\n      "kind": "bool",\n      "value": false\n    },\n    "garbage": {\n      "kind": "bool",\n      "value": false\n    },\n    "naive": {\n      "kind": "bool",\n      "value": true\n    },\n    "none": {\n      "kind": "bool",\n      "value": false\n    },\n    "object": {\n      "kind": "bool",\n      "value": false\n    },\n    "z_suffix": {\n      "kind": "bool",\n      "value": true\n    }\n  }\n}'
)

CHANGED: dict[str, dict[str, dict[str, object]]] = json.loads(
    '{\n  "sprint_liveness._observed_seconds": {\n    "date_only": {\n      "base": {\n        "kind": "number",\n        "value": 1709593200.0\n      },\n      "expected": {\n        "kind": "number",\n        "value": 1709596800.0\n      },\n      "reason": "the same naive reading as above: a date-only string carries no zone, so the unified parser reads it as UTC midnight rather than local midnight"\n    },\n    "epoch": {\n      "base": {\n        "error": "TypeError: fromisoformat: argument must be str",\n        "kind": "raised"\n      },\n      "expected": {\n        "kind": "number",\n        "value": 1709620028.0\n      },\n      "reason": "base rejected a non-string with the C parser\'s TypeError; parse_utc reads a numeric epoch below its millisecond threshold as whole seconds"\n    },\n    "naive": {\n      "base": {\n        "kind": "number",\n        "value": 1709615228.0\n      },\n      "expected": {\n        "kind": "number",\n        "value": 1709618828.0\n      },\n      "reason": "base read a naive string as local time through .timestamp(); parse_utc reads a naive value as UTC, which removes the dependence on the host timezone this node runs under"\n    },\n    "object": {\n      "base": {\n        "error": "TypeError: fromisoformat: argument must be str",\n        "kind": "raised"\n      },\n      "expected": {\n        "kind": "literal",\n        "value": null\n      },\n      "reason": "base raised TypeError on a non-string object; parse_utc returns None for a value of an unsupported type so the caller decides what an absent moment means"\n    }\n  }\n}'
)

MODULES = [doccheck, project_state, schedule, sprint_liveness, worker_stop]


def canon(value: object) -> dict[str, object]:
    if isinstance(value, datetime):
        return {
            "kind": "datetime",
            "iso": value.isoformat(),
            "aware": value.tzinfo is not None,
        }
    if isinstance(value, date):
        return {"kind": "date", "iso": value.isoformat()}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, (int, float)):
        return {"kind": "number", "value": value}
    if value is None:
        return {"kind": "literal", "value": None}
    if isinstance(value, str):
        return {"kind": "literal", "value": value}
    return {"kind": "literal", "value": repr(value)}


def _review_with(resolved_at: object) -> dict[str, object]:
    return {
        "reviewed_at": "2024-03-01",
        "reviewed_by": "rev",
        "basis": "basis",
        "version": 1,
        "findings": [
            {
                "id": "f-one",
                "code": "one-code",
                "category": "sprint",
                "severity": "warn",
                "subject": {"kind": "project", "id": "reckon"},
                "evidence": ["line"],
                "recommended_action": {
                    "verb": "close",
                    "owner_skill": "x",
                    "detail": "d",
                },
                "validated": "confirmed",
                "checked_at": "2024-03-01",
                "resolved_at": resolved_at,
                "resolved_by": "rev",
                "outcome": "done",
            }
        ],
        "priority": [],
    }


def _validate_review_cell(value: object) -> object:
    result = project_state._validate_review(_review_with(value))
    return result["findings"][0]["resolved_at"]


def _worker_stop_cell(value: object) -> object:
    previous = os.environ.get("RECKON_ATTEMPT_STARTED_AT")
    os.environ["RECKON_ATTEMPT_STARTED_AT"] = "" if value is None else str(value)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.md"
            manifest.write_text("status: in-progress\n")
            os.utime(manifest, (MANIFEST_MTIME, MANIFEST_MTIME))
            return worker_stop._manifest_predates_attempt(manifest)
    finally:
        if previous is None:
            os.environ.pop("RECKON_ATTEMPT_STARTED_AT", None)
        else:
            os.environ["RECKON_ATTEMPT_STARTED_AT"] = previous


CALLERS = {
    "doccheck.modified_age_days": lambda v: doccheck.modified_age_days(
        v, today=FIXED_TODAY
    ),
    "schedule._stamp_millis": schedule._stamp_millis,
    "sprint_liveness._observed_seconds": sprint_liveness._observed_seconds,
    "project_state._review_date": lambda v: project_state._review_date(
        v, "reviewed_at"
    ),
    "project_state._validate_review": _validate_review_cell,
    "worker_stop._manifest_predates_attempt": _worker_stop_cell,
}


def observe(name: str, value: object) -> dict[str, object]:
    try:
        return canon(CALLERS[name](value))
    except Exception as exc:  # noqa: BLE001 - a raised exception is the output
        return {"kind": "raised", "error": f"{type(exc).__name__}: {exc}"}


def expected_for(name: str, key: str) -> dict[str, object]:
    changed = CHANGED.get(name, {}).get(key)
    if changed is not None:
        return changed["expected"]  # type: ignore[return-value]
    return EXPECTED[name][key]  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("name", "key"),
    [(name, key) for name in CALLERS for key in INPUTS],
)
def test_migrated_caller_reproduces_recorded_output(name: str, key: str) -> None:
    assert observe(name, INPUTS[key]) == expected_for(name, key)


def test_changed_cells_are_named_with_base_and_reason() -> None:
    for name, cells in CHANGED.items():
        for key, entry in cells.items():
            assert entry["base"] == EXPECTED[name][key], (name, key)
            assert entry["expected"] != entry["base"], (name, key)
            assert entry["reason"], (name, key)


def test_no_fromisoformat_call_remains() -> None:
    offenders: dict[str, list[int]] = {}
    for module in MODULES:
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fromisoformat"
            ):
                offenders.setdefault(module.__name__, []).append(node.lineno)
    assert offenders == {}


def _parse_utc_import_sources(module: object) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text())
    sources: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "parse_utc" for alias in node.names
        ):
            sources.add(node.module or "")
    return sources


def test_each_module_binds_the_one_shared_parser() -> None:
    for module in MODULES:
        assert _parse_utc_import_sources(module) == {_timestamps.__name__}, (
            module.__name__
        )
