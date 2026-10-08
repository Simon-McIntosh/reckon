"""The four dispatch timestamp readers reproduce their recorded behaviour.

Each of the four timestamp readers in ``reckon/crew/dispatch.py`` resolves
through ``reckon._timestamps``: the budget-hold evidence stamp, the parse of the
repository clock's own output, ``_lane_advisory_instant``, and the lane
document's ``observed_at``. The table below records what the BASE revision's
functions produced for each reader over one shared six-input list -- a Z stamp,
an offset stamp, a naive stamp, an empty string, a malformed string and a
non-string value -- and asserts each reader still produces exactly that at head.

The base revision was read from a scratch tree (``git archive HEAD``). Three of
the four readers reproduce every cell; the four cells that move belong to the
repository-clock reader and are named in CHANGED with the base output and the
reason the unified rule replaces it.

The lane reading keeps its own ``datetime.fromisoformat`` call: its refusal
quotes the parser's own exception text, which the shared parser swallows to
return ``None``. The retained exception is named below, with the input on which
the shared parser would differ -- a stamp padded with surrounding whitespace,
which the shared parser strips and accepts and the retained reader rejects.
"""

from __future__ import annotations

import ast
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _timestamps
from reckon.crew import dispatch_admission as dispatch_admission_module

dispatch = importlib.import_module("reckon.crew.dispatch")

FIXED_NOW = "2026-09-29T00:00:00Z"
FIXED_OBSERVED = "2026-09-29T00:00:00Z"
CONFIG = {"budget": {"evidence_shelf_life_minutes": 30}}
READING_NOW = datetime(2026, 9, 29, 0, 1, tzinfo=UTC)


class _Probe:
    """A non-string object whose repr carries no parsable timestamp."""

    def __repr__(self) -> str:
        return "<probe-object>"


INPUTS: dict[str, object] = {
    "z_suffix": "2026-09-29T01:02:03Z",
    "offset": "2026-09-29T01:02:58+02:00",
    "naive": "2026-09-29T01:02:03",
    "empty": "",
    "malformed": "not a timestamp",
    "non_string": 1709620028,
}


def canon(value: object) -> dict[str, object]:
    if isinstance(value, datetime):
        return {
            "kind": "datetime",
            "iso": value.isoformat(),
            "aware": value.tzinfo is not None,
        }
    if isinstance(value, dict):
        return {"kind": "dict", "json": {k: canon(v) for k, v in sorted(value.items())}}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, (int, float)):
        return {"kind": "number", "value": value}
    if value is None:
        return {"kind": "literal", "value": None}
    if isinstance(value, str):
        return {"kind": "literal", "value": value}
    return {"kind": "literal", "value": repr(value)}


def _budget_evidence_observed_at(value: object) -> object:
    original = dispatch_admission_module._utc_now
    dispatch_admission_module._utc_now = lambda: FIXED_NOW
    try:
        return dispatch._actionable_budget_hold(
            {"backend": "b", "reason": "held", "state": {"observed_at": value}},
            config=CONFIG,
        ).verdict["reason"]
    finally:
        dispatch_admission_module._utc_now = original


def _budget_evidence_now(value: object) -> object:
    original = dispatch_admission_module._utc_now
    dispatch_admission_module._utc_now = lambda: value
    try:
        return dispatch._actionable_budget_hold(
            {"backend": "b", "reason": "held", "state": {"observed_at": FIXED_OBSERVED}},
            config=CONFIG,
        ).verdict["reason"]
    finally:
        dispatch_admission_module._utc_now = original


def _lane_advisory_instant(value: object) -> object:
    return dispatch._lane_advisory_instant(value)


def _lane_reading(value: object) -> object:
    document = {
        "observed_at": value,
        "headroom": 1,
        "mean_context": 2,
        "suggested_shelf_life_seconds": 3600,
    }
    reading = dispatch._lane_reading_carry(document, now=READING_NOW)
    return {key: reading[key] for key in ("state", "observed_at", "age_seconds", "detail")}


CALLERS = {
    "budget_evidence_observed_at": _budget_evidence_observed_at,
    "budget_evidence_now": _budget_evidence_now,
    "lane_advisory_instant": _lane_advisory_instant,
    "lane_reading_observed_at": _lane_reading,
}


