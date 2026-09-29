"""Every migrated timestamp caller keeps the output it had before it migrated.

The table in ``EXPECTED`` was recorded by running each caller at base cea6da07
over one shared input list, through the door each caller already exposed, so a
recorded value is the caller's own observable output rather than an
intermediate.

A datetime is recorded as the instant it names and whether it is zone-aware.
The wall offset a value happens to carry is not recorded: ``parse_utc`` fixes
the instant and normalises it to UTC, and a caller may not re-parse to recover
the offset without becoming the second parser these callers are shedding.

Two callers carry behaviour the shared parser does not, preserved here rather
than unified:

* ``metering._event_timestamp`` and the other readers refuse a stamp whose text
  is not strictly spelled (surrounding space, a lowercase zone designator) and
  a value that is not a string at all, exactly as the parser they replaced did.
  ``parse_utc`` is deliberately tolerant of both, so the guard sits around the
  call. ``metering._event_timestamp`` keeps raising on a non-string, as its
  recorded cells show.
* ``ticker.row_moment`` reads a stamp that names no zone as a *local* wall
  clock, which is the epoch a replay measures its windows against on the machine
  doing the replay. ``parse_utc`` reads a zoneless value as UTC. The local-zone
  branch reproduces the recorded epoch, so the module pins the local zone.

``ticker.local_clock`` renders the reader's own wall clock for display and keeps
that rendering exactly; it is migrated but its output is a local-zone clock,
so the pinned zone fixes it too.
"""

from __future__ import annotations

import ast
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import reckon
from reckon import _backends, budget
from reckon._timestamps import parse_utc
from reckon.crew import budget_group, metering, ticker


class _NonStringObject:
    """A non-string object whose repr carries no parsable timestamp."""

    def __repr__(self) -> str:
        return "<probe-object>"


