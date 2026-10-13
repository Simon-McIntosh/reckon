"""The pane row is read in one order, whatever a row carries.

A reader scans this pane down a column rather than across a line, so every
field holds its screen column and the fields appear in one order on every row:
the clock, the stage, the job, the transition, the elapsed span and the four
fleet counters, which every row carries at fixed widths, and then the lane's
rate, the model and the effort. The trailing three are added left to right only
while the pane has room, so a narrow pane drops the effort first and the rate
last and never wraps. Two facts inside the row are asserted here as well: the
four counters read w, h, d and q, and a review's job cell drops the prefix and
the reviewed head's digest the stage cell already says.

This file renders rows through :meth:`Ticker.render` and reads the columns off
the rendered characters, so an assertion is about the cell a reader sees rather
than about a format string.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The fixed columns a row starts with, read off the module's own widths, so a
# change to any of them moves the offsets here rather than leaving a stale
# literal matching a column that moved.
CLOCK_AT = 0
STAGE_AT = ticker_module.CLOCK + ticker_module.GAP
JOB_AT = STAGE_AT + ticker_module.ROLE + ticker_module.GAP
CLOCK_PATTERN = re.compile(r"\d\d:\d\d:\d\d")


def plain(line: str) -> str:
    """The row as the grid measures it, with any colour removed."""
    return ESCAPES.sub("", line)


def _event(**overrides):
    event = {
        "observed_at": "2026-09-24T12:00:00+00:00",
        "run_id": "r-row-order",
        "node": "n-row-order",
        "role": "implement",
        "from_state": None,
        "to_state": "working",
        "working": 3,
        "blocked": 1,
        "unpromoted": 7,
        "waiting": 2,
        "spend_wall_seconds": 3421.0,
    }
    event.update(overrides)
    return event


def _cell(line: str, start: int, width: int) -> str:
    """The cell occupying ``start`` for ``width`` columns of a rendered row."""
    return plain(line)[start : start + width]


@pytest.fixture
def grid():
    """A grid whose model cell is pinned, so the row's columns are its own."""
    return ticker_module.Ticker(
        width=180, theme="light", color=False, model_aliases=("sonnet5",)
    )


