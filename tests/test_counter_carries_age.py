"""The counter block carries counts alone: the span beside a count is gone.

The row's counters are totals. A total holds no membership and no time, so the
age of the oldest member of an actionable bucket used to be printed beside the
count it qualified — that was the one reading on the row separating a backlog
being worked from one standing still. The cell is removed: the row has no room
for a figure its reader goes to another surface for, and the surface that
answers it is the needs-you line, which spells the age beside the node it
belongs to. Nothing computes a bucket age while that line is unbuilt, so this
file holds the row to what follows: two fleets with the same counts render the
same row whatever their arrival times, the fleet whose oldest member has stood
for hours prints no span, and the one span the row still prints is the reading
run's own elapsed time.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# A span printed beside a counter — the cell the row stopped carrying: a minute,
# hour or day count with the letter of the bucket it qualified immediately after
# it. The counts are digits followed by w, b, u or q with no unit between them,
# so requiring a unit before the letter finds a span and never a count.
AGES = re.compile(r"(\d+[mhd]\+?)([buq])")

# Two transitions of the same run, far enough apart that a rendered span would
# land in different units and could not be confused by a rounding difference.
ARRIVED_LONG_AGO = "2026-09-18T05:00:00+00:00"
ARRIVED_JUST_NOW = "2026-09-18T11:58:00+00:00"
READ_AT = "2026-09-18T12:00:00+00:00"


def plain(line: str) -> str:
    """The line as the grid measures it, with any colour removed."""
    return ESCAPES.sub("", line)


def spans(line: str) -> dict[str, str]:
    """Each bucket letter's rendered span on this row, keyed by its letter."""
    return {letter: span for span, letter in AGES.findall(plain(line))}


def _transition(run: str, at: str, state: str = "blocked", **overrides):
    """One transition, placing ``run`` in the bucket ``state`` belongs to."""
    event = {
        "observed_at": at,
        "run_id": run,
        "node": run,
        "session": "ship-s18-20260918",
        "role": "implement",
        "from_state": "working",
        "to_state": state,
        "working": 0,
        "blocked": 0,
        "unpromoted": 0,
    }
    event.update(overrides)
    return event


def _reading(at: str, **overrides):
    """A row rendered at ``at``, carrying no reason of its own."""
    return _transition("r-reader", at, state="working", **overrides)


def _row(events, at: str = READ_AT, width: int = 180, **overrides) -> str:
    """Render one row on a grid that has seen ``events``, returned plainly."""
    grid = ticker_module.Ticker(width=width, color=False)
    for event in events:
        grid.render(event)
    return plain(grid.render(_reading(at, **overrides)))


def test_the_pattern_matches_the_span_beside_a_counter_it_forbids():
    """The pattern can fire: it matches the cell the row no longer carries.

    Without this control every absence assertion below would also pass on a
    pattern that matches nothing at all. The shape it forbids is a span with the
    letter of its bucket printed adjacent, and here is that shape.
    """
    assert AGES.search(" 7hb")
    assert AGES.search(" 12mu")
    assert AGES.search(" 2dq")


def test_two_fleets_with_the_same_counts_render_identically():
    """The count is the whole reading again, whatever the members' arrival times.

    The inversion of what the cell existed for. A fresh arrival and a backlog
    standing for seven hours were once made to render differently by design, and
    they now render the same row, because the row carries no age of any member
    to separate them.
    """
    fresh = _row([_transition("r-a", ARRIVED_JUST_NOW)], blocked=1)
    stale = _row([_transition("r-a", ARRIVED_LONG_AGO)], blocked=1)
    assert fresh == stale
    assert spans(stale) == {}


def test_a_long_standing_member_prints_no_span_beside_its_counter():
    """The population that used to produce a span renders the count alone.

    Run against the fleet whose oldest blocked member arrived seven hours before
    the reading — the exact case a span beside the counter existed to make
    visible — so a span still rendered would be found here rather than missed on
    a fleet too young to have one.
    """
    row = _row([_transition("r-old", ARRIVED_LONG_AGO)], blocked=1)
    assert spans(row) == {}
    assert not AGES.search(row)
    assert "1b" in row


def test_the_only_span_on_the_row_is_the_reading_runs_own_elapsed_time():
    """A stale backlog does not leak into the one measure cell that remains.

    The row still prints one span — the elapsed time of the run the row is about
    — and it reads that run's own fact rather than anything about the fleet
    behind it: the seven-hour-old member leaves no span on the row, and the
    elapsed figure appears from the reading run's own spend.
    """
    row = _row(
        [_transition("r-old", ARRIVED_LONG_AGO)],
        blocked=1,
        spend_wall_seconds=1_234.0,
    )
    assert "20m" in row
    assert spans(row) == {}


@pytest.mark.parametrize("width", [180, 208])
def test_a_stale_fleet_row_holds_the_requested_width(width):
    """Removing the cell must not leave a gap where it stood.

    The cell was paid for out of the free text's margin, so a row that grew to
    keep it — or kept the width it spent — would wrap in a pane the grid already
    fills, and a wrapped row costs the reader two lines of the eight a pane
    shows.
    """
    stale = _row([_transition("r-old", ARRIVED_LONG_AGO)], width=width, blocked=1)
    assert len(stale) == width
    assert spans(stale) == {}
