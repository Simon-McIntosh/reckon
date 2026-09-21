"""The window reader: newest carrying report, its age, or an explicit unknown.

The fixtures imitate the real streams in the two respects that matter, both
measured from a fleet sample: only a minority of events carry
``unifiedWindows``, and a rate-limit event carries no timestamp of its own, so
its age must be anchored off the records around it. Both are what the reader's
rules exist for, so both appear in the fixtures rather than being assumed away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew.window_reading import read_windows

FIVE_HOUR_RESET = 1787751600
SEVEN_DAY_RESET = 1788206400
NOW = datetime(2026, 9, 21, 12, 0, 1, tzinfo=UTC)


def _stamped(minutes_ago: int) -> dict[str, object]:
    """An ordinary stamped record, as a rate-limit event is bracketed by."""
    moment = NOW - timedelta(minutes=minutes_ago)
    return {"type": "assistant", "timestamp": moment.isoformat()}


def _rate_limit_event(
    windows: dict[str, object] | None = None,
    *,
    top_level: float | None = None,
    rate_limit_type: str = "five_hour",
) -> dict[str, object]:
    info: dict[str, object] = {
        "status": "allowed",
        "rateLimitType": rate_limit_type,
        "resetsAt": FIVE_HOUR_RESET,
    }
    if top_level is not None:
        info["utilization"] = top_level
    if windows is not None:
        info["unifiedWindows"] = windows
    return {"type": "rate_limit_event", "rate_limit_info": info}


def _both_windows(five_hour: float, seven_day: float) -> dict[str, object]:
    return {
        "five_hour": {"utilization": five_hour, "resetsAt": FIVE_HOUR_RESET},
        "seven_day": {"utilization": seven_day, "resetsAt": SEVEN_DAY_RESET},
    }


def test_newest_carrying_event_wins_over_a_newest_that_carries_none() -> None:
    """A newer windowless report does not erase the window."""
    events = [
        _stamped(90),
        _rate_limit_event(_both_windows(0.61, 0.46)),
        _stamped(89),
        _stamped(1),
        _rate_limit_event(top_level=0.05, rate_limit_type="overage"),
    ]

    reading = read_windows(events, now=NOW)

    assert reading.known
    assert reading.utilisation("five_hour") == 0.61
    assert reading.utilisation("seven_day") == 0.46
    assert reading.utilisation("five_hour") != 0.05
    assert {f.period for f in reading.figures} == {"five_hour", "seven_day"}


def test_a_stream_whose_every_event_lacks_a_window_is_unknown() -> None:
    """Absence is neither exhaustion nor headroom: it is unknown."""
    events = [
        _stamped(40),
        _rate_limit_event(top_level=1.21, rate_limit_type="overage"),
        _stamped(1),
    ]

    reading = read_windows(events, now=NOW)

    assert not reading.known
    assert reading.figures == ()
    for period in ("five_hour", "seven_day"):
        value = reading.utilisation(period)
        assert value is None
        assert value not in (0.0, 1.0)
        assert reading.figure(period) is None
    assert reading.reason


def test_a_window_reported_as_zero_is_returned_as_zero() -> None:
    """Positive control: the reader does return a 0.0 the provider reported."""
    events = [_stamped(5), _rate_limit_event(_both_windows(0.0, 0.31))]

    reading = read_windows(events, now=NOW)

    assert reading.known
    assert reading.utilisation("five_hour") == 0.0
    assert reading.utilisation("seven_day") == 0.31


def test_every_figure_carries_its_observation_age_in_seconds() -> None:
    """The age is anchored off the following record; both periods share it."""
    events = [
        _stamped(30),
        _rate_limit_event(_both_windows(0.13, 0.31)),
        _stamped(29),
        _stamped(10),
    ]

    reading = read_windows(events, now=NOW)

    assert reading.observed_at is not None
    assert reading.age_seconds == 29 * 60
    for figure in reading.figures:
        assert figure.age_seconds == 29 * 60
        assert figure.observed_at == reading.observed_at


def test_the_age_prefers_the_following_record_when_distances_are_equal() -> None:
    """Ageing a reading too far triggers a needless re-query, so ties go forward."""
    events = [
        _stamped(10),
        _rate_limit_event(_both_windows(0.5, 0.5)),
        _stamped(8),
    ]

    reading = read_windows(events, now=NOW)

    assert reading.age_seconds == 8 * 60


def test_a_window_with_no_observation_time_anywhere_is_unknown() -> None:
    """A figure whose observation time cannot be established is not a figure."""
    events = [
        {"type": "assistant"},
        _rate_limit_event(_both_windows(0.2, 0.3)),
        {"type": "assistant"},
    ]

    reading = read_windows(events, now=NOW)

    assert not reading.known
    assert reading.utilisation("five_hour") is None
    assert reading.reason


def test_a_stream_path_and_a_stream_of_lines_are_both_read(tmp_path: Path) -> None:
    """The reader reaches a run's stream on disk, which is how a caller has it."""
    events = [_stamped(12), _rate_limit_event(_both_windows(0.44, 0.52))]
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    from_path = read_windows(stream, now=NOW)
    as_lines = read_windows([json.dumps(event) for event in events], now=NOW)

    assert from_path.utilisation("five_hour") == 0.44
    assert as_lines.utilisation("seven_day") == 0.52
    assert from_path.age_seconds == 12 * 60
    assert as_lines.age_seconds == 12 * 60

    missing = read_windows(tmp_path / "absent.jsonl", now=NOW)
    assert not missing.known
    assert missing.reason


def test_the_overage_top_level_figure_is_never_a_window() -> None:
    """An overage record is where the mistake is most available."""
    events = [
        _stamped(3),
        _rate_limit_event(
            _both_windows(0.7, 0.2), top_level=1.21, rate_limit_type="overage"
        ),
        _rate_limit_event(top_level=1.21, rate_limit_type="overage"),
    ]

    reading = read_windows(events, now=NOW)

    assert reading.utilisation("five_hour") == 0.7
    assert reading.utilisation("seven_day") == 0.2
    assert all(figure.utilisation != 1.21 for figure in reading.figures)


def test_a_z_suffixed_stamp_anchors_the_age() -> None:
    """The streams write a trailing ``Z``, so that is the form the anchor reads.

    The other fixtures build their stamps with ``isoformat``, which emits
    ``+00:00``. This one uses the form the fleet actually carries, because a
    parser handling only the offset form would age every real reading as
    unknown, and no fixture written the convenient way would say so.
    """
    events = [
        _rate_limit_event(_both_windows(0.13, 0.31)),
        {"type": "assistant", "timestamp": "2026-09-21T11:31:01Z"},
    ]

    reading = read_windows(events, now=NOW)

    assert reading.utilisation("five_hour") == 0.13
    assert reading.utilisation("seven_day") == 0.31
    assert reading.age_seconds == 29 * 60
