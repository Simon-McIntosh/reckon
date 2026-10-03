"""A row's generation rate is shown beside the lane's mean, or not at all.

A rate is a reading about one worker with no denominator on the row, and a
reader holding only that number attributes a reader-wide cause to whichever
node it owns: measured, two coordinators read the same figure as a slow worker
and as a slow lane. So the row carries the lane's mean in the same cell as the
run's own rate, from the lane reading the run's record already carries, and a
record that carries no reading prints neither figure — a rate without the
lane's own on the row is the reading this cell exists to replace. A reading
that measured no mean is named rather than numbered, because an unmeasured lane
and a lane whose figure was withheld must not read alike.

The rows are rendered through the ticker's own formatter, so what is asserted
is the row a pane draws rather than a helper behind it.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The two figures the row puts side by side: the run's own rate and the mean
# the lane's reading reports. Distinct values, because a cell that repeated one
# figure twice would satisfy an assertion that only looked for a number.
ROW_RATE = 1.4
LANE_MEAN = 1.44

DETAIL = "the gate refused and no receipt was written"
# The clause's opening, which is all a row carrying the lane cell has room for
# inside the 180-column pane.
CLAUSE_HEAD = DETAIL.split(" and ", maxsplit=1)[0]


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


def _fresh_reading(mean: float = LANE_MEAN) -> dict:
    """The carry the producer composes from a lane document it could read.

    The shape is the producer's own: the reading names its state, its counts
    and a throughput block, so the row consumes exactly what a dispatch already
    carries rather than a private spelling of the figure.
    """
    return {
        "state": "fresh",
        "headroom": 13,
        "generating": 3,
        "waiting": 0,
        "throughput": {
            "state": "measured",
            "mean_tokens_per_second": mean,
            "aggregate_tokens_per_second": mean * 3,
            "runs": 3,
            "observed_at": "2026-10-03T09:30:00Z",
            "age_seconds": 12,
            "detail": "",
        },
        "binding_observed": None,
        "mean_context": 136000,
        "observed_at": "2026-10-03T09:30:00Z",
        "age_seconds": 12,
        "suggested_shelf_life_seconds": 45,
        "detail": "",
    }


def _unknown_reading() -> dict:
    """The carry the producer composes when no figure could be read.

    This is the absent lane on a row: the reading arrives, and says that the
    lane published no mean rather than publishing a zero a reader would take
    for a measurement.
    """
    return {
        "state": "unknown",
        "headroom": "unknown",
        "generating": "unknown",
        "waiting": "unknown",
        "throughput": {
            "state": "unknown",
            "mean_tokens_per_second": "unknown",
            "aggregate_tokens_per_second": "unknown",
            "runs": "unknown",
            "observed_at": None,
            "age_seconds": None,
            "detail": "no lane document",
        },
        "binding_observed": "unknown",
        "mean_context": "unknown",
        "observed_at": None,
        "age_seconds": None,
        "suggested_shelf_life_seconds": None,
        "detail": "no lane document",
    }


def _event(**overrides):
    event = {
        "observed_at": "2026-10-03T09:30:00+00:00",
        "run_id": "r-mean-beside",
        "node": "n-mean-beside",
        "role": "implement",
        "from_state": "working",
        "to_state": "working",
        "working": 3,
        "blocked": 1,
        "unpromoted": 7,
        "model": "dsv4.1-flash",
        "effort": "xhigh",
        "spend_wall_seconds": 3421.0,
        "spend_generation_rate": ROW_RATE,
        "detail": DETAIL,
    }
    event.update(overrides)
    return event


@pytest.fixture
def grid():
    """A grid whose model cell is pinned, so the row's columns are its own."""
    return ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())


def test_a_row_with_a_lane_reading_shows_both_figures_side_by_side(grid):
    """The run's rate and the lane's mean stand in one cell, run first.

    The pair is the whole change: the row already carried the run's own figure,
    and the lane's mean arriving beside it is what lets a reader tell a slow
    worker from a slow lane.
    """
    line = plain(grid.render(_event(to_state="blocked", lane_reading=_fresh_reading())))
    pair = "1.40 lane  1.44"
    assert pair in line, line
    # Beside each other on the row, after the fleet counters and before the
    # clause, which is where the reading says the two figures stand.
    assert line.index(pair) > line.index("3w"), line
    assert line.index(pair) < line.index(CLAUSE_HEAD), line


