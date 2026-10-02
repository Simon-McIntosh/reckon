"""The remaining direct timestamp callers read through reckon._timestamps.

Three functions still parsed an ISO-8601 stamp themselves: the worker time-fence
statement, the repair's record-moment reader, and the served-code drift summary.
Each now reads through the shared parser while keeping the same output on the
six input shapes a caller meets — a valid UTC stamp, a numeric offset, a
zone-less stamp, a ``Z`` suffix, malformed text and the empty string.

The zone-less time-fence row is asserted against the base semantics rather than
a literal: the fence renders a zone-less launch instant through the host's local
zone, so a hardcoded deadline would encode whichever machine ran the test. Every
other row is a value recorded from the base revision.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest

from reckon.crew.node import parse_duration
from reckon.crew.prompts import _utc_instant, time_fence_statement
from reckon.crew.repair import _parsed_moment
from reckon.served_code import _summary

BUDGET = "45m"
UTC_STAMP = "2026-09-28T21:30:00+00:00"
OFFSET_STAMP = "2026-09-28T23:30:00+02:00"
NAIVE_STAMP = "2026-09-28T21:30:00"
Z_STAMP = "2026-09-28T21:30:00Z"
MALFORMED = "not a timestamp"
EMPTY = ""

FILES = ["a.py", "b.py"]

# The base revision's outputs, read by running each reader on the six inputs: a
# zone-less stamp is taken as UTC, an offset keeps the zone it states, and a
# malformed or empty stamp yields no moment.
PARSED_MOMENT_TABLE = [
    (UTC_STAMP, datetime(2026, 9, 28, 21, 30, tzinfo=UTC)),
    (OFFSET_STAMP, datetime(2026, 9, 28, 23, 30, tzinfo=timezone(timedelta(hours=2)))),
    (NAIVE_STAMP, datetime(2026, 9, 28, 21, 30, tzinfo=UTC)),
    (Z_STAMP, datetime(2026, 9, 28, 21, 30, tzinfo=UTC)),
    (MALFORMED, None),
    (EMPTY, None),
]

# The base revision's summary rendered a zone-carrying stamp by its wall clock,
# so an offset stamp reads 23:30 rather than the 22:15 UTC it names, and a
# malformed stamp is repeated verbatim.
SUMMARY_STARTED_TABLE = [
    (UTC_STAMP, "2026-09-28 21:30 UTC"),
    (OFFSET_STAMP, "2026-09-28 23:30 UTC"),
    (NAIVE_STAMP, "2026-09-28 21:30 UTC"),
    (Z_STAMP, "2026-09-28 21:30 UTC"),
    (MALFORMED, "not a timestamp"),
    (EMPTY, "an unknown time"),
]

# The fence's deadline on a zone-carrying launch: the launch instant plus the
# budget, rendered in UTC. The zone-less row is computed from the same base
# semantics in its own assertion below.
FENCE_DEADLINE_TABLE = [
    (UTC_STAMP, "2026-09-28T22:15:00Z"),
    (OFFSET_STAMP, "2026-09-28T22:15:00Z"),
    (Z_STAMP, "2026-09-28T22:15:00Z"),
]

DIRECT_PARSER_CALLERS = {
    "time_fence_statement": time_fence_statement,
    "_parsed_moment": _parsed_moment,
    "_summary": _summary,
}


def test_none_of_the_three_calls_fromisoformat_directly():
    for name, func in DIRECT_PARSER_CALLERS.items():
        source = inspect.getsource(func)
        assert "fromisoformat" not in source, name
        assert "parse_utc" in source or "parse_iso" in source, name


@pytest.mark.parametrize(
    ("stamp", "expected"),
    PARSED_MOMENT_TABLE,
)
def test_parsed_moment_keeps_base_output(stamp, expected):
    assert _parsed_moment(stamp) == expected


def test_parsed_moment_keeps_the_zone_an_offset_stamp_states():
    moment = _parsed_moment(OFFSET_STAMP)
    assert moment is not None
    assert moment.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(
    ("stamp", "expected_started"),
    SUMMARY_STARTED_TABLE,
)
def test_summary_keeps_base_output(stamp, expected_started):
    assert f"it started at {expected_started} (" in _summary(FILES, stamp, "executed")


@pytest.mark.parametrize(
    ("stamp", "expected_deadline"),
    FENCE_DEADLINE_TABLE,
)
def test_time_fence_keeps_base_deadline_on_zoned_launches(stamp, expected_deadline):
    statement = time_fence_statement(time_budget=BUDGET, launch_instant=stamp)
    assert f"deadline {expected_deadline} —" in statement


def test_time_fence_keeps_base_deadline_on_a_zone_less_launch():
    launch = datetime.fromisoformat(NAIVE_STAMP)
    expected = _utc_instant(launch + timedelta(seconds=parse_duration(BUDGET)))
    statement = time_fence_statement(time_budget=BUDGET, launch_instant=NAIVE_STAMP)
    assert f"deadline {expected} —" in statement


@pytest.mark.parametrize("stamp", [MALFORMED, EMPTY])
def test_time_fence_still_refuses_an_unparseable_launch(stamp):
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        time_fence_statement(time_budget=BUDGET, launch_instant=stamp)
