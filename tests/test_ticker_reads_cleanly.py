"""A ticker row reads the same way whatever it is reporting:

its fixed cells hold still, its counters always show every bucket, its
transition contains no action word, and a producer's destination state is
rendered as given.

Four properties are asserted here, each over the whole vocabulary rather than
over a fixture chosen to pass:

* the state word, the arrow and the counter block hold one screen column on
  every row, a queued run in the fleet or not;
* every classification carrying a remedy renders only the transition before
  elapsed, with no attention mark anywhere on the row;
* a line's trailing counter includes the transition that line reports;
* a departure word is never re-derived from a ledger side fact.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")
LABEL = re.compile(r"^!?\s*\w+:")

# Every state the renderer knows, so a state added without a thought for the
# fixed frame fails here rather than on a pane.
STATES = sorted(ticker_module.STATE_HUE["light"])

# Where each fixed cell begins: the sum of the row's own cells ahead of it, read
# from the constants the renderer sizes them by rather than from a rendered row,
# so a cell added or widened without the cells after it moving fails here. The
# model and effort come after the lane's rate at the row's right end and are not
# part of the fixed frame this file locates. The model cell is at its default
# width because this file's grid pins no aliases.
STAGE_COLUMN = ticker_module.CLOCK + ticker_module.GAP
JOB_COLUMN = STAGE_COLUMN + ticker_module.ROLE + ticker_module.GAP
TRANSITION_COLUMN = JOB_COLUMN + ticker_module.NODE + ticker_module.GAP
NEW_STATE_COLUMN = TRANSITION_COLUMN + ticker_module.NEW_STATE_OFFSET
ELAPSED_COLUMN = (
    TRANSITION_COLUMN
    + ticker_module.STATE
    + ticker_module.GAP
    + ticker_module.SPEND_GAP
)
COUNTER_COLUMN = ELAPSED_COLUMN + ticker_module.SPEND + ticker_module.GAP
TRAILING_COLUMN = COUNTER_COLUMN + ticker_module.STATS + ticker_module.GAP

# Zero seconds to thirty hours: the span an elapsed cell can be asked to print,
# across the places the format changes shape (minutes, the hour boundary, the
# two-digit hour, and the ceiling the cell claims past ninety-nine hours).
ELAPSED = (0.0, 59.0, 60.0, 3421.0, 3600.0, 5580.0, 108_000.0)


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


def ticker_state(state: str) -> str:
    """The state cell's rendered form: the display word, elided to the cell."""
    return ticker_module._display_state(state)


@pytest.fixture
def grid():
    """A grid whose model cell is pinned, so the row's columns are its own."""
    return ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())


def _event(**overrides):
    event = {
        "observed_at": "2026-09-25T09:00:00+00:00",
        "run_id": "r-reads-cleanly",
        "node": "n-reads-cleanly",
        "role": "implement",
        "from_state": None,
        "to_state": "working",
        "working": 3,
        "blocked": 1,
        "unpromoted": 2,
        "model": "dsv4.1-flash",
        "effort": "medium",
        "spend_wall_seconds": 3421.0,
    }
    event.update(overrides)
    return event


def test_the_fixed_cells_hold_one_column_whatever_the_row_reports(grid):
    """One screen column for each fixed cell on every row of the vocabulary.

    The row a reader scans gets ragged when an optional cell appears and the
    cells after it shift, and the class that cost the most was the queued
    counter: a fourth bucket appeared while a run waited and vanished when it
    started, moving every column after it by five. The counter block now holds
    all four buckets at one width always, and the elapsed cell is right-aligned
    to its own, so the state word, the arrow and the counter block hold one
    column for every state the renderer knows, whether a run is queued, and
    however long the run has been going.

    Every state is read here at every elapsed value and with and without a queued
    run, because a property asserted over one fixture is a property of that
    fixture. The whole fixed region is compared rather than one cell's index
    alone: the counter's column is only the last of them.
    """
    state_columns: set[int] = set()
    counter_columns: set[int] = set()
    arrow_columns: set[int] = set()
    span = ticker_module.SPEND
    end = ELAPSED_COLUMN + ticker_module.SPEND
    for state in STATES:
        for seconds in ELAPSED:
            for queued in (False, True):
                event = _event(to_state=state, spend_wall_seconds=seconds)
                if queued:
                    event["waiting"] = 2
                line = plain(grid.render(event))
                key = f"{state}/{seconds}/{queued}"

                # The cells a reader scans line up down the pane: the state
                # word, the arrow with its two-state transition, the right edge
                # of the counter block, and the elapsed token right-aligned to a
                # cell of one width. The state cell renders the display form the
                # renderer produces, so a word wider than the cell is compared as
                # the elided form the row carries rather than as the word the
                # classifier emitted — a state is elided to the cell, never
                # allowed to widen it and push the arrow off its column.
                rendered_state = ticker_state(state)
                state_cell = line[
                    NEW_STATE_COLUMN : NEW_STATE_COLUMN + ticker_module.STATE_WORD
                ].strip()
                assert state_cell == rendered_state, (key, line)
                state_columns.add(NEW_STATE_COLUMN)
                arrow_columns.add(line.index(ticker_module.ARROW))
                counter_columns.add(re.search(r"\d+q", line).end())
                cell = line[end - span : end]
                assert len(cell) == span, (key, line)
                assert cell.strip() == (
                    "" if state == "dispatched" else ticker_module._elapsed(seconds)
                ), (key, line)

    assert len(state_columns) == 1, sorted(state_columns)
    assert len(counter_columns) == 1, sorted(counter_columns)
    # The arrow holds one column whatever state the row entered, the long
    # recovery spellings included: each half is exactly the elided state word,
    # so no word can widen its cell and push the arrow.
    assert len(arrow_columns) == 1, sorted(arrow_columns)