def observe(name: str, value: object) -> dict[str, object]:
    try:
        return canon(CALLERS[name](value))
    except Exception as exc:  # noqa: BLE001 - a raised exception is the output
        return {"kind": "raised", "error": f"{type(exc).__name__}: {exc}"}


def expected_for(name: str, key: str) -> dict[str, object]:
    changed = CHANGED.get(name, {}).get(key)
    if changed is not None:
        return changed["expected"]
    return EXPECTED[name][key]


@pytest.mark.parametrize(
    ("name", "key"), [(name, key) for name in CALLERS for key in INPUTS]
)
def test_reader_reproduces_recorded_output(name: str, key: str) -> None:
    assert observe(name, INPUTS[key]) == expected_for(name, key)


def test_changed_cells_are_named_with_base_and_reason() -> None:
    for name, cells in CHANGED.items():
        for key, entry in cells.items():
            assert entry["base"] == EXPECTED[name][key], (name, key)
            assert entry["expected"] != entry["base"], (name, key)
            assert entry["reason"], (name, key)


def test_recorded_table_covers_every_reader_and_input() -> None:
    assert set(EXPECTED) == set(CALLERS)
    for name, row in EXPECTED.items():
        assert set(row) == set(INPUTS), name


def _fromisoformat_enclosing_functions() -> list[str]:
    tree = ast.parse(Path(dispatch_admission_module.__file__).read_text())
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "fromisoformat"
                ):
                    found.append(node.name)
    return found


def test_only_the_lane_reading_retains_fromisoformat() -> None:
    assert _fromisoformat_enclosing_functions() == ["_lane_reading_carry"]


