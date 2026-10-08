"""Three promotion timestamp readers keep their recorded output on the shared parser.

The table in ``EXPECTED`` was recorded by running each reader at the base revision
before any edit, over one shared input list: a ``Z``-suffixed stamp, an offset
stamp, a naive stamp, an empty string, a malformed string and a numeric value. Each
reader is exercised through the door it already exposed. The migration moves the
parsing onto ``reckon._timestamps`` and leaves the policy each reader carried:

* ``_elapsed_seconds`` keeps its elapsed-time policy — a leading reference pair and
  a clamp at zero seconds — on top of ``parse_utc``.
* ``_assume_utc_if_naive`` keeps its display policy — a stamp that names no zone is
  rewritten with a trailing ``Z``, anything else is returned as written — on top of
  ``parse_iso``.
* the stream-event loop in ``_terminal_stream_data`` keeps only zone-aware stamps,
  now decided by ``_zone_aware_stream_timestamp`` reading ``parse_iso`` so the span
  is measured from moments that stated where they were.

One cell is a deliberate delta and is not a preserved reading.
``_assume_utc_if_naive`` at base called ``value.replace`` on its argument, so a
numeric value raised ``AttributeError: 'int' object has no attribute 'replace'``.
The migration routes through ``parse_iso``, whose non-string miss returns ``None``,
so the reader now returns the value unchanged — the same policy it applies to a
string it cannot parse. The recorded cell below is that corrected output, and no
caller reaches it: the one caller guards the stamp before the call.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import (
    promotion,
    promotion_checks,
    promotion_evidence,
    promotion_gate,
    promotion_records,
    promotion_release,
    promotion_scope,
)

REFERENCE = "2026-09-26T12:00:00Z"

INPUTS: dict[str, object] = {
    "z": "2026-09-26T11:00:00Z",
    "offset": "2026-09-26T13:00:00+02:00",
    "naive": "2026-09-26T11:00:00",
    "empty": "",
    "malformed": "not-a-timestamp",
    "numeric": 1234567890,
}


def canon(value: object) -> dict[str, Any]:
    """Reduce a reader's output to the shape the recorded table uses."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return {"kind": "naive", "iso": value.isoformat()}
        return {"kind": "aware", "utc": value.astimezone(UTC).isoformat()}
    return {"kind": "literal", "value": value}


def _elapsed(value: object) -> object:
    return promotion._elapsed_seconds(value, REFERENCE)


READERS = {
    "elapsed_seconds": _elapsed,
    "assume_utc_if_naive": promotion._assume_utc_if_naive,
    "zone_aware_stream_timestamp": promotion._zone_aware_stream_timestamp,
}


def _lit(value: Any) -> dict[str, Any]:
    return {"kind": "literal", "value": value}


def _aware(iso: str) -> dict[str, Any]:
    return {"kind": "aware", "utc": iso}


EXPECTED: dict[str, dict[str, Any]] = {
    "elapsed_seconds": {
        "z": _lit(3600),
        "offset": _lit(3600),
        "naive": _lit(3600),
        "empty": _lit(None),
        "malformed": _lit(None),
        "numeric": _lit(None),
    },
    "assume_utc_if_naive": {
        "z": _lit("2026-09-26T11:00:00Z"),
        "offset": _lit("2026-09-26T13:00:00+02:00"),
        "naive": _lit("2026-09-26T11:00:00Z"),
        "empty": _lit(""),
        "malformed": _lit("not-a-timestamp"),
        # Deliberate delta: base raised AttributeError for a numeric value; the
        # shared parser's non-string miss returns the value as written.
        "numeric": _lit(1234567890),
    },
    "zone_aware_stream_timestamp": {
        "z": _aware("2026-09-26T11:00:00+00:00"),
        "offset": _aware("2026-09-26T11:00:00+00:00"),
        "naive": _lit(None),
        "empty": _lit(None),
        "malformed": _lit(None),
        "numeric": _lit(None),
    },
}


CASES = [(reader, key) for reader in READERS for key in INPUTS]


@pytest.mark.parametrize(("reader", "key"), CASES)
def test_reader_keeps_its_recorded_output(reader: str, key: str) -> None:
    assert canon(READERS[reader](INPUTS[key])) == EXPECTED[reader][key]


def test_every_reader_is_covered() -> None:
    assert set(READERS) == set(EXPECTED)
    for reader, row in EXPECTED.items():
        assert set(row) == set(INPUTS), reader


def test_no_reader_parses_iso_strings_itself() -> None:
    """Promotion parses through reckon._timestamps, not fromisoformat directly."""
    modules = (
        promotion,
        promotion_checks,
        promotion_evidence,
        promotion_gate,
        promotion_records,
        promotion_release,
        promotion_scope,
    )
    offenders = [
        (module.__file__, node.lineno)
        for module in modules
        for node in ast.walk(ast.parse(Path(module.__file__).read_text(), filename=str(module.__file__)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fromisoformat"
    ]
    assert offenders == []
