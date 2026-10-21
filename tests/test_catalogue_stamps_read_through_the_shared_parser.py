"""``_created`` reads every ISO stamp through the shared parser.

``reckon.crew.model_catalogue._created`` resolves a model entry's ``created``
value into a POSIX instant. Its numeric branch is unchanged; every *string* it
accepts is now parsed by ``reckon._timestamps.parse_utc``. The table below
asserts that for every input ``_created(s)`` equals
``parse_utc(s).timestamp()`` -- or ``None`` where ``parse_utc(s)`` is ``None``.

Three rows distinguish the shared parser from a direct ``fromisoformat`` call.
A stamp carrying no zone is read as UTC by the shared parser, so its result no
longer depends on the host's ``TZ``; the table exercises it under both ``UTC``
and ``Europe/Paris``. A stamp padded with surrounding whitespace and one with a
lowercase ``z`` suffix both returned ``None`` before this change, because
``datetime.fromisoformat`` rejected each while the shared parser reads both. A
zoned stamp, an empty string and a malformed string pin the rows whose result
does not move.
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from reckon._timestamps import parse_utc

catalogue = importlib.import_module("reckon.crew.model_catalogue")

ZONED = "2026-09-29T01:02:03Z"
ZONELESS = "2026-09-29T01:02:03"
PADDED = "  2026-09-29T01:02:03Z  "
LOWERCASE_Z = "2026-09-29T01:02:03z"
EMPTY = ""
MALFORMED = "not a timestamp"

# Each row is (label, input, host zone). ``None`` leaves the process zone as the
# runner set it; a name pins it while the row is asserted, because only a
# zoneless stamp's result can depend on the zone.
ROWS = (
    ("zoned", ZONED, None),
    ("zoneless_utc", ZONELESS, "UTC"),
    ("zoneless_paris", ZONELESS, "Europe/Paris"),
    ("padded", PADDED, None),
    ("lowercase_z", LOWERCASE_Z, None),
    ("empty", EMPTY, None),
    ("malformed", MALFORMED, None),
)


@contextmanager
def _host_zone(name: str | None) -> Iterator[None]:
    """Run a block with the process time zone pinned to ``name``, then restore.

    ``None`` leaves the zone untouched. ``time.tzset()`` makes the assignment
    visible to ``datetime``, whose naive-to-instant conversion reads the C
    library's local zone.
    """

    if name is None:
        yield
        return
    original = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def _expected(value: str) -> float | None:
    parsed = parse_utc(value)
    return None if parsed is None else parsed.timestamp()


@pytest.mark.parametrize(
    ("value", "zone"),
    [pytest.param(value, zone, id=label) for label, value, zone in ROWS],
)
def test_created_reads_every_stamp_through_the_shared_parser(
    value: str, zone: str | None
) -> None:
    with _host_zone(zone):
        assert catalogue._created(value) == _expected(value)


def test_the_numeric_branch_is_a_plain_float() -> None:
    """A number is returned as itself, not routed through the parser.

    Only the string branch moved to the shared parser. ``_created`` returns a
    numeric ``created`` as-is, where ``parse_utc`` would apply its epoch
    seconds/milliseconds threshold; a value at or above the threshold must
    stay the number it arrived as. A ``bool`` is not a numeric stamp and is
    ``None``.
    """

    assert catalogue._created(1709620028) == 1709620028.0
    assert catalogue._created(1e12) == 1e12
    assert catalogue._created(True) is None