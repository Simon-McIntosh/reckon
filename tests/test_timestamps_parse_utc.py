"""Behaviour of the one UTC timestamp parser.

Each case fixes a shape a caller already meets: a Z suffix, an explicit
offset, a naive value, an epoch in seconds, an epoch in milliseconds, and
malformed input. The naive case is the one policy choice this module makes
rather than inherits from the input, so it is asserted against a fixed moment
rather than against the machine's local zone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reckon._timestamps import parse_utc

UTC_MOMENT = datetime(2026, 9, 28, 21, 30, 0, tzinfo=UTC)


def test_z_suffix_returns_aware_utc():
    result = parse_utc("2026-09-28T21:30:00Z")
    assert result == UTC_MOMENT
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_explicit_offset_returns_aware_utc():
    result = parse_utc("2026-09-28T23:30:00+02:00")
    assert result == UTC_MOMENT
    assert result.utcoffset() == timedelta(0)


def test_negative_offset_normalises_to_utc():
    result = parse_utc("2026-09-28T16:30:00-05:00")
    assert result == UTC_MOMENT
    assert result.utcoffset() == timedelta(0)


def test_naive_value_is_read_as_utc():
    result = parse_utc("2026-09-28T21:30:00")
    assert result == UTC_MOMENT
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_naive_value_does_not_depend_on_local_zone():
    # A local-time reading would shift this by the host's offset; the module
    # reads it as UTC regardless of what the host's zone is.
    result = parse_utc("2026-09-28T21:30:00")
    assert result.timestamp() == UTC_MOMENT.timestamp()


def test_epoch_seconds_is_read():
    result = parse_utc(int(UTC_MOMENT.timestamp()))
    assert result == UTC_MOMENT


def test_epoch_milliseconds_is_read():
    result = parse_utc(int(UTC_MOMENT.timestamp() * 1000))
    assert result == UTC_MOMENT


def test_epoch_seconds_and_milliseconds_are_told_apart_by_magnitude():
    seconds = parse_utc(1_800_000_000)
    millis = parse_utc(1_800_000_000_000)
    assert seconds == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert millis == seconds


def test_malformed_string_returns_none():
    assert parse_utc("not a timestamp") is None


def test_empty_string_returns_none():
    assert parse_utc("") is None


def test_none_returns_none():
    assert parse_utc(None) is None


def test_boolean_is_not_an_epoch():
    assert parse_utc(True) is None
    assert parse_utc(False) is None


def test_non_timestamp_type_returns_none():
    assert parse_utc(["2026-09-28T21:30:00Z"]) is None
    assert parse_utc({"stamp": "2026-09-28T21:30:00Z"}) is None


def test_out_of_range_epoch_returns_none():
    assert parse_utc(1e30) is None


def test_returned_moment_is_always_utc():
    for value in (
        "2026-09-28T21:30:00Z",
        "2026-09-28T23:30:00+02:00",
        "2026-09-28T21:30:00",
        1_800_000_000,
        1_800_000_000_000,
    ):
        result = parse_utc(value)
        assert result is not None
        assert result.tzinfo is UTC