@pytest.fixture
def lane_row(tmp_path, monkeypatch):
    """A grid and an event whose lane publishes a rate the row can show.

    The rate cell reads the lane document the row's own backend declares, so a
    row carrying a rate needs a resolved flight config naming that backend and
    a lane document to read. Both are synthesised under the test's own tmp path,
    so nothing outside the repository under test is read or written.
    """
    now = datetime.now(UTC).isoformat()
    document = tmp_path / "lane.json"
    document.write_text(
        json.dumps(
            {
                "observed_at": now,
                "suggested_shelf_life_seconds": 3600,
                "throughput": {
                    "mean_tokens_per_second": 38.2,
                    "aggregate_tokens_per_second": 38.2,
                    "runs": 1,
                    "observed_at": now,
                },
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "flight.yaml"
    config.write_text(
        "version: 1\n"
        "default_backend: local\n"
        "backends:\n"
        "  local:\n"
        "    launch: in-harness\n"
        "    sandbox: worktree-full\n"
        f"    lane_document: {document}\n"
        "roles:\n"
        "  implement: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(config))
    event = _event(
        model="claude-sonnet-5",
        alias="sonnet5",
        effort="medium",
        backend="local",
    )
    return ticker_module.Ticker(
        width=180, theme="light", color=False, model_aliases=("sonnet5",)
    ), event


def test_the_cells_sit_on_one_screen_column_in_the_reading_order(lane_row):
    """Every cell's offset increases in the order a reader scans the row.

    The head — clock, stage, job, transition, elapsed, counters — is fixed, so
    each of its cells starts further right than the one before it, and the
    trailing rate, model and effort follow the counters in that order. The
    order is asserted by screen offset rather than by the order the renderer
    appends its cell list, because it is the reader's left-to-right scan that
    has to hold.
    """
    grid, event = lane_row
    line = plain(grid.render(event))

    offsets = {
        "clock": CLOCK_AT,
        "stage": line.index("implement"),
        "job": line.index("n-row-order"),
        "transition": line.index(ticker_module.ARROW),
        "elapsed": line.index("57m"),
        "counters": line.index("3w"),
        "rate": line.index("tok/s"),
        "model": line.index("sonnet5"),
        "effort": line.index("medium"),
    }
    ordered = [
        "clock",
        "stage",
        "job",
        "transition",
        "elapsed",
        "counters",
        "rate",
        "model",
        "effort",
    ]
    starts = [offsets[name] for name in ordered]
    assert starts == sorted(starts), offsets
    assert len(set(starts)) == len(starts), offsets


def test_the_stage_and_job_cells_hold_the_row_s_own_columns(grid):
    """The clock, stage and job land on their fixed cells whatever the row says.

    The job is the fixed head's third cell — after the clock and the stage — so
    a reader finds it on one screen column rather than after free text of
    varying width.
    """
    line = grid.render(_event())
    assert CLOCK_PATTERN.fullmatch(_cell(line, CLOCK_AT, ticker_module.CLOCK))
    assert _cell(line, STAGE_AT, ticker_module.ROLE).strip() == "implement"
    assert _cell(line, JOB_AT, ticker_module.NODE).strip() == "n-row-order"


def test_the_counters_are_labelled_w_h_d_q(grid):
    """The four counters carry one distinct lower-case letter each.

    The waiting bucket counts the runs the scheduler has admitted and not
    started, so it reads q, its own name's initial, and no two counters share a
    letter or need a case to tell them apart.
    """
    assert ticker_module.STAT_LETTER == {
        "working": "w",
        "halted": "h",
        "delivered": "d",
        "waiting": "q",
    }
    line = plain(grid.render(_event(working=3, blocked=1, unpromoted=7, waiting=2)))
    assert "3w  1h  7d  2q" in line


def test_a_review_shows_the_job_under_review_beside_its_stage(grid):
    """A review's job cell drops the prefix and digest the stage already says.

    The stage cell spells ``review``, so the node id repeats it, and the
    reviewed head's twelve-hex digest identifies nothing a reader can use. The
    cell reads the job under review instead, under the stage that names what
    kind of work it is.
    """
    node = "review-of-sddd-pf-a-00de3452289c"
    line = grid.render(_event(node=node, role="review"))
    assert _cell(line, STAGE_AT, ticker_module.ROLE).strip() == "review"
    assert _cell(line, JOB_AT, ticker_module.NODE).strip() == "sddd-pf-a"
    assert "review-of-" not in plain(line)
    assert "00de3452289c" not in plain(line)


def test_a_jobs_second_run_is_numbered_and_a_launch_keeps_its_number():
    """A job's runs are numbered in draw order, and a launch continues one.

    A lone run has no second row to be told apart from, so it stands bare. Once
    the pane has drawn a second run under one stage and cell text, every later
    row on that key carries its run's number: 1 for the first the pane drew and
    counting up. A run an admission pass launched from a queued request is that
    request continuing, so it keeps the request's number rather than the next.
    """
    grid = ticker_module.Ticker(width=180, color=False, model_aliases=("sonnet5",))
    job_first = _event(node="job-two", run_id="r-num-1", role="implement")
    job_second = _event(node="job-two", run_id="r-num-2", role="implement")

    assert _cell(grid.render(job_first), JOB_AT, ticker_module.NODE).strip() == (
        "job-two"
    )
    assert _cell(grid.render(job_second), JOB_AT, ticker_module.NODE).strip() == (
        "job-two 2"
    )
    # A later row of the first run now carries its own number.
    assert _cell(grid.render(job_first), JOB_AT, ticker_module.NODE).strip() == (
        "job-two 1"
    )

    base = ticker_module.Ticker(width=180, color=False, model_aliases=("sonnet5",))
    queued_first = _event(
        node="job-keep", run_id="r-q-1", role="implement", to_state="queued"
    )
    queued_second = _event(
        node="job-keep", run_id="r-q-2", role="implement", to_state="queued"
    )
    launched = _event(
        node="job-keep",
        run_id="r-c-1",
        role="implement",
        to_state="working",
        admitted_from="r-q-1",
    )
    base.render(queued_first)
    assert _cell(base.render(queued_second), JOB_AT, ticker_module.NODE).strip() == (
        "job-keep 2"
    )
    # The launch names the request it was admitted from, so it keeps number 1.
    assert _cell(base.render(launched), JOB_AT, ticker_module.NODE).strip() == (
        "job-keep 1"
    )


def test_a_lift_row_reads_its_stage_as_lift(grid):
    """A lift row carries no role, so its stage cell names the lift itself."""
    line = grid.render(
        _event(
            run_id="",
            node="budget-group",
            role=None,
            to_state=ticker_module.LIFT_GRANTED,
        )
    )
    assert ticker_module.LIFT_STAGE == "lift"
    assert _cell(line, STAGE_AT, ticker_module.ROLE).strip() == "lift"
    assert _cell(line, JOB_AT, ticker_module.NODE).strip() == "budget-group"


def test_a_narrow_pane_drops_the_effort_then_the_model_then_the_rate(lane_row):
    """A row too narrow for its right end loses effort, then model, then rate.

    The trailing cells are added, left to right, only while the pane has room,
    so the effort goes first and the rate last, and no row exceeds the width it
    was asked for — a wrapped row costs a quarter of the visible history. The
    rate needs a lane document to be present at all, so this test carries the
    lane fixture and keeps the event fixed while only the width moves.
    """
    _, event = lane_row

    def row_at(width: int) -> str:
        narrower = ticker_module.Ticker(
            width=width, color=False, model_aliases=("sonnet5",)
        )
        return plain(narrower.render(event))

    wide = row_at(180)
    assert "tok/s" in wide and "sonnet5" in wide and "medium" in wide

    # Effort dropped, rate and model kept.
    no_effort = row_at(124)
    assert "tok/s" in no_effort and "sonnet5" in no_effort
    assert "medium" not in no_effort

    # Model dropped too, rate kept.
    rate_only = row_at(116)
    assert "tok/s" in rate_only
    assert "sonnet5" not in rate_only and "medium" not in rate_only

    # Rate dropped last: none of the three remains at the floor.
    none_left = row_at(ticker_module.MIN_WIDTH)
    assert "tok/s" not in none_left
    assert "sonnet5" not in none_left and "medium" not in none_left

    for width, line in (
        (180, wide),
        (124, no_effort),
        (116, rate_only),
        (ticker_module.MIN_WIDTH, none_left),
    ):
        assert len(line) <= width, (width, line)