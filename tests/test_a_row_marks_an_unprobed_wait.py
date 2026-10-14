"""A wait no probe has run for is marked, so it reads apart from one being tested.

A run can declare an external condition that no probe has ever read — because its
probe never launched, or cannot read the condition. Its row must not look like a
wait whose condition is still being tested: nothing is advancing the first, so a
reader waiting for it waits for nothing. The record separates the two — the
probe's verdict is absent when no probe has run — and the row does too, marking
the unprobed wait with a glyph beside its destination word. The glyph rides in
the state cell's spare column, so a marked row and an unmarked one align and no
later cell shifts.
"""

from __future__ import annotations

import re

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The fleet counter block, wherever it sits on the row, located by its own shape
# so the cell after the state word can be compared between two rows.
COUNTERS = re.compile(r"(\s?\d{1,2})w( \s?\d{1,2})h( \s?\d{1,2})d")


def plain(line: str) -> str:
    """The line as the grid measures it, with any colour removed."""
    return ESCAPES.sub("", line)


def _event(**overrides):
    event = {
        "observed_at": "2026-10-14T12:16:13+00:00",
        "run_id": "r-1",
        "node": "n-unprobed-wait",
        "session": "ship-s26",
        "role": "implement",
        "from_state": "dispatched",
        "to_state": "waiting",
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 0,
    }
    event.update(overrides)
    return event


def _grid():
    """A grid whose model cell is pinned, so the row's columns are its own."""
    return ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())


def test_an_unprobed_wait_is_marked_beside_its_destination_word():
    """A declared wait with no probe verdict draws the marker after the word.

    The manifest asked to wait and emitted its horizon and overdue flag, and no
    probe has read the condition, so the verdict is absent — the situation the
    marker names. The row must show it rather than read as an ordinary wait.
    """
    declared = plain(
        _grid().render(_event(wait_overdue=False, expected_horizon_seconds=300.0))
    )
    probed = plain(_grid().render(_event(wait_condition_state="pending")))

    assert "waiting?" in declared
    # The word itself is still the wait's word, with the glyph beside it.
    assert "waiting" in declared
    # A wait whose probe has reported is being tested, so it is not marked.
    assert "waiting?" not in probed
    assert "waiting" in probed


def test_a_marked_wait_keeps_every_later_cell_on_its_column():
    """The glyph spends a spare column, not a cell, so no later cell shifts.

    The state cell is a fixed ten columns and the longest pane word is nine, so
    the marker rides inside the word's own field: a marked row and an unmarked
    one are the same width and every cell after the state word lands on the same
    screen column. A reader scanning the counters (or anything after them) finds
    the column wherever it was.
    """
    declared = plain(
        _grid().render(_event(wait_overdue=False, expected_horizon_seconds=300.0))
    )
    probed = plain(_grid().render(_event(wait_condition_state="pending")))

    assert len(declared) == len(probed)
    assert declared.index("waiting") == probed.index("waiting")
    assert declared.index("→") == probed.index("→")
    assert COUNTERS.search(declared).start() == COUNTERS.search(probed).start()


def test_a_row_declaring_no_wait_carries_no_marker():
    """An ordinary row carries none of the declaration's facts, so no marker.

    The marker is derived from the declaration alone, never from the state word,
    so a row with no horizon, no overdue flag and no verdict is unmarked.
    """
    line = plain(_grid().render(_event()))

    assert "waiting" in line
    assert "waiting?" not in line
    assert ticker_module.UNPROBED_WAIT_MARKER not in line


def test_the_marker_follows_the_word_through_the_display_mapping():
    """An overdue wait displays as `waiting`; the glyph rides the displayed word.

    The internal `wait-aged` classification keeps the pane word `waiting`, so a
    marked row's glyph must sit beside that word rather than the internal one.
    """
    line = plain(
        _grid().render(
            _event(
                to_state="wait-aged",
                recovery_classification="wait-aged",
                wait_overdue=True,
            )
        )
    )

    assert "waiting?" in line
    assert "wait-aged?" not in line
