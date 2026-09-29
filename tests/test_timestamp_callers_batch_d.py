"""Batch D timestamp callers reproduce their recorded observable behaviour.

The recorded table below was produced at base cea6da07 by running the base code
over one shared sixteen-input list: an aware ISO string, a ``Z``-suffixed
string, a lowercase-``z`` string, a space-separated zoned stamp, a
space-separated naive stamp, an offset carrying seconds, a near-midnight
offset, a naive string, a date-only string, a valid date followed by trailing
text, a whitespace-padded string, an epoch number, an empty string, ``None``, a
non-string object and garbage text. A raised exception is recorded by its type.
A clock is frozen for the callers that age a stamp against ``now`` so their
output is a value rather than the moment of reading.

Every caller reproduces its base cell for every input. Where a value that
carries no zone is read as UTC, the base did the same, so the unified parser
lands the same instant.

Ten of the eleven callers now route their stamp through
``reckon._timestamps.parse_utc``; the eleventh is a retained exception whose
reason the table below states. The second test walks the six touched modules
with ``ast`` and asserts the only function still calling ``fromisoformat`` is
that retained exception.
"""

from __future__ import annotations

import ast
import io
import json
import tempfile
import time
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

import pytest

from reckon import _timestamps, cli, flight, ledger, mcp_views, serve, velocity

FIXED_NOW = datetime(2026, 9, 29, 2, 2, 3, tzinfo=UTC)
FIXED_EPOCH = FIXED_NOW.timestamp()


