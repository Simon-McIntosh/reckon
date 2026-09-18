"""A counter total cannot show whether the row's backlog is moving.

The inbox cells read the same whether the same item has stood there for an hour
or a stalled one was cleared this second and a fresh one took its place: the
figure is a total, and a total has no membership and no time in it. The age of
the oldest member is what separates the two — it climbs while nothing is
cleared and falls when the oldest item is dealt with.

The counter block is also the part of the row a reader scans down a column, so
the age arrives without moving a single column of it, and an empty bucket
renders nothing rather than a zero, because a zero in this position reads as
fresh work rather than as none.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# An age carries the letter of the counter it qualifies, and its own unit tells
# it apart from that counter: the counts are digits followed by w, b, u or q
# with no unit between them, so requiring a minute, hour or day unit before the
# letter finds the ages and never the counts.
AGES = re.compile(r"(\d+[mhd]\+?)([bu])")

# Two transitions of the same run, far enough apart that their ages land in
# different units and could not be confused by a rounding difference.
ARRIVED_LONG_AGO = "2026-09-18T05:00:00+00:00"
ARRIVED_JUST_NOW = "2026-09-18T11:58:00+00:00"
READ_AT = "2026-09-18T12:00:00+00:00"


def plain(line: str) -> str:
    """The line as the grid measures it, with any colour removed."""
    return ESCAPES.sub("", line)


def ages(line: str) -> dict[str, str]:
    """Each actionable bucket's rendered age on this row, keyed by its letter."""
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
    """A row rendered at ``at``, carrying no reason of its own.

    The reason is left empty because the age is paid for out of the free text:
    a clause that claims the margin keeps it whole and the age blanks, so a
    fixture that exists to read an age must not spend the margin on prose.
    """
    return _transition("r-reader", at, state="working", **overrides)


def _row(events, at: str = READ_AT, width: int = 180, **overrides) -> str:
    """Render one row on a grid that has seen ``events``, returned plainly."""
    grid = ticker_module.Ticker(width=width, color=False)
    for event in events:
        grid.render(event)
    return plain(grid.render(_reading(at, **overrides)))


def test_a_fleet_with_nothing_actionable_carries_no_age():
    """Nothing outstanding renders no age, and never a zero.

    Asserted first, because it is the half that fails silently: an empty bucket
    printing ``0m`` would read as work that had only just arrived, which is the
    opposite of what it is, and no cell would be left to tell the two apart.
    """
    row = _row(
        [_transition("r-working", ARRIVED_LONG_AGO, state="working")],
        blocked=0,
        unpromoted=0,
    )
    assert ages(row) == {}
    assert "0m" not in row
    assert "0d" not in row


def test_two_fleets_with_the_same_counts_and_different_ages_render_differently():
    """One figure, two situations, two rows — the change this node lands.

    Both rows carry the same counts and differ only in when the oldest member
    of the bucket arrived. If they rendered identically the age did not take and
    the cell is still only a total.
    """
    fresh = _row([_transition("r-a", ARRIVED_JUST_NOW)], blocked=1)
    stale = _row([_transition("r-a", ARRIVED_LONG_AGO)], blocked=1)
    assert fresh != stale
    assert ages(fresh) == {"b": "2m"}
    assert ages(stale) == {"b": "7h"}


def test_the_oldest_member_sets_the_age_and_a_fresh_arrival_does_not_reset_it():
    """A new arrival beside a long-standing item leaves the reading where it was.

    This is the membership turnover a total cannot show: the count does not
    move, a fresh item has just landed, and the backlog has in fact been
    standing still the whole time. Reading the newest stamp would report the
    arrival and hide the backlog.
    """
    row = _row(
        [
            _transition("r-old", ARRIVED_LONG_AGO),
            _transition("r-new", ARRIVED_JUST_NOW),
        ],
        blocked=2,
    )
    assert ages(row) == {"b": "7h"}


def test_the_age_falls_when_the_oldest_member_is_cleared():
    """Clearing the oldest item moves the reading, which is what it is for.

    A backlog being worked and one standing still are the two situations the
    count cannot separate. Here the count is unchanged between the two
    readings — two blocked either way — and the age dropped because the oldest
    one left the bucket.
    """
    events = [
        _transition("r-old", ARRIVED_LONG_AGO),
        _transition("r-young", ARRIVED_JUST_NOW),
    ]
    before = _row(events, blocked=2)
    grid = ticker_module.Ticker(width=180, color=False)
    for event in events:
        grid.render(event)
    # The oldest leaves the fleet entirely, so it is no longer a member of
    # anything the row counts even though the count is unchanged.
    grid.render(_transition("r-old", READ_AT, state="promoted", from_state="blocked"))
    after = plain(grid.render(_reading(READ_AT, blocked=2)))
    assert ages(before) == {"b": "7h"}
    assert ages(after) == {"b": "2m"}


def test_the_work_in_progress_cells_carry_no_age():
    """Only the buckets that ask something of the reader answer with an age.

    A blocked run needs a decision and an unpromoted one needs merging or
    recording; a working run and a queued run are progressing, and an age
    beside them would measure nothing a reader acts on.
    """
    row = _row(
        [
            _transition("r-blocked", ARRIVED_LONG_AGO),
            _transition("r-done", ARRIVED_LONG_AGO, state="complete"),
            _transition("r-working", ARRIVED_LONG_AGO, state="working"),
            _transition("r-waiting", ARRIVED_LONG_AGO, state="waiting"),
        ],
        blocked=1,
        unpromoted=1,
        working=1,
        waiting=1,
    )
    assert set(ages(row)) == {"b", "u"}


def test_the_counter_block_holds_one_column_as_counts_and_ages_change():
    """The counts' letters land on one screen column across every row.

    The block is what a reader scans down, so an age that widened it would move
    every count on the rows that carry one and defeat the scan.
    """
    rows = [
        _row(
            [_transition("r-a", ARRIVED_LONG_AGO)], working=12, blocked=9, unpromoted=7
        ),
        _row(
            [_transition("r-a", ARRIVED_JUST_NOW)], working=3, blocked=1, unpromoted=0
        ),
        _row([], working=3, blocked=0, unpromoted=0),
    ]
    assert len({row.index("w") for row in rows}) == 1, rows
    assert len({row.rindex("u") for row in rows}) == 1, rows


@pytest.mark.parametrize("width", [180, 208])
def test_the_row_holds_its_width_with_and_without_an_age(width):
    """The age is paid for out of the free text's margin, never by wrapping.

    A row that grew a column to carry an age would wrap in a pane the grid
    already fills, and a wrapped row costs the reader two lines of the eight a
    pane shows.
    """
    with_age = _row([_transition("r-a", ARRIVED_LONG_AGO)], width=width, blocked=1)
    without = _row([], width=width, blocked=0)
    assert len(with_age) == width
    assert len(without) == width
    assert ages(with_age)
    assert not ages(without)


def test_the_age_yields_the_margin_to_a_clause_that_needs_it():
    """A clause that fills the margin keeps it whole; the age gives way.

    The ages are paid for out of the free text, so a long clause cannot both
    keep the predicate a reader acts on and leave the columns an age would
    occupy. The predicate wins: it is the actionable text, and the age is a
    reading of it.
    """
    clause = "the process is gone without a complete manifest"
    crowded = _row(
        [_transition("r-a", ARRIVED_LONG_AGO)],
        to_state="blocked",
        detail=clause + " for the archive dry run which reported the marker moved",
        blocked=1,
    )
    assert clause in crowded
    assert ages(crowded) == {}
