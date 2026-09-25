"""A reason clause too long for its room is cut at a word, never through one.

The clause is the row's payload: a reader acts on the words it keeps, and a cut
inside a word leaves a fragment that reads as a typo rather than as a cut. So a
clause wider than the field the grid leaves it loses its tail at the last word
boundary that fits, marked with an ellipsis, and a clause that fits renders
verbatim. The row is a rendering of the transition record rather than its store,
so a cut row leaves the record — and the JSON the follower prints of it —
carrying the clause whole.
"""

from __future__ import annotations

import json
from typing import Any

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

# Forty words, as the measure names, and of varied length on purpose: a cut that
# counts characters rather than words keeps the fragment "command" is broken
# into, so the whole-word assertion is what discriminates the cut.
FORTY_WORDS = (
    "the same focused pytest command failed twice after different fixes and "
    "the worker stopped rather than guess at a third repair because the "
    "failure sits outside the files this node was given to change so it "
    "wants a scope decision"
)

# A clause whose first word is wide enough that no boundary falls in the second
# half of a thirty-column room, so the cut must choose between keeping that whole
# word and keeping a fragment of the long token after it.
LONG_HEAD = (
    "unpromoted review-of-sgc-sweep-control-at-the-banked-seam has sat since "
    "the wave closed"
)

FITS = "pytest exited 1"

# The room the measure renders into.
ROOM = 30


def _ticker(width: int) -> ticker_module.Ticker:
    return ticker_module.Ticker(width=width, color=False)


def _reason_start(model_width: int) -> int:
    """The column the reason cell starts on, from the grid's own geometry.

    Every cell ahead of the clause is fixed-width, so the clause's room is the
    requested width minus these columns. Derived from the module's own constants
    rather than pinned to a screen column, so a layout change moves this with
    the row it measures.
    """
    start = (
        ticker_module.CLOCK
        + ticker_module.GAP
        + ticker_module.ROLE
        + ticker_module.GAP
        + ticker_module.NODE
        + ticker_module.STATE_REGION
        + model_width
        + ticker_module.PAIR_GAP
        + ticker_module.EFFORT
    )
    start += sum(3 for _ in ticker_module._CELLS) + (len(ticker_module._CELLS) - 1)
    return start + ticker_module.SPEND_GAP + ticker_module.WALL + ticker_module.GAP


def _event(clause: str, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "observed_at": "2026-09-25T04:38:00Z",
        "run_id": "r-20260925T043800000000-reason-cell",
        "node": "n-reason-cell",
        "role": "implement",
        "from_state": "working",
        "to_state": "blocked",
        "working": 1,
        "blocked": 1,
        "unpromoted": 0,
        "detail": clause,
    }
    event.update(overrides)
    return event


def _reason_cell(clause: str, *, room: int = ROOM) -> str:
    """The reason cell of a row rendered into exactly ``room`` columns.

    Read off the row's own characters — the cell runs from where the fixed
    columns end to the row's last one — and the room is asserted rather than
    assumed, so a layout change cannot move the measure silently.
    """
    model_width = _ticker(0).model_width
    start = _reason_start(model_width)
    row = _ticker(start + room).render(_event(clause))
    assert row.index(clause.split(maxsplit=1)[0]) == start, row
    assert len(row) - start == room, (len(row), start)
    return row[start:]


def _kept(cell: str) -> str:
    """The words a cut cell keeps, with the padding and the ellipsis removed."""
    text = cell.rstrip()
    assert text.endswith("…"), cell
    return text[:-1].rstrip()


def test_a_forty_word_clause_into_thirty_columns_ends_in_a_whole_word() -> None:
    """The room is thirty columns, and what it keeps is whole words."""
    assert len(FORTY_WORDS.split()) == 40
    cell = _reason_cell(FORTY_WORDS)
    kept = _kept(cell)
    assert kept
    # The cut is a boundary of the clause itself: what follows the kept text is
    # the space the ellipsis replaces, so no word was broken.
    assert FORTY_WORDS.startswith(kept + " ")
    assert kept.split()[-1] in FORTY_WORDS.split()
    # And it is the last boundary that fits: no later one falls inside the room.
    assert " " not in FORTY_WORDS[len(kept) + 1 : ROOM]
    assert len(cell.rstrip()) <= ROOM


def test_a_clause_that_fits_its_room_is_unchanged() -> None:
    """A short clause renders as written, with no cut and no mark of one."""
    cell = _reason_cell(FITS)
    assert "…" not in cell
    assert cell.rstrip() == FITS


def test_a_clause_whose_first_word_fills_the_room_keeps_that_word() -> None:
    """A long token after a short word does not drag the cut inside itself.

    The clause's own first word is where its sense starts, so the cut stops
    after the last whole word that fits the room rather than running into the
    token past it — the room is left short, and what it holds is readable.
    """
    cell = _reason_cell(LONG_HEAD)
    kept = _kept(cell)
    assert kept == "unpromoted"
    assert LONG_HEAD.startswith(kept + " ")
    assert len(cell.rstrip()) < ROOM


def test_the_transition_record_and_its_json_keep_the_clause_whole() -> None:
    """The record is the store and the row is a rendering of it.

    The follower's JSON output prints the transition object itself, so the
    machine-readable stream carries every word of the clause even where the row
    had room for its head alone.
    """
    transition = recovery._watch_transition(
        "reckon",
        kind="transition",
        snapshot={
            "run_id": "r-20260925T043800000000-reason-cell",
            "node": "n-reason-cell",
            "role": "implement",
            "to_state": "blocked",
            "detail": FORTY_WORDS,
        },
        previous="working",
        current="blocked",
        counts={"working": 1, "blocked": 1, "unpromoted": 0},
        spend_runs=[],
    )
    assert transition["detail"] == FORTY_WORDS
    assert FORTY_WORDS in json.dumps(transition)

    # The row rendered from that same record is the cut one, and its cut is a
    # word boundary: the record and the row disagree in length, never in words.
    model_width = _ticker(0).model_width
    line = recovery.format_watch_transition(
        transition, ticker=_ticker(_reason_start(model_width) + ROOM)
    )
    kept = _kept(line[line.index(FORTY_WORDS.split(maxsplit=1)[0]) :])
    assert FORTY_WORDS.startswith(kept + " ")
    assert kept != FORTY_WORDS
