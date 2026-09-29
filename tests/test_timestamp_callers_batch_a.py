"""Every migrated timestamp caller keeps the output it had before it migrated.

The table in ``EXPECTED`` was recorded by running each caller at base 142cdf05
over one shared input list. Each caller is exercised through the door it
already exposed, so the recorded value is the caller's own observable output
rather than an intermediate.

A datetime is recorded as the instant it names and whether it is zone-aware.
The wall offset a value happens to carry is not recorded: ``parse_utc`` fixes
the instant and normalises it to UTC, and a caller may not re-parse to recover
the offset without becoming the second parser these callers are shedding. Two
callers are read as UTC instants; the rest compare equal as instants either way.

One cell is a deliberate delta rather than a preserved reading.
``lane_evidence._moment`` raised ``AttributeError`` for a naive stamp at base —
it referenced ``datetime.UTC`` on the class, which does not exist — so its
recorded output there is the corrected UTC instant the migration produces, and
the base crash is quoted above the row.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import reckon
from reckon.crew import (
    hold,
    lane_document,
    lane_evidence,
    obligations,
    pace_replay,
    paid_lanes,
    query,
    resumption,
    rollout,
    routing,
    runs,
    summary,
    window_reading,
)

FIXED_NOW = datetime(2026, 9, 29, 2, 0, 0, tzinfo=UTC)
FIXED_NOW_SECONDS = FIXED_NOW.timestamp()

MODULES = (
    "hold",
    "lane_document",
    "lane_evidence",
    "obligations",
    "pace_replay",
    "paid_lanes",
    "query",
    "resumption",
    "rollout",
    "routing",
    "runs",
    "summary",
    "window_reading",
)


class _NonStringObject:
    """A non-string object whose repr carries no parsable timestamp."""

    def __repr__(self) -> str:
        return "<probe-object>"


INPUTS: dict[str, object] = {
    "aware_iso": "2026-09-29T03:02:03+02:00",
    "z_suffix": "2026-09-29T01:02:03Z",
    "naive": "2026-09-29T01:02:03",
    "date_only": "2026-09-29",
    "epoch": 1759107723,
    "empty": "",
    "none": None,
    "object": _NonStringObject(),
    "garbage": "not a timestamp",
}


def _aware(utc: str) -> dict[str, Any]:
    return {"kind": "aware_datetime", "utc": utc}


def _naive(iso: str) -> dict[str, Any]:
    return {"kind": "naive_datetime", "iso": iso}


def _literal(value: Any) -> dict[str, Any]:
    return {"kind": "literal", "value": value}


def _unmeasured(reason: str) -> dict[str, Any]:
    return {"kind": "unmeasured", "reason": reason}


def _tuple(a: Any, b: Any) -> dict[str, Any]:
    return {"kind": "tuple", "value": [a, b]}


_A = "2026-09-29T01:02:03+00:00"
_D = "2026-09-29T00:00:00+00:00"
_NONE = _literal(None)

EXPECTED: dict[str, dict[str, Any]] = {
    "hold._as_moment": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "lane_document._parse_stamp": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    # At base, "naive" and "date_only" raised
    # AttributeError: type object 'datetime.datetime' has no attribute 'UTC'.
    # The migration to parse_utc repairs that; the repaired instant is recorded.
    "lane_evidence._moment": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "obligations._seconds_since": {
        "aware_iso": _literal(3477),
        "z_suffix": _literal(3477),
        "naive": _literal(3477),
        "date_only": _literal(7200),
        "epoch": _literal(0),
        "empty": _literal(0),
        "none": _literal(0),
        "object": _literal(0),
        "garbage": _literal(0),
    },
    "pace_replay._read": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _unmeasured("v is not an instant: 1759107723"),
        "empty": _unmeasured("missing"),
        "none": _unmeasured("missing"),
        "object": _unmeasured("v is not an instant: <probe-object>"),
        "garbage": _unmeasured("v is not a valid instant: 'not a timestamp'"),
    },
    "paid_lanes._parse_stamp": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "query._normalize_stamp": {
        "aware_iso": _tuple(_A, "utc"),
        "z_suffix": _tuple(_A, "utc"),
        "naive": _tuple(None, "unknown"),
        "date_only": _tuple(None, "unknown"),
        "epoch": _tuple(None, "unknown"),
        "empty": _tuple(None, "unknown"),
        "none": _tuple(None, "unknown"),
        "object": _tuple(None, "unknown"),
        "garbage": _tuple(None, "unknown"),
    },
    "resumption._parse_stamp": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "rollout._parse_timestamp": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _naive("2026-09-29T01:02:03"),
        "date_only": _naive("2026-09-29T00:00:00"),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "routing._parse_utc_timestamp": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "runs._stream_quiet_seconds": {
        "aware_iso": _literal(3477),
        "z_suffix": _literal(3477),
        "naive": _literal(3477),
        "date_only": _literal(7200),
        "epoch": _literal(0),
        "empty": _literal(0),
        "none": _literal(0),
        "object": _literal(0),
        "garbage": _literal(0),
    },
    "summary._row_moment": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
    "window_reading._parse_stamp": {
        "aware_iso": _aware(_A),
        "z_suffix": _aware(_A),
        "naive": _aware(_A),
        "date_only": _aware(_D),
        "epoch": _NONE,
        "empty": _NONE,
        "none": _NONE,
        "object": _NONE,
        "garbage": _NONE,
    },
}


def _seconds_since(value: object) -> int:
    return obligations._seconds_since(value, now=FIXED_NOW)


def _pace_instant(value: object) -> object:
    return pace_replay._read(
        {"v": value}, ("v",), "instant", label="v", missing="missing"
    )


def _quiet_seconds(value: object) -> int:
    return runs._stream_quiet_seconds(
        {"created_at": value}, now_seconds=FIXED_NOW_SECONDS
    )


def _row_moment(value: object) -> datetime | None:
    return summary._row_moment({"k": value}, "k")


CALLERS: dict[str, Any] = {
    "hold._as_moment": hold._as_moment,
    "lane_document._parse_stamp": lane_document._parse_stamp,
    "lane_evidence._moment": lane_evidence._moment,
    "obligations._seconds_since": _seconds_since,
    "pace_replay._read": _pace_instant,
    "paid_lanes._parse_stamp": paid_lanes._parse_stamp,
    "query._normalize_stamp": query._normalize_stamp,
    "resumption._parse_stamp": resumption._parse_stamp,
    "rollout._parse_timestamp": rollout._parse_timestamp,
    "routing._parse_utc_timestamp": routing._parse_utc_timestamp,
    "runs._stream_quiet_seconds": _quiet_seconds,
    "summary._row_moment": _row_moment,
    "window_reading._parse_stamp": window_reading._parse_stamp,
}


def canon(value: object) -> dict[str, Any]:
    """Reduce a caller's output to the recorded shape."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return {"kind": "naive_datetime", "iso": value.isoformat()}
        return {"kind": "aware_datetime", "utc": value.astimezone(UTC).isoformat()}
    if isinstance(value, pace_replay._UnmeasuredAllowance):
        return {"kind": "unmeasured", "reason": value.reason}
    if isinstance(value, tuple):
        return {"kind": "tuple", "value": list(value)}
    return {"kind": "literal", "value": value}


CASES = [
    (caller, key)
    for caller in CALLERS
    for key in INPUTS
]


@pytest.mark.parametrize(("caller", "key"), CASES)
def test_migrated_caller_keeps_its_recorded_output(caller: str, key: str) -> None:
    observed = canon(CALLERS[caller](INPUTS[key]))
    assert observed == EXPECTED[caller][key]


def test_every_caller_is_covered() -> None:
    assert set(CALLERS) == set(EXPECTED)
    for caller, row in EXPECTED.items():
        assert set(row) == set(INPUTS), caller


def test_no_caller_parses_iso_strings_itself() -> None:
    """The batch parses through reckon._timestamps, not fromisoformat directly."""
    crew = Path(reckon.__file__).parent / "crew"
    offenders = [
        f"{module}.py:{node.lineno}"
        for module in MODULES
        for node in ast.walk(
            ast.parse((crew / f"{module}.py").read_text(), filename=f"{module}.py")
        )
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fromisoformat"
    ]
    assert offenders == []
