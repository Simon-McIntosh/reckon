"""A node name keeps both its ends, and two names that read alike name their runs.

The node column is read down a pane to answer *which worker is this?*, and the
peers of one wave share a long head: a name cut at its right renders every peer
of the wave as the same string, which is a row attributed to the wrong worker.
So a name wider than its cell is cut from the middle, keeping its head and its
end. Two names can still land on one text when they differ in the middle, and
then each row carries the last four characters of its own run id, inside the
cell, so the figure a reader checks is on the row itself.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker as ticker_module

# Two reviews of similarly named nodes: peers of one wave, differing exactly
# where a right-hand cut would throw the difference away.
BANKED_SEAM = "review-of-sgc-sweep-control-at-the-banked-seam"
FOUR_OF_TWENTY = "review-of-sgc-sweep-control-at-the-four-of-twenty"

# Two names a middle cut still renders as one text: they agree at both ends and
# differ only where the cut falls. Their run ids are the fleet's own shape,
# minted a second apart, and their last four characters differ so the suffix a
# row carries is checkable on the row.
REPAIR = "review-of-sgc-sweep-repair-at-the-banked-seam"
CENSUS = "review-of-sgc-sweep-census-at-the-banked-seam"
REPAIR_ID = "r-20260925T011629068862-repair"
CENSUS_ID = "r-20260925T011629068863-census"


def _event(node: str, run_id: str) -> dict[str, Any]:
    return {
        "observed_at": "2026-09-25T01:16:29Z",
        "run_id": run_id,
        "node": node,
        "to_state": "working",
    }


def _node_cell(line: str) -> str:
    """The node cell's text, read off the rendered row at the grid's columns.

    Taken from the row's own characters rather than from a format string, so
    the assertion is about the cell a reader sees.
    """
    start = (
        ticker_module.CLOCK + ticker_module.GAP + ticker_module.ROLE + ticker_module.GAP
    )
    return line[start : start + ticker_module.NODE]


def _render(grid: ticker_module.Ticker, node: str, run_id: str) -> str:
    return _node_cell(grid.render(_event(node, run_id)))


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


def test_two_names_that_cut_alike_each_carry_their_own_run_id() -> None:
    """A row is the only place two names cut to one text can be told apart.

    The collision is observable only once the second name has been seen: a row
    is rendered as its event reaches the grid, so the first row carries its
    suffix from the render at which the collision is known, and the claim is
    remembered rather than applied once. The suffix never widens the cell, so
    the columns after the node cell stay where they were.
    """
    assert ticker_module.elide(
        REPAIR, ticker_module.NODE, keep_end=True
    ) == ticker_module.elide(CENSUS, ticker_module.NODE, keep_end=True)

    grid = ticker_module.Ticker(color=False)
    first = _render(grid, REPAIR, REPAIR_ID)
    second = _render(grid, CENSUS, CENSUS_ID)
    again = _render(grid, REPAIR, REPAIR_ID)

    assert second.rstrip().endswith(CENSUS_ID[-4:])
    assert again.rstrip().endswith(REPAIR_ID[-4:])
    assert first.rstrip() != second.rstrip()
    assert len(grid.render(_event(CENSUS, CENSUS_ID))) == ticker_module.DEFAULT_WIDTH
