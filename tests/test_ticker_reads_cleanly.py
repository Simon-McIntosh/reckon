"""A ticker row reads the same way whatever it is reporting:

its columns hold still, its counters always show every bucket, its state cell
carries the action and the clause explains without repeating it, and a run that
leaves the fleet is called promoted only when the ledger recorded it.

Four properties are asserted here, each over the whole vocabulary rather than
over a fixture chosen to pass:

* the clause begins at one screen column on every row, a queued run in the fleet
  or not, and no cell abuts the text;
* every classification carrying a remedy renders its action in the state cell,
  and the clause neither opens with a label nor repeats a word the state cell
  says;
* a line's trailing counter includes the transition that line reports;
* a departure is promoted only when a ledger row for the run exists, and a
  refused dispatch is never promoted.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")
LABEL = re.compile(r"^!?\s*\w+:")
MARK = "REASONWORD"

# Every state the renderer knows, so a state added without a thought for the
# clause's column fails here rather than on a pane.
STATES = sorted(ticker_module.STATE_HUE["light"])

# Where the clause begins: the sum of the row's own fixed cells, read from the
# constants the renderer sizes them by rather than from a rendered row, so a
# column added or widened without the clause moving with it fails here. The
# model cell is at its default width because this file's grid pins no aliases.
CLAUSE_COLUMN = (
    ticker_module.CLOCK
    + ticker_module.GAP
    + ticker_module.ROLE
    + ticker_module.GAP
    + ticker_module.NODE
    + ticker_module.STATE_REGION
    + ticker_module.MODEL
    + ticker_module.PAIR_GAP
    + ticker_module.EFFORT
    + ticker_module.STATS
    + ticker_module.SPEND_GAP
    + ticker_module.SPEND
    + ticker_module.GAP
)

# Zero seconds to thirty hours: the span an elapsed cell can be asked to print,
# across the places the format changes shape (minutes, the hour boundary, the
# two-digit hour, and the ceiling the cell claims past ninety-nine hours).
ELAPSED = (0.0, 59.0, 60.0, 3421.0, 3600.0, 5580.0, 108_000.0)


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


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
        "from_state": "working",
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


def test_the_clause_begins_at_one_column_whatever_the_row_reports(grid):
    """One screen column for the clause on every row of the vocabulary.

    The row a reader scans gets ragged when an optional cell appears and the
    cells after it shift, and the class that cost the most was the queued
    counter: a fourth bucket appeared while a run waited and vanished when it
    started, moving every column after it by five. The counter block now holds
    all four buckets at one width always, and the elapsed cell is right-aligned
    to its own, so the clause begins at one column for every state the renderer
    knows, whether a run is queued, and however long the run has been going.

    Every state is read here at every elapsed value and with and without a
    queued run, because a property asserted over one fixture is a property of
    that fixture. The whole fixed region is compared rather than the clause's
    index alone: the clause's column is only the last column of it.
    """
    state_columns: set[int] = set()
    counter_columns: set[int] = set()
    clause_columns: set[int] = set()
    span = ticker_module.SPEND
    end = CLAUSE_COLUMN - ticker_module.GAP
    for state in STATES:
        for seconds in ELAPSED:
            for queued in (False, True):
                event = _event(to_state=state, spend_wall_seconds=seconds)
                event["detail"] = MARK
                if queued:
                    event["waiting"] = 2
                line = plain(grid.render(event))
                key = f"{state}/{seconds}/{queued}"

                # The cells a reader scans line up down the pane: the state
                # word, the right edge of the counter block, and the elapsed
                # token right-aligned to a cell of one width.
                state_columns.add(line.index(state))
                counter_columns.add(re.search(r"\d+q", line).end())
                cell = line[end - span : end]
                assert len(cell) == span, (key, line)
                assert cell.strip() == (
                    "" if state == "dispatched" else ticker_module._elapsed(seconds)
                ), (key, line)

                if MARK in line:
                    # A cell that vanished would let the clause's own cell sit
                    # one space against the last one, which is how
                    # `0mbinvestigate:` read as a single token.
                    assert line[end : end + 2] == "  ", (key, line)
                    # The clause begins in its own cell, past the glyph the
                    # record's own verdict adds when it carries one.
                    assert line.index(MARK) in (end + 2, end + 4), (key, line)
                    clause_columns.add(line.index(MARK))

    assert len(state_columns) == 1, sorted(state_columns)
    assert len(counter_columns) == 1, sorted(counter_columns)
    assert len(clause_columns) <= 2, sorted(clause_columns)


def test_every_counter_bucket_renders_at_one_width(grid):
    """All four buckets print on every row, zeros dimmed rather than dropped."""
    counts = []
    for queued in (False, True):
        event = _event(to_state="working")
        if queued:
            event["waiting"] = 9
        line = plain(grid.render(event))
        block = re.search(r"\d+w· *\d+b· *\d+u· *\d+q", line)
        assert block, line
        counts.append(block.group(0))
    assert {len(block) for block in counts} == {len(counts[0])}, counts
    # The zeros are present, not absent: a fleet with nothing queued still
    # prints the bucket, which is the whole reason the column holds still.
    assert "0q" in counts[0]


@pytest.mark.parametrize("classification", sorted(recovery.RECOVERY_VERBS))
def test_the_state_cell_carries_the_action_and_the_clause_explains_it(
    grid, classification
):
    """Every classification with a remedy: the action in the cell, never in the
    clause.

    The clause opened with the remedy — ``resume: ready to resume: the worker
    process is gone`` — so a reader saw the same word twice before reaching the
    explanation, and the worst case said it twice in one line. The remedy now
    renders in the state cell, and the clause is the explanation alone: it never
    opens with a label and never begins with a word the cell already says.
    """
    action = recovery.RECOVERY_VERBS[classification]
    event = _event(
        to_state=classification,
        recovery_classification=classification,
        recovery=action,
        detail=f"{action}: ready to {action}: the worker process is gone",
    )
    line = plain(grid.render(event))

    assert action in line, line
    # The action is in the state cell, ahead of the clause's own column.
    assert line.index(action) < CLAUSE_COLUMN, line
    body = line[CLAUSE_COLUMN:].strip()
    assert not LABEL.match(body), body
    cell_words = set(re.findall(r"[A-Za-z0-9_-]+", line[:CLAUSE_COLUMN]))
    if body:
        first = re.findall(r"[A-Za-z0-9_-]+", body)[0]
        assert first not in cell_words, (first, cell_words, body)


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
    assert "1b" in line, line


def grid_row(snapshot, counts):
    """The row a transition renders as, through the pane the fold feeds."""
    event = {**snapshot, **counts, "to_state": "blocked"}
    return ticker_module.Ticker(
        width=180, theme="light", color=False, model_aliases=()
    ).render(event)


@pytest.mark.parametrize("ledger_row", [True, False])
def test_a_departure_is_promoted_only_with_a_ledger_row(grid, ledger_row):
    """The word a departure renders as is the fact the ledger holds.

    A run that leaves the live set with no ledger row behind it vanished — a
    refused dispatch, a discarded run, a reflex review whose worker never
    started — and calling that `promoted` tells a coordinator a review exists
    that it will then wait on. The ledger row is what separates the two.
    """
    event = _event(to_state="promoted", ledger_row=ledger_row)
    line = plain(grid.render(event))
    if ledger_row:
        assert "promoted" in line
        assert "withdrawn" not in line
    else:
        assert "withdrawn" in line
        assert "promoted" not in line


def test_a_refused_dispatch_is_never_promoted(grid):
    """A refusal is a departure whose cause is known: nothing ran to promote."""
    event = _event(to_state="promoted", refused=True, detail="refused at admission")
    assert "withdrawn" in plain(grid.render(event))
    assert ticker_module.departure_word({"event": "refused"}) == "withdrawn"
    assert ticker_module.departure_word({"ledger_row": False}) == "withdrawn"
    assert ticker_module.departure_word({"ledger_row": True}) == "promoted"
