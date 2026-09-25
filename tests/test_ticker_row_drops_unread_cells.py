"""The row's measure block carries the elapsed time and nothing a reader skips.

Two cells used to sit beside the elapsed time. A generation rate is a reading
about one model's throughput rather than about the run, so a row usually had
none and rendered the absence marker in its place; the oldest age in each
outstanding bucket is a fact about a backlog, which a reader goes to a surface
for rather than reading off a line in a scrolling pane. This file holds the row
to a single measure — how long the run has been going — rendered in hours and
minutes rather than as a clock, so a reader compares spans instead of decoding
timestamps. The columns those two cells spent are the recovered width asserted
below, and the margin the clause gains is the same count.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The age-plus-bucket cell this revision stops rendering: a span in minutes with
# the initial of the bucket it qualified immediately after it, as in the columns
# a row used to spend on the oldest member of each outstanding bucket.
AGE_PATTERN = re.compile(r"[0-9]+m[a-z]\b")

TRANSITION_COLUMN = (
    ticker_module.CLOCK
    + ticker_module.GAP
    + ticker_module.MODEL
    + ticker_module.GAP
    + ticker_module.EFFORT
    + ticker_module.GAP
    + ticker_module.ROLE
    + ticker_module.GAP
    + ticker_module.NODE
    + ticker_module.GAP
)
ELAPSED_COLUMN = (
    TRANSITION_COLUMN
    + ticker_module.STATE
    + ticker_module.GAP
    + ticker_module.SPEND_GAP
)
COUNTER_COLUMN = ELAPSED_COLUMN + ticker_module.SPEND + ticker_module.GAP
REASON_COLUMN = COUNTER_COLUMN + ticker_module.STATS + ticker_module.GAP

# The clause the blocked fixture renders, so the row's fixed columns can be
# lifted clear of it.
CLAUSE = "the gate refused and no receipt was written"


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


def _event(**overrides):
    event = {
        "observed_at": "2026-09-24T12:00:00+00:00",
        "run_id": "r-row-drops-cells",
        "node": "n-row-drops-cells",
        "role": "review",
        "from_state": "working",
        "to_state": "working",
        "working": 3,
        "blocked": 1,
        "unpromoted": 7,
        "model": "dsv4.1-flash",
        "effort": "xhigh",
        "spend_wall_seconds": 3421.0,
        "spend_generation_rate": 38.0,
        "detail": CLAUSE,
    }
    event.update(overrides)
    return event


def _classes() -> dict[str, dict]:
    """One event of every class the follower pane streams a line for."""
    return {
        "baseline": _event(event="baseline", to_state="unpromoted"),
        "working": _event(to_state="working"),
        "blocked": _event(to_state="blocked"),
        "unpromoted": _event(to_state="unpromoted"),
    }


def fixed_columns(line: str) -> str:
    """The row up to the clause, or the whole line when the class has none."""
    text = plain(line).rstrip()
    return text[: text.index(CLAUSE)].rstrip() if CLAUSE in text else text


def elapsed_cell(line: str) -> str:
    text = plain(line)
    return text[ELAPSED_COLUMN : ELAPSED_COLUMN + ticker_module.SPEND]


@pytest.fixture
def grid():
    """A grid whose model cell is pinned, so the row's columns are its own."""
    return ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())


def test_the_pattern_matches_the_age_cell_it_forbids():
    """The pattern can fire: it matches the cell the row no longer carries.

    Without this control every assertion below would also pass on a pattern
    that matches nothing at all. The shape it forbids is a span with the
    initial of its bucket printed adjacent, and here is that shape.
    """
    assert AGE_PATTERN.search(" 57mb")
    assert AGE_PATTERN.search(" 12mb")


@pytest.mark.parametrize("with_rate", [True, False])
def test_no_row_carries_an_age_cell(grid, with_rate):
    """No class of row renders a number followed by a bucket letter."""
    for name, event in _classes().items():
        if not with_rate:
            event.pop("spend_generation_rate")
        line = plain(grid.render(event))
        assert not AGE_PATTERN.search(line), (name, line)


@pytest.mark.parametrize("with_rate", [True, False])
def test_the_measure_block_holds_the_elapsed_figure_alone(grid, with_rate):
    """Nothing stands between the elapsed time and the clause.

    The absence marker had exactly one source on a row like these — the rate
    cell — so a row carrying none is a row without that cell, and the figure
    nearest the clause is the elapsed time rather than a second reading.
    """
    for name, event in _classes().items():
        if not with_rate:
            event.pop("spend_generation_rate")
        line = plain(grid.render(event))
        cell = elapsed_cell(line)
        assert ticker_module.DIM_MARKER not in cell, (name, line)
        assert cell.strip() == "57m", (name, line)


def test_an_unmeasured_span_marks_the_elapsed_position_once(grid):
    """An unmeasured span marks its own cell and adds no second marker."""
    line = plain(grid.render(_event(spend_wall_seconds=None)))
    cell = elapsed_cell(line)
    assert cell.strip() == ticker_module.DIM_MARKER, line
    assert plain(line).count(ticker_module.DIM_MARKER) == 1, line


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (3421.0, "57m"),
        (5580.0, "1h33m"),
        (0.0, "0m"),
        (3599.0, "59m"),
        (3600.0, "1h00m"),
        (359_999.0, "99h59m"),
        (360_000.0, "99h+"),
    ],
)
def test_elapsed_renders_hours_and_minutes_not_a_clock(grid, seconds, expected):
    """The span is a duration a reader compares, not a clock they decode."""
    line = plain(grid.render(_event(spend_wall_seconds=seconds)))
    cell = elapsed_cell(line)
    assert cell.strip() == expected, (seconds, line)
    assert ":" not in cell, (seconds, line)


def test_the_floor_is_the_fixed_grid_before_the_reason():
    assert ticker_module.MIN_WIDTH == REASON_COLUMN


def test_the_clause_starts_at_the_measured_column(grid):
    """The clause begins where the recovered columns put it, not where they claim.

    The base row's clause begins at column 126, which is the 120 fixed columns
    plus the six the age cells took from the margin. This row's begins at
    ``FIXED_BEFORE - RECOVERED_COLUMNS`` — the fixed columns, net of the state
    cell and queued counter this revision widens, less the six the rate and age
    cells spent — so the count is read off the row rather than taken from the
    constant beside it.
    """
    at = plain(grid.render(_event(to_state="blocked"))).index("the gate refused")
    assert at == REASON_COLUMN, at


def test_the_clause_gains_the_recovered_columns(grid):
    """The margin grew by the same count, and is still bounded there.

    A clause exactly the size of the margin is printed whole and one column
    more is elided, so the room claimed is the room: the pair is what says the
    recovered columns reached the clause rather than stopping at the counters.
    """
    room = 180 - plain(grid.render(_event(to_state="blocked"))).index(
        "the gate refused"
    )
    assert room == 180 - REASON_COLUMN, room
    whole = grid.render(_event(to_state="blocked", detail=("x" * (room - 1)) + "z"))
    elided = grid.render(_event(to_state="blocked", detail=("x" * room) + "z"))
    assert whole.rstrip().endswith("x" * (room - 1) + "z"), whole
    assert "…" in elided and not elided.rstrip().endswith("z"), elided