class _FakeDateTime(datetime):
    """A datetime whose ``now`` is the frozen instant these callers age against."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)


class _Probe:
    """A non-string object whose repr carries no parsable timestamp."""

    def __repr__(self) -> str:
        return "<probe-object>"


TICKED_MODULES = [cli, flight, serve, ledger, mcp_views]

INPUTS: dict[str, object] = {
    "aware_iso": "2026-09-29T01:02:03+00:00",
    "z_suffix": "2026-09-29T01:02:03Z",
    "lowercase_z": "2026-09-29T01:02:03z",
    "space_zoned": "2026-09-29 01:02:03+00:00",
    "space_naive": "2026-09-29 01:02:03",
    "offset_seconds": "2026-09-29T01:02:03+00:00:30",
    "offset_midnight": "2024-03-05T00:30:00+02:00",
    "naive": "2026-09-29T01:02:03",
    "date_only": "2024-03-05",
    "date_prefix_bogus": "2024-03-05bogus",
    "padded_z": "  2026-09-29T01:02:03Z  ",
    "epoch": 1709620028,
    "empty": "",
    "none": None,
    "object": _Probe(),
    "garbage": "not a timestamp",
}


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
    if isinstance(value, dict):
        return {"kind": "mapping", "value": value}
    if isinstance(value, list):
        return {"kind": "list", "value": value}
    return {"kind": "literal", "value": repr(value)}


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: FIXED_EPOCH)
    for module in TICKED_MODULES:
        monkeypatch.setattr(module, "datetime", _FakeDateTime)


def _event_completion_cell(value: object) -> object:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stream.jsonl"
        path.write_text(
            json.dumps({"timestamp": value}, default=repr) + "\n", encoding="utf-8"
        )
        return ledger._event_completion([path])


def _completion_key_cell(value: object) -> object:
    records = [
        {"run_id": "a", "completed_at": None, "dispatched_at": value},
        {"run_id": "b", "completed_at": None, "dispatched_at": "2024-03-05T00:00:00Z"},
    ]
    with mock.patch.object(serve.ledger, "runs", return_value=records):
        ordered = serve._finished_crew_rows({"reckon": Path("/x/state")}, "reckon")
    return [row["run_id"] for row in ordered]


def _compose_review_cell(value: object) -> object:
    review = {
        "reviewed_at": "2024-03-01",
        "findings": [
            {
                "id": "f-one",
                "subject": {"kind": "plan", "id": "p"},
                "checked_at": "2024-03-01",
                "resolved_at": "",
            }
        ],
        "priority": [],
    }
    inventory = [{"slug": "p", "type": "plan", "modified": value, "status": "active"}]
    composed = mcp_views.compose_review(review, inventory, [], "reckon")
    row = composed["findings"][0]
    return {"stale": row["stale"], "current": row["current"]}


def _measure_cell(value: object) -> object:
    run = {
        "run_id": "r1",
        "plan": "p",
        "node": "n",
        "role": "implement",
        "resolved_commits": [],
        "promotion_commits": [],
        "completed_at": "2024-03-05T06:07:08+00:00",
        "dispatched_at": "2024-03-05T06:00:00+00:00",
        "lineage": {},
        "attempt": 1,
        "backend": "local",
        "worker_seconds": 3600,
        "unresolved_or_unreachable_commits": [],
        "coordinator": "c",
        "gate": "g",
    }
    commit = {
        "sha": "abc",
        "subject": "fix: x",
        "epoch": 1709618828,
        "parents": ["p"],
        "product_patches": [],
        "files": [],
    }
    project = {
        "project": "reckon",
        "runs": [run],
        "commits": [commit],
        "primary_branch": "main",
        "head": "h",
        "base": "b",
        "ledger_sha256": "x",
        "per_run_files": [],
        "recovered_from_sqlite": [],
        "promotion_ids_without_record": [],
        "ledger_clock_recoveries": [],
    }
    with mock.patch("sys.stdout", new=io.StringIO()):
        result = velocity.measure(
            {"projects": [project]},
            window_start=value,
            window_end="2027-01-01T00:00:00Z",
        )
    window = result["window"]
    return {
        "kind": "mapping",
        "value": {
            "start": window["start"],
            "end": window["end"],
            "elapsed_days": window["elapsed_days"],
            "followup_through": window["complete_seven_day_followup_through"],
        },
    }


CALLERS = {
    "cli._follow_row_stamp": lambda v: cli._follow_row_stamp({"observed_at": v}),
    "flight._observation_age": flight._observation_age,
    "serve._elapsed_since": serve._elapsed_since,
    "serve.completion_key": _completion_key_cell,
    "ledger._worker_seconds": lambda v: ledger._worker_seconds(
        v, "2024-03-05T06:07:08+00:00"
    ),
    "ledger._parse_timestamp": lambda v: ledger._parse_timestamp(v, "stamp"),
    "ledger._event_completion": _event_completion_cell,
    "mcp_views._parsed_observation": mcp_views._parsed_observation,
    "mcp_views.compose_review": _compose_review_cell,
    "velocity.stamp": velocity.stamp,
    "velocity.measure": _measure_cell,
}


def observe(name: str, value: object) -> dict[str, object]:
    try:
        return canon(CALLERS[name](value))
    except Exception as exc:  # noqa: BLE001 - a raised exception is the output
        return {"kind": "raised", "error": type(exc).__name__}


@pytest.mark.parametrize(
    ("name", "key"),
    [(name, key) for name in CALLERS for key in INPUTS],
)
def test_migrated_caller_reproduces_recorded_output(name: str, key: str) -> None:
    assert observe(name, INPUTS[key]) == EXPECTED[name][key]


def test_recorded_table_covers_every_caller_and_input() -> None:
    assert set(EXPECTED) == set(CALLERS)
    for name, row in EXPECTED.items():
        assert set(row) == set(INPUTS), name


RETAINED_EXCEPTIONS: dict[str, str] = {
    "ledger._parse_timestamp": (
        "returns the moment in the zone the stamp was written in; the shared "
        "parser normalises every zone to UTC"
    ),
}


def _fromisoformat_functions() -> set[str]:
    offenders: set[str] = set()
    for module in [cli, flight, serve, ledger, mcp_views, velocity]:
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "fromisoformat"
                ):
                    offenders.add(f"{module.__name__.rsplit('.', 1)[-1]}.{node.name}")
    return offenders


def test_no_fromisoformat_call_remains_outside_the_retained_exceptions() -> None:
    assert _fromisoformat_functions() == set(RETAINED_EXCEPTIONS)


def test_every_retained_exception_states_its_reason() -> None:
    for name, reason in RETAINED_EXCEPTIONS.items():
        assert reason.strip(), name


def _parse_utc_import_sources(module: object) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text())
    sources: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "parse_utc" for alias in node.names
        ):
            sources.add(node.module or "")
    return sources


def test_each_touched_module_binds_the_one_shared_parser() -> None:
    for module in [cli, flight, serve, ledger, mcp_views, velocity]:
        assert _parse_utc_import_sources(module) == {_timestamps.__name__}, (
            module.__name__
        )


EXPECTED: dict[str, dict[str, object]] = json.loads(
    '{\n  "cli._follow_row_stamp": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "date_prefix_bogus": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "empty": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "epoch": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "garbage": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "lowercase_z": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "naive": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "none": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "object": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "offset_midnight": {\n      "kind": "number",\n      "value": 1709591400.0\n    },\n    "offset_seconds": {\n      "kind": "number",\n      "value": 1790643693.0\n    },\n    "padded_z": {\n      "kind": "number",\n      "value": 1790647323.0\n    },\n    "space_naive": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "space_zoned": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 1790643723.0\n    }\n  },\n  "flight._observation_age": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 3600.0\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 81050523.0\n    },\n    "date_prefix_bogus": {\n      "kind": "literal",\n      "value": null\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "number",\n      "value": 81027295.0\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "lowercase_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 3600.0\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "offset_midnight": {\n      "kind": "number",\n      "value": 81055923.0\n    },\n    "offset_seconds": {\n      "kind": "number",\n      "value": 3630.0\n    },\n    "padded_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_naive": {\n      "kind": "number",\n      "value": 3600.0\n    },\n    "space_zoned": {\n      "kind": "number",\n      "value": 3600.0\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 3600.0\n    }\n  },\n  "ledger._event_completion": {\n    "aware_iso": {\n      "kind": "literal",\n      "value": "2026-09-29T01:02:03+00:00"\n    },\n    "date_only": {\n      "kind": "literal",\n      "value": null\n    },\n    "date_prefix_bogus": {\n      "kind": "literal",\n      "value": null\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "literal",\n      "value": null\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "lowercase_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "literal",\n      "value": null\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "offset_midnight": {\n      "kind": "literal",\n      "value": "2024-03-05T00:30:00+02:00"\n    },\n    "offset_seconds": {\n      "kind": "literal",\n      "value": "2026-09-29T01:02:03+00:00:30"\n    },\n    "padded_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_naive": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_zoned": {\n      "kind": "literal",\n      "value": "2026-09-29 01:02:03+00:00"\n    },\n    "z_suffix": {\n      "kind": "literal",\n      "value": "2026-09-29T01:02:03Z"\n    }\n  },\n  "ledger._parse_timestamp": {\n    "aware_iso": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    },\n    "date_only": {\n      "aware": true,\n      "iso": "2024-03-05T00:00:00+00:00",\n      "kind": "datetime"\n    },\n    "date_prefix_bogus": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "empty": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "epoch": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "garbage": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "lowercase_z": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "naive": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    },\n    "none": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "object": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "offset_midnight": {\n      "aware": true,\n      "iso": "2024-03-05T00:30:00+02:00",\n      "kind": "datetime"\n    },\n    "offset_seconds": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00:30",\n      "kind": "datetime"\n    },\n    "padded_z": {\n      "error": "LedgerError",\n      "kind": "raised"\n    },\n    "space_naive": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    },\n    "space_zoned": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    },\n    "z_suffix": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    }\n  },\n  "ledger._worker_seconds": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 0\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 22028\n    },\n    "date_prefix_bogus": {\n      "kind": "literal",\n      "value": null\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "literal",\n      "value": null\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "lowercase_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 0\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "offset_midnight": {\n      "kind": "number",\n      "value": 27428\n    },\n    "offset_seconds": {\n      "kind": "number",\n      "value": 0\n    },\n    "padded_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_naive": {\n      "kind": "number",\n      "value": 0\n    },\n    "space_zoned": {\n      "kind": "number",\n      "value": 0\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 0\n    }\n  },\n  "mcp_views._parsed_observation": {\n    "aware_iso": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    },\n    "date_only": {\n      "kind": "literal",\n      "value": null\n    },\n    "date_prefix_bogus": {\n      "kind": "literal",\n      "value": null\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "lowercase_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "literal",\n      "value": null\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "offset_midnight": {\n      "aware": true,\n      "iso": "2024-03-04T22:30:00+00:00",\n      "kind": "datetime"\n    },\n    "offset_seconds": {\n      "aware": true,\n      "iso": "2026-09-29T01:01:33+00:00",\n      "kind": "datetime"\n    },\n    "padded_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_naive": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_zoned": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    },\n    "z_suffix": {\n      "aware": true,\n      "iso": "2026-09-29T01:02:03+00:00",\n      "kind": "datetime"\n    }\n  },\n  "mcp_views.compose_review": {\n    "aware_iso": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "date_only": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "date_prefix_bogus": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "empty": {\n      "kind": "mapping",\n      "value": {\n        "current": true,\n        "stale": false\n      }\n    },\n    "epoch": {\n      "kind": "mapping",\n      "value": {\n        "current": true,\n        "stale": false\n      }\n    },\n    "garbage": {\n      "kind": "mapping",\n      "value": {\n        "current": true,\n        "stale": false\n      }\n    },\n    "lowercase_z": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "naive": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "none": {\n      "kind": "mapping",\n      "value": {\n        "current": true,\n        "stale": false\n      }\n    },\n    "object": {\n      "kind": "mapping",\n      "value": {\n        "current": true,\n        "stale": false\n      }\n    },\n    "offset_midnight": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "offset_seconds": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "padded_z": {\n      "kind": "mapping",\n      "value": {\n        "current": true,\n        "stale": false\n      }\n    },\n    "space_naive": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "space_zoned": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    },\n    "z_suffix": {\n      "kind": "mapping",\n      "value": {\n        "current": false,\n        "stale": true\n      }\n    }\n  },\n  "serve._elapsed_since": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 3600\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 81050523\n    },\n    "date_prefix_bogus": {\n      "kind": "literal",\n      "value": null\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "literal",\n      "value": null\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "lowercase_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 3600\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "offset_midnight": {\n      "kind": "number",\n      "value": 81055923\n    },\n    "offset_seconds": {\n      "kind": "number",\n      "value": 3630\n    },\n    "padded_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_naive": {\n      "kind": "number",\n      "value": 3600\n    },\n    "space_zoned": {\n      "kind": "number",\n      "value": 3600\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 3600\n    }\n  },\n  "serve.completion_key": {\n    "aware_iso": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    },\n    "date_only": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    },\n    "date_prefix_bogus": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "empty": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "epoch": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "garbage": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "lowercase_z": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "naive": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    },\n    "none": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "object": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "offset_midnight": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "offset_seconds": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    },\n    "padded_z": {\n      "kind": "list",\n      "value": [\n        "b",\n        "a"\n      ]\n    },\n    "space_naive": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    },\n    "space_zoned": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    },\n    "z_suffix": {\n      "kind": "list",\n      "value": [\n        "a",\n        "b"\n      ]\n    }\n  },\n  "velocity.measure": {\n    "aware_iso": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 93.95690972222222,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2026-09-29T01:02:03+00:00"\n        }\n      }\n    },\n    "date_only": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 1032.0,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2024-03-05"\n        }\n      }\n    },\n    "date_prefix_bogus": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "empty": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "epoch": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "garbage": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "lowercase_z": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "naive": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 93.95690972222222,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2026-09-29T01:02:03"\n        }\n      }\n    },\n    "none": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "object": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "offset_midnight": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 1032.0625,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2024-03-05T00:30:00+02:00"\n        }\n      }\n    },\n    "offset_seconds": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 93.95725694444444,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2026-09-29T01:02:03+00:00:30"\n        }\n      }\n    },\n    "padded_z": {\n      "error": "TypeError",\n      "kind": "raised"\n    },\n    "space_naive": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 93.95690972222222,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2026-09-29 01:02:03"\n        }\n      }\n    },\n    "space_zoned": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 93.95690972222222,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2026-09-29 01:02:03+00:00"\n        }\n      }\n    },\n    "z_suffix": {\n      "kind": "mapping",\n      "value": {\n        "kind": "mapping",\n        "value": {\n          "elapsed_days": 93.95690972222222,\n          "end": "2027-01-01T00:00:00Z",\n          "followup_through": "2026-12-25T00:00:00Z",\n          "start": "2026-09-29T01:02:03Z"\n        }\n      }\n    }\n  },\n  "velocity.stamp": {\n    "aware_iso": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "date_only": {\n      "kind": "number",\n      "value": 1709596800.0\n    },\n    "date_prefix_bogus": {\n      "kind": "literal",\n      "value": null\n    },\n    "empty": {\n      "kind": "literal",\n      "value": null\n    },\n    "epoch": {\n      "kind": "literal",\n      "value": null\n    },\n    "garbage": {\n      "kind": "literal",\n      "value": null\n    },\n    "lowercase_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "naive": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "none": {\n      "kind": "literal",\n      "value": null\n    },\n    "object": {\n      "kind": "literal",\n      "value": null\n    },\n    "offset_midnight": {\n      "kind": "number",\n      "value": 1709591400.0\n    },\n    "offset_seconds": {\n      "kind": "number",\n      "value": 1790643693.0\n    },\n    "padded_z": {\n      "kind": "literal",\n      "value": null\n    },\n    "space_naive": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "space_zoned": {\n      "kind": "number",\n      "value": 1790643723.0\n    },\n    "z_suffix": {\n      "kind": "number",\n      "value": 1790643723.0\n    }\n  }\n}'
)