def test_migrated_dispatch_binds_both_shared_parsers() -> None:
    tree = ast.parse(Path(dispatch_admission_module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _timestamps.__name__:
            imported.update(alias.name for alias in node.names)
    assert {"parse_utc", "parse_iso"} <= imported


PADDED_INPUT = " 2026-09-29T01:02:03Z "


def test_shared_parser_differs_on_the_retained_input() -> None:
    assert _timestamps.parse_iso(PADDED_INPUT) is not None
    reading = _lane_reading(PADDED_INPUT)
    assert reading["state"] == "unknown"
    assert "Invalid isoformat string" in reading["detail"]

EXPECTED: dict[str, dict[str, dict[str, object]]] = json.loads('{"budget_evidence_now": {"empty": {"error": "ValueError: Invalid isoformat string: \'\'", "kind": "raised"}, "malformed": {"error": "ValueError: Invalid isoformat string: \'not a timestamp\'", "kind": "raised"}, "naive": {"error": "TypeError: can\'t subtract offset-naive and offset-aware datetimes", "kind": "raised"}, "non_string": {"error": "TypeError: fromisoformat: argument must be str", "kind": "raised"}, "offset": {"kind": "literal", "value": "held; the evidence is 0.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-29T00:30:00Z; a served turn on backend \'b\' refreshes this evidence"}, "z_suffix": {"kind": "literal", "value": "held; the evidence is 62.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-29T00:30:00Z; a served turn on backend \'b\' refreshes this evidence"}}, "budget_evidence_observed_at": {"empty": {"kind": "literal", "value": "held; the evidence age is unknown against the 30 minute shelf-life bound because its refusal carries no readable time; a served turn on backend \'b\' refreshes this evidence"}, "malformed": {"kind": "literal", "value": "held; the evidence age is unknown against the 30 minute shelf-life bound because its refusal carries no readable time; a served turn on backend \'b\' refreshes this evidence"}, "naive": {"kind": "literal", "value": "held; the evidence is 0.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-29T01:32:03Z; a served turn on backend \'b\' refreshes this evidence"}, "non_string": {"kind": "literal", "value": "held; the evidence age is unknown against the 30 minute shelf-life bound because its refusal carries no readable time; a served turn on backend \'b\' refreshes this evidence"}, "offset": {"kind": "literal", "value": "held; the evidence is 57.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-28T23:32:58Z; a served turn on backend \'b\' refreshes this evidence"}, "z_suffix": {"kind": "literal", "value": "held; the evidence is 0.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-29T01:32:03Z; a served turn on backend \'b\' refreshes this evidence"}}, "lane_advisory_instant": {"empty": {"kind": "literal", "value": null}, "malformed": {"kind": "literal", "value": null}, "naive": {"aware": true, "iso": "2026-09-29T01:02:03+00:00", "kind": "datetime"}, "non_string": {"kind": "literal", "value": null}, "offset": {"aware": true, "iso": "2026-09-29T01:02:58+02:00", "kind": "datetime"}, "z_suffix": {"aware": true, "iso": "2026-09-29T01:02:03+00:00", "kind": "datetime"}}, "lane_reading_observed_at": {"empty": {"json": {"age_seconds": {"kind": "literal", "value": null}, "detail": {"kind": "literal", "value": "\'observed_at\' \'\' is not an ISO-8601 timestamp: Invalid isoformat string: \'\'"}, "observed_at": {"kind": "literal", "value": null}, "state": {"kind": "literal", "value": "unknown"}}, "kind": "dict"}, "malformed": {"json": {"age_seconds": {"kind": "literal", "value": null}, "detail": {"kind": "literal", "value": "\'observed_at\' \'not a timestamp\' is not an ISO-8601 timestamp: Invalid isoformat string: \'not a timestamp\'"}, "observed_at": {"kind": "literal", "value": null}, "state": {"kind": "literal", "value": "unknown"}}, "kind": "dict"}, "naive": {"json": {"age_seconds": {"kind": "literal", "value": null}, "detail": {"kind": "literal", "value": "\'observed_at\' \'2026-09-29T01:02:03\' lies in the future"}, "observed_at": {"kind": "literal", "value": null}, "state": {"kind": "literal", "value": "unknown"}}, "kind": "dict"}, "non_string": {"json": {"age_seconds": {"kind": "literal", "value": null}, "detail": {"kind": "literal", "value": "lane document carries no parseable \'observed_at\' timestamp"}, "observed_at": {"kind": "literal", "value": null}, "state": {"kind": "literal", "value": "unknown"}}, "kind": "dict"}, "offset": {"json": {"age_seconds": {"kind": "number", "value": 3482}, "detail": {"kind": "literal", "value": ""}, "observed_at": {"kind": "literal", "value": "2026-09-29T01:02:58+02:00"}, "state": {"kind": "literal", "value": "fresh"}}, "kind": "dict"}, "z_suffix": {"json": {"age_seconds": {"kind": "literal", "value": null}, "detail": {"kind": "literal", "value": "\'observed_at\' \'2026-09-29T01:02:03Z\' lies in the future"}, "observed_at": {"kind": "literal", "value": null}, "state": {"kind": "literal", "value": "unknown"}}, "kind": "dict"}}}')

CHANGED: dict[str, dict[str, dict[str, object]]] = json.loads('{"budget_evidence_now": {"empty": {"base": {"error": "ValueError: Invalid isoformat string: \'\'", "kind": "raised"}, "expected": {"error": "AssertionError: the repository clock is not ISO-8601", "kind": "raised"}, "reason": "the base revision handed an empty clock reading to the C parser\'s ValueError; the shared parser returns None for a malformed clock and the reader asserts the repository clock\'s ISO-8601 invariant"}, "malformed": {"base": {"error": "ValueError: Invalid isoformat string: \'not a timestamp\'", "kind": "raised"}, "expected": {"error": "AssertionError: the repository clock is not ISO-8601", "kind": "raised"}, "reason": "the base revision raised ValueError on a malformed clock reading; the shared parser returns None and the reader asserts the clock invariant rather than letting a crash reach the caller"}, "naive": {"base": {"error": "TypeError: can\'t subtract offset-naive and offset-aware datetimes", "kind": "raised"}, "expected": {"kind": "literal", "value": "held; the evidence is 62.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-29T00:30:00Z; a served turn on backend \'b\' refreshes this evidence"}, "reason": "the base revision subtracted an aware stamp from a naive clock reading and raised TypeError; parse_utc reads a naive value as UTC, the rule the repository\'s writers already rely on"}, "non_string": {"base": {"error": "TypeError: fromisoformat: argument must be str", "kind": "raised"}, "expected": {"kind": "literal", "value": "held; the evidence is 0.0 minutes old against the 30 minute shelf-life bound, and ageing lifts this hold at 2026-09-29T00:30:00Z; a served turn on backend \'b\' refreshes this evidence"}, "reason": "the base revision raised TypeError on a non-string clock reading; parse_utc reads a numeric epoch, so an epoch-valued clock resolves instead of crashing"}}}')