INPUTS: dict[str, object] = {
    "aware_iso": "2026-09-29T03:02:03+02:00",
    "z_suffix": "2026-09-29T01:02:03Z",
    "lowercase_z": "2026-09-29T01:02:03z",
    "space_zoned": "2026-09-29 01:02:03+00:00",
    "space_naive": "2026-09-29 01:02:03",
    "offset_seconds": "2026-09-29T01:02:03+00:00:30",
    "near_midnight": "2024-03-05T00:30:00+02:00",
    "naive": "2026-09-29T01:02:03",
    "date_only": "2024-03-05",
    "trailing_text": "2024-03-05bogus",
    "whitespace": " 2026-09-29T01:02:03+00:00 ",
    "epoch": 1700000000,
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


def _raised(name: str) -> dict[str, Any]:
    return {"kind": "raised", "type": name}


_NONE = _literal(None)

EXPECTED: dict[str, dict[str, Any]] = {
    "_backends._event_timestamp": {
        "aware_iso": _aware("2026-09-29T01:02:03+00:00"),
        "date_only": _aware("2024-03-05T00:00:00+00:00"),
        "empty": _NONE,
        "epoch": _NONE,
        "garbage": _NONE,
        "lowercase_z": _NONE,
        "naive": _aware("2026-09-29T01:02:03+00:00"),
        "near_midnight": _aware("2024-03-04T22:30:00+00:00"),
        "none": _NONE,
        "object": _NONE,
        "offset_seconds": _aware("2026-09-29T01:01:33+00:00"),
        "space_naive": _aware("2026-09-29T01:02:03+00:00"),
        "space_zoned": _aware("2026-09-29T01:02:03+00:00"),
        "trailing_text": _NONE,
        "whitespace": _NONE,
        "z_suffix": _aware("2026-09-29T01:02:03+00:00"),
    },
    "_backends._parse_cached_fetch_stamp": {
        "aware_iso": _aware("2026-09-29T01:02:03+00:00"),
        "date_only": _NONE,
        "empty": _NONE,
        "epoch": _aware("2023-11-14T22:13:20+00:00"),
        "garbage": _NONE,
        "lowercase_z": _NONE,
        "naive": _NONE,
        "near_midnight": _aware("2024-03-04T22:30:00+00:00"),
        "none": _NONE,
        "object": _NONE,
        "offset_seconds": _aware("2026-09-29T01:01:33+00:00"),
        "space_naive": _NONE,
        "space_zoned": _aware("2026-09-29T01:02:03+00:00"),
        "trailing_text": _NONE,
        "whitespace": _NONE,
        "z_suffix": _aware("2026-09-29T01:02:03+00:00"),
    },
    "budget._parse_stamp": {
        "aware_iso": _aware("2026-09-29T01:02:03+00:00"),
        "date_only": _aware("2024-03-05T00:00:00+00:00"),
        "empty": _NONE,
        "epoch": _NONE,
        "garbage": _NONE,
        "lowercase_z": _NONE,
        "naive": _aware("2026-09-29T01:02:03+00:00"),
        "near_midnight": _aware("2024-03-04T22:30:00+00:00"),
        "none": _NONE,
        "object": _NONE,
        "offset_seconds": _aware("2026-09-29T01:01:33+00:00"),
        "space_naive": _aware("2026-09-29T01:02:03+00:00"),
        "space_zoned": _aware("2026-09-29T01:02:03+00:00"),
        "trailing_text": _NONE,
        "whitespace": _NONE,
        "z_suffix": _aware("2026-09-29T01:02:03+00:00"),
    },
    "budget_group._observed_moment": {
        "aware_iso": _aware("2026-09-29T01:02:03+00:00"),
        "date_only": _aware("2024-03-05T00:00:00+00:00"),
        "empty": _NONE,
        "epoch": _aware("2023-11-14T22:13:20+00:00"),
        "garbage": _NONE,
        "lowercase_z": _NONE,
        "naive": _aware("2026-09-29T01:02:03+00:00"),
        "near_midnight": _aware("2024-03-04T22:30:00+00:00"),
        "none": _NONE,
        "object": _NONE,
        "offset_seconds": _aware("2026-09-29T01:01:33+00:00"),
        "space_naive": _aware("2026-09-29T01:02:03+00:00"),
        "space_zoned": _aware("2026-09-29T01:02:03+00:00"),
        "trailing_text": _NONE,
        "whitespace": _NONE,
        "z_suffix": _aware("2026-09-29T01:02:03+00:00"),
    },
    "metering._event_timestamp": {
        "aware_iso": _aware("2026-09-29T01:02:03+00:00"),
        "date_only": _aware("2024-03-05T00:00:00+00:00"),
        "empty": _NONE,
        "epoch": _raised("TypeError"),
        "garbage": _NONE,
        "lowercase_z": _NONE,
        "naive": _aware("2026-09-29T01:02:03+00:00"),
        "near_midnight": _aware("2024-03-04T22:30:00+00:00"),
        "none": _raised("TypeError"),
        "object": _raised("TypeError"),
        "offset_seconds": _aware("2026-09-29T01:01:33+00:00"),
        "space_naive": _aware("2026-09-29T01:02:03+00:00"),
        "space_zoned": _aware("2026-09-29T01:02:03+00:00"),
        "trailing_text": _NONE,
        "whitespace": _NONE,
        "z_suffix": _aware("2026-09-29T01:02:03+00:00"),
    },
    "metering._stamped_elapsed": {
        "aware_iso": _literal(9356277.0),
        "date_only": _literal(90403200.0),
        "empty": _NONE,
        "epoch": _NONE,
        "garbage": _NONE,
        "lowercase_z": _NONE,
        "naive": _literal(9356277.0),
        "near_midnight": _literal(90408600.0),
        "none": _NONE,
        "object": _NONE,
        "offset_seconds": _literal(9356307.0),
        "space_naive": _literal(9356277.0),
        "space_zoned": _literal(9356277.0),
        "trailing_text": _NONE,
        "whitespace": _NONE,
        "z_suffix": _literal(9356277.0),
    },
    "reckon._timestamps.parse_utc": {
        "aware_iso": _aware("2026-09-29T01:02:03+00:00"),
        "date_only": _aware("2024-03-05T00:00:00+00:00"),
        "empty": _NONE,
        "epoch": _aware("2023-11-14T22:13:20+00:00"),
        "garbage": _NONE,
        "lowercase_z": _aware("2026-09-29T01:02:03+00:00"),
        "naive": _aware("2026-09-29T01:02:03+00:00"),
        "near_midnight": _aware("2024-03-04T22:30:00+00:00"),
        "none": _NONE,
        "object": _NONE,
        "offset_seconds": _aware("2026-09-29T01:01:33+00:00"),
        "space_naive": _aware("2026-09-29T01:02:03+00:00"),
        "space_zoned": _aware("2026-09-29T01:02:03+00:00"),
        "trailing_text": _NONE,
        "whitespace": _aware("2026-09-29T01:02:03+00:00"),
        "z_suffix": _aware("2026-09-29T01:02:03+00:00"),
    },
    "ticker.local_clock": {
        "aware_iso": _literal("03:02:03"),
        "date_only": _literal("--:--:--"),
        "empty": _literal("--:--:--"),
        "epoch": _literal("--:--:--"),
        "garbage": _literal("--:--:--"),
        "lowercase_z": _literal("01:02:03"),
        "naive": _literal("03:02:03"),
        "near_midnight": _literal("23:30:00"),
        "none": _literal("--:--:--"),
        "object": _literal("--:--:--"),
        "offset_seconds": _literal("03:01:33"),
        "space_naive": _literal("03:02:03"),
        "space_zoned": _literal("03:02:03"),
        "trailing_text": _literal("--:--:--"),
        "whitespace": _literal("T01:02:0"),
        "z_suffix": _literal("03:02:03"),
    },
    "ticker.row_moment": {
        "aware_iso": _literal(1790643723.0),
        "date_only": _literal(1709593200.0),
        "empty": _literal(0.0),
        "epoch": _literal(0.0),
        "garbage": _literal(0.0),
        "lowercase_z": _literal(0.0),
        "naive": _literal(1790636523.0),
        "near_midnight": _literal(1709591400.0),
        "none": _literal(0.0),
        "object": _literal(0.0),
        "offset_seconds": _literal(1790643693.0),
        "space_naive": _literal(1790636523.0),
        "space_zoned": _literal(1790643723.0),
        "trailing_text": _literal(0.0),
        "whitespace": _literal(0.0),
        "z_suffix": _literal(1790643723.0),
    },
}


def _cached(value: object) -> object:
    return _backends._parse_cached_fetch_stamp({"fetch_stamp": value})


def _event(value: object) -> object:
    return _backends._event_timestamp({"timestamp": value})


def _stamped(value: object) -> object:
    return metering._stamped_elapsed({"created_at": value}, now_seconds=1800000000.0)


def _row(value: object) -> object:
    return ticker.row_moment({"observed_at": value})


CALLERS: dict[str, Any] = {
    "_backends._event_timestamp": _event,
    "_backends._parse_cached_fetch_stamp": _cached,
    "budget._parse_stamp": budget._parse_stamp,
    "budget_group._observed_moment": budget_group._observed_moment,
    "metering._event_timestamp": metering._event_timestamp,
    "metering._stamped_elapsed": _stamped,
    "reckon._timestamps.parse_utc": parse_utc,
    "ticker.local_clock": ticker.local_clock,
    "ticker.row_moment": _row,
}


def canon(value: object) -> dict[str, Any]:
    """Reduce a caller's output to the recorded shape."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return {"kind": "naive_datetime", "iso": value.isoformat()}
        return {"kind": "aware_datetime", "utc": value.astimezone(UTC).isoformat()}
    return {"kind": "literal", "value": value}


def observe(fn: Any, value: object) -> dict[str, Any]:
    """Run one caller over one input and reduce its output or raised type."""
    try:
        produced = fn(value)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "raised", "type": type(exc).__name__}
    return canon(produced)


@pytest.fixture(autouse=True)
def _pinned_local_zone() -> Any:
    """Fix the local zone the display and zoneless-epoch callers read.

    ``ticker.local_clock`` renders the reader's own wall clock and
    ``ticker.row_moment`` reads a zoneless stamp as a local wall clock, so
    their recorded output is only meaningful under a known zone.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


CASES = [(caller, key) for caller in CALLERS for key in INPUTS]


@pytest.mark.parametrize(("caller", "key"), CASES)
def test_migrated_caller_keeps_its_recorded_output(caller: str, key: str) -> None:
    observed = observe(CALLERS[caller], INPUTS[key])
    assert observed == EXPECTED[caller][key]


def test_every_caller_is_covered() -> None:
    assert set(CALLERS) == set(EXPECTED)
    for caller, row in EXPECTED.items():
        assert set(row) == set(INPUTS), caller


_TARGETS: dict[str, tuple[str, ...]] = {
    "_backends.py": ("_parse_cached_fetch_stamp", "_event_timestamp"),
    "budget.py": ("_parse_stamp",),
    "crew/budget_group.py": ("_observed_moment",),
    "crew/metering.py": ("_event_timestamp", "_stamped_elapsed"),
    "crew/ticker.py": ("row_moment", "local_clock"),
}


def _module_tree(relative: str) -> ast.Module:
    root = Path(reckon.__file__).parent
    source = (root / relative).read_text()
    return ast.parse(source, filename=relative)


def test_no_touched_module_parses_iso_strings_itself() -> None:
    """No ``fromisoformat`` call remains in a touched module.

    Every one of the eight callers migrates to the shared parser, so the
    touched modules carry no retained exception and no direct parse of their
    own.
    """
    offenders = [
        f"{relative}:{node.lineno}"
        for relative in _TARGETS
        for node in ast.walk(_module_tree(relative))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fromisoformat"
    ]
    assert offenders == []


def test_each_caller_reads_through_the_shared_parser() -> None:
    """Each named caller's own body calls ``parse_utc``.

    This proves the migration landed rather than that the old parse was simply
    deleted: the shared parser's name appears inside each caller's body.
    """
    missing = []
    for relative, names in _TARGETS.items():
        tree = _module_tree(relative)
        for name in names:
            node = next(
                (
                    item
                    for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == name
                ),
                None,
            )
            assert node is not None, f"{relative}:{name} not found"
            calls_parse_utc = any(
                isinstance(item, ast.Name) and item.id == "parse_utc"
                for item in ast.walk(node)
            )
            if not calls_parse_utc:
                missing.append(f"{relative}:{name}")
    assert missing == []
