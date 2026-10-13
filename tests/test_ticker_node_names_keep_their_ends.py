"""A node name keeps both its ends, and two rows that read alike are numbered.

The node column is read down a pane to answer *which worker is this?*, and the
peers of one wave share a long head: a name cut at its right renders every peer
of the wave as the same string, which is a row attributed to the wrong worker.
So a name wider than its cell is cut from the middle, keeping its head and its
end. Two rows can still land on one text — two names that differ only where the
cut falls, and two runs of one node, which share a name outright. Once a wave
numbers two runs under one stage and cell text, each row carries its own run's
number, 1 for the first the pane drew and counting up, so the runs read apart
and in order. A lone run carries no number, because it has no second row to be
told apart from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from reckon.crew import ticker as ticker_module
from reckon.crew.runs import new_run_id

# Two reviews of similarly named nodes: peers of one wave, differing exactly
# where a right-hand cut would throw the difference away.
BANKED_SEAM = "review-of-sgc-sweep-control-at-the-banked-seam"
FOUR_OF_TWENTY = "review-of-sgc-sweep-control-at-the-four-of-twenty"

# Two names a middle cut still renders as one text: they agree at both ends and
# differ only where the cut falls.
REPAIR = "review-of-sgc-sweep-repair-at-the-banked-seam"
CENSUS = "review-of-sgc-sweep-census-at-the-banked-seam"

# Dispatch instants handed to the fleet's own id mint, so the cases run against
# the ids production produces rather than against a fixture whose ends happen to
# differ. The stamps' last four digits are 6862 and 0578.
FIRST_MINT = datetime(2026, 9, 25, 1, 16, 29, 6862, tzinfo=UTC)
SECOND_MINT = datetime(2026, 9, 25, 2, 41, 4, 740578, tzinfo=UTC)
REPAIR_ID = new_run_id(REPAIR, now=FIRST_MINT)
CENSUS_ID = new_run_id(CENSUS, now=SECOND_MINT)


def _event(node: str, run_id: str) -> dict[str, Any]:
    return {
        "observed_at": "2026-09-25T01:16:29Z",
        "run_id": run_id,
        "node": node,
        "to_state": "working",
    }


def _node_cell(grid: ticker_module.Ticker, line: str) -> str:
    """The node cell's text, read off the rendered row at the grid's columns.

    Taken from the row's own characters rather than from a format string, so
    the assertion is about the cell a reader sees. The node is the fixed head's
    third cell — after the clock and the stage — so its column does not move
    with the trailing rate, model and effort cells at the row's right end.
    """
    start = (
        ticker_module.CLOCK
        + ticker_module.GAP
        + ticker_module.ROLE
        + ticker_module.GAP
    )
    return line[start : start + ticker_module.NODE]


def _render(grid: ticker_module.Ticker, node: str, run_id: str) -> str:
    return _node_cell(grid, grid.render(_event(node, run_id)))


def test_two_long_names_render_apart_at_the_default_width() -> None:
    """The difference at the end of a name survives the cut, so peers read apart."""
    grid = ticker_module.Ticker(color=False)
    seam = _render(grid, BANKED_SEAM, f"r-20260925T011629068862-{BANKED_SEAM}")
    twenty = _render(grid, FOUR_OF_TWENTY, f"r-20260925T011629068863-{FOUR_OF_TWENTY}")

    assert seam != twenty
    assert BANKED_SEAM not in seam
    assert FOUR_OF_TWENTY not in twenty
    assert "…" in seam and "…" in twenty
    assert seam.startswith(BANKED_SEAM[:10])
    assert twenty.startswith(FOUR_OF_TWENTY[:10])
    assert seam.rstrip().endswith("banked-seam")
    assert twenty.rstrip().endswith("four-of-twenty")


def test_a_name_that_fits_its_cell_is_unchanged() -> None:
    grid = ticker_module.Ticker(color=False)
    name = "fleet-view-unseen-surfaces"
    cell = _render(grid, name, f"r-20260925T011629068862-{name}")

    assert cell.rstrip() == name
    assert "…" not in cell


def test_two_names_that_cut_alike_are_numbered_in_draw_order() -> None:
    """A row is the only place two names cut to one text can be told apart.

    The collision is observable only once the second run has been drawn, and a
    row already written to the pane cannot be recalled: the first render stands
    bare, and every later render under that key carries its own run's number, 1
    for the first the pane drew and counting up. The number never widens the
    cell, so the columns after the node cell stay where they were.
    """
    assert ticker_module.elide(REPAIR, ticker_module.NODE, keep_end=True) == (
        ticker_module.elide(CENSUS, ticker_module.NODE, keep_end=True)
    )

    grid = ticker_module.Ticker(color=False)
    first = _render(grid, REPAIR, REPAIR_ID)
    second = _render(grid, CENSUS, CENSUS_ID)
    again = _render(grid, REPAIR, REPAIR_ID)

    assert first.rstrip() != second.rstrip()
    assert first.rstrip() == ticker_module.elide(
        REPAIR, ticker_module.NODE, keep_end=True
    )
    assert again.rstrip().endswith(" 1")
    assert second.rstrip().endswith(" 2")
    assert len(grid.render(_event(CENSUS, CENSUS_ID))) <= ticker_module.DEFAULT_WIDTH


def test_two_runs_of_one_node_show_different_cells() -> None:
    """One node dispatched twice is two rows a reader has to tell apart.

    Both runs render the same name, so the cell is identical until each carries
    its own number.
    """
    second_run = new_run_id(REPAIR, now=SECOND_MINT)

    grid = ticker_module.Ticker(color=False)
    first = _render(grid, REPAIR, REPAIR_ID)
    second = _render(grid, REPAIR, second_run)
    again = _render(grid, REPAIR, REPAIR_ID)

    assert first.rstrip() != second.rstrip()
    assert again.rstrip().endswith(" 1")
    assert second.rstrip().endswith(" 2")
    assert len(grid.render(_event(REPAIR, REPAIR_ID))) <= ticker_module.DEFAULT_WIDTH


def test_a_lone_long_name_carries_no_number() -> None:
    """One run's row has no second row to be told apart from."""
    grid = ticker_module.Ticker(color=False)
    cell = _render(grid, CENSUS, CENSUS_ID)

    assert cell.rstrip() == ticker_module.elide(
        CENSUS, ticker_module.NODE, keep_end=True
    )
    assert "…" in cell