@pytest.mark.parametrize(
    ("mean", "rendered"),
    [
        (0.4, "0.40"),
        (1.44, "1.44"),
        (20.0, "20.0"),
    ],
)
def test_the_lane_figure_follows_the_reading_it_was_given(grid, mean, rendered):
    """The cell renders the reading rather than a constant.

    A figure that did not follow its input would make the row a decoration
    rather than a reading, so the same row is rendered against three lane means
    and the cell must carry each of them beside the run's own rate. Two
    decimals below ten keep figures a reader compares at the resolution that
    separates them; above it one decimal is all the column needs.
    """
    line = plain(grid.render(_event(lane_reading=_fresh_reading(mean=mean))))
    assert f"{rendered} " in line, (mean, line)
    assert f"{rendered} " in line.split(ticker_module.LANE_LABEL)[1], (mean, line)
    assert "1.40 " in line, (mean, line)


def test_a_row_with_no_lane_reading_prints_no_rate(grid):
    """A rate is never shown without the lane figure beside it.

    This is the row's guarantee rather than an omission: the number that was
    read as a lane's verdict is withheld entirely when the record carries no
    reading, so the failure cannot recur through this cell.
    """
    with_reading = plain(grid.render(_event(lane_reading=_fresh_reading())))
    without = plain(grid.render(_event()))
    # The control: the same event with a reading does print the figure, so the
    # absence below is the reading's absence rather than a formatter that never
    # prints a rate at all.
    assert f"{ROW_RATE:.2f}" in with_reading, with_reading
    assert f"{ROW_RATE:.2f}" not in without, without
    assert ticker_module.LANE_LABEL not in without, without


def test_an_unmeasured_lane_reading_is_named_not_numbered(grid):
    """The absence is named: no figure, and no zero standing in for one.

    A reading that measured no mean is the case the producer composes when the
    document is unreadable or stale, and the row must say so in words. A number
    here would be the defect the lane's zero-versus-absent rule prevents on
    every other surface.
    """
    line = plain(grid.render(_event(lane_reading=_unknown_reading())))
    assert ticker_module.LANE_UNKNOWN in line, line
    assert f"{ROW_RATE:.2f}" not in line, line
    assert f"{LANE_MEAN:.2f}" not in line, line


def test_a_lane_reading_that_is_not_an_object_is_named(grid):
    """A malformed carry reads as unmeasured rather than as a figure.

    The producer composes a mapping, and a row handed anything else must not
    rendered a number out of it or raise on it.
    """
    for reading in ("unknown", [], 3, {"state": "fresh"}):
        line = plain(grid.render(_event(lane_reading=reading)))
        assert ticker_module.LANE_UNKNOWN in line, (reading, line)


def test_the_counters_keep_their_column_whether_a_reading_is_carried(grid):
    """The lane cell is added after the counters, so the fleet figures hold.

    The counters are the pane's fixed frame; a cell that displaced them would
    make a reader re-find the reader's own fleet numbers on every row that
    carried a reading. One row carries the cell and one does not, and the count
    must land on the same column in both.
    """
    without = plain(grid.render(_event()))
    with_reading = plain(grid.render(_event(lane_reading=_fresh_reading())))
    assert without.index("3w") == with_reading.index("3w"), (without, with_reading)


def test_the_row_stays_exactly_the_pane_width(grid):
    """The lane cell spends the clause's margin, never the pane's edge."""
    for reading in (None, _fresh_reading(), _unknown_reading()):
        event = _event() if reading is None else _event(lane_reading=reading)
        line = plain(grid.render(event))
        assert len(line) == grid.width, (len(line), line)


def test_the_clause_survives_beside_the_lane_cell(grid):
    """The reason is still the row's payload, cut to the room the cell leaves.

    A lane cell that overflowed or wrapped would cost the row the clause a
    reader came for, and a wrapped row costs a quarter of the pane's history.
    The cell does spend margin — the clause is shorter here than on a row
    carrying no reading — so what is asserted is that the reason still starts
    after the figures and that the row is still one line of the pane's width.
    """
    line = plain(grid.render(_event(to_state="blocked", lane_reading=_fresh_reading())))
    assert CLAUSE_HEAD in line, line
    assert line.index(CLAUSE_HEAD) > line.index(ticker_module.LANE_LABEL), line
    assert len(line) == grid.width, line