def test_a_long_state_word_keeps_the_arrow_on_one_column(grid):
    """A state wider than the cell elides into it; the arrow never moves.

    The three classifier words longer than the ten-column cell —
    ended-without-manifest, completed_unpromoted and refused-at-admission —
    must render their head inside the cell so the arrow sits on the same screen
    column as it does for a short state. A word rendered at full length would
    widen its half and push the arrow right, which is what a reader scanning the
    arrow column down the pane would see as ragged.
    """
    short = plain(grid.render(_event(to_state="blocked")))
    arrow = short.index(ticker_module.ARROW)
    long_states = (
        "ended-without-manifest",
        "completed_unpromoted",
        "refused-at-admission",
    )
    for state in long_states:
        for side in ("to_state", "from_state"):
            line = plain(grid.render(_event(**{side: state})))
            assert line.index(ticker_module.ARROW) == arrow, (state, side, line)
            start = NEW_STATE_COLUMN if side == "to_state" else TRANSITION_COLUMN
            cell = line[start : start + ticker_module.STATE_WORD].strip()
            assert cell == ticker_module._display_state(state), (state, side, line)
            assert cell.strip("…") != "", (state, side, line)


def test_every_counter_bucket_renders_at_one_width(grid):
    """All four buckets print on every row, zeros dimmed rather than dropped."""
    counts = []
    for queued in (False, True):
        event = _event(to_state="working")
        if queued:
            event["waiting"] = 9
        line = plain(grid.render(event))
        block = re.search(r"\d+w *\d+h *\d+d *\d+q", line)
        assert block, line
        counts.append(block.group(0))
    assert {len(block) for block in counts} == {len(counts[0])}, counts
    # The zeros are present, not absent: a fleet with nothing queued still
    # prints the bucket, which is the whole reason the column holds still.
    assert "0q" in counts[0]


@pytest.mark.parametrize("classification", sorted(recovery.RECOVERY_VERBS))
def test_the_transition_prints_no_action_and_no_attention_mark(grid, classification):
    """Every classified remedy stays data; the row prints neither verb nor mark."""
    action = recovery.RECOVERY_VERBS[classification]
    event = _event(
        to_state=classification,
        recovery_classification=classification,
        recovery=action,
        detail=f"{action}: ready to {action}: the worker process is gone",
    )
    line = plain(grid.render(event))

    transition_words = set(
        re.findall(r"[A-Za-z0-9_-]+", line[TRANSITION_COLUMN:ELAPSED_COLUMN])
    )
    assert action not in transition_words, line
    # No attention column can reach here, whatever the destination: an attention
    # mark tried by an earlier revision was dropped at the lead's direction. The
    # stage cell follows the time's two-column gutter directly, so nothing can
    # occupy a reserved column between them.
    assert "!" not in line, line
    gutter = line[ticker_module.CLOCK : ticker_module.CLOCK + ticker_module.GAP]
    assert gutter == " " * ticker_module.GAP, line
    assert line[ticker_module.CLOCK + ticker_module.GAP] != " ", line
    # Nothing after the counters carries a composed `action:` label: the row
    # holds only data, never a producer's verdict word.
    body = line[TRAILING_COLUMN:].strip()
    assert not LABEL.match(body), body


def test_a_line_counter_includes_the_transition_it_reports():
    """The number on the line is the fleet at that line, its own change included.

    The counters were stamped batch-wide at first, so a line reported the fleet
    it had already left: a promotion printed the totals it moved out of, and
    three simultaneous landings all claimed the third one's numbers. The fold
    recomputes after each change is applied, so the run arriving on a line is in
    the number that line prints.
    """
    known = {
        "r-1": {"run_id": "r-1", "node": "n-1", "state": "working"},
        "r-2": {"run_id": "r-2", "node": "n-2", "state": "working"},
    }
    current = {
        "r-1": {"run_id": "r-1", "node": "n-1", "state": "working"},
        # r-2 stops and needs the coordinator, so the line announcing it must
        # carry it in the blocked count.
        "r-2": {"run_id": "r-2", "node": "n-2", "state": "blocked"},
    }
    events, _running = recovery.fleet_transitions(known, current)
    changes = [event for event in events if event[2] == "blocked"]
    assert len(changes) == 1, events

    snapshot, _previous, _state, counts = changes[0]
    assert counts["blocked"] == 1, counts
    # And the rendered row prints that number, so the line and the fleet agree.
    line = plain(grid_row(snapshot, counts))
    assert "1h" in line, line


def grid_row(snapshot, counts):
    """The row a transition renders as, through the pane the fold feeds."""
    event = {**snapshot, **counts, "to_state": "blocked"}
    return ticker_module.Ticker(
        width=180, theme="light", color=False, model_aliases=()
    ).render(event)


@pytest.mark.parametrize("ledger_row", [True, False])
def test_a_departure_renders_the_producer_state_without_a_ledger_branch(
    grid, ledger_row
):
    """A renderer does not restate a producer verdict from a ledger side fact."""
    event = _event(to_state="promoted", ledger_row=ledger_row)
    line = plain(grid.render(event))
    assert "landed" in line
    assert "dropped" not in line


def test_a_withdrawn_producer_state_is_rendered_as_given(grid):
    event = _event(to_state="withdrawn", refused=True, detail="refused at admission")
    assert "dropped" in plain(grid.render(event))
