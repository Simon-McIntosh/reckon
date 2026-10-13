"""The fleet counter's halted bucket counts a launch failure with every stop.

A run whose launcher exited before writing any stream record never reached a
model, so it stops under the halted bucket like any other stop. The counter
block keeps four cells, so that run's number arrives under the halted bucket
without widening the block. These cases read the fleet the way a follower does,
through the producer's own fleet partition and transition builder, so the counts
on each row are the counts a pane receives.
"""

from __future__ import annotations

import re
from typing import Any

from reckon.crew import ticker as ticker_module
from reckon.crew.recovery import _fleet_counts, _watch_transition
from reckon.crew.ticker import STATS

WIDTH = 180

# The four-cell block a reading holding one launch-failed run, one ordinary
# blocked run, one working run and one queued run renders, pinned as a literal
# because the requirement is that this block keeps exactly the width it renders
# today: a fifth cell or a wider label fails on the literal. Each cell is two
# right-aligned digits and its letter, one space between cells; a count above
# two digits widens its own cell, which is why the counts here stay single.
BLOCK = " 1w  2h  0d  1q"
BLOCK_WIDTH = 15


def _snapshot(
    run_id: str,
    node: str,
    state: str,
    *,
    detail: str = "",
    recovery: str = "",
) -> dict[str, Any]:
    """One live pointer reduced to the facts the partition and the row read."""
    return {
        "run_id": run_id,
        "node": node,
        "state": state,
        "detail": detail,
        "recovery_classification": state,
        "recovery": recovery,
        "role": "implement",
        "model": "dsv4-flash",
        "effort": "high",
        "alias": "",
        "session": "",
    }


def _reading(*snapshots: dict[str, Any]) -> list[dict[str, Any]]:
    """Every run's row, each stamped with the counts the partition derives.

    This is the producer's own path, so an assertion about a row's number is an
    assertion about what a follower emits rather than about a fixture.
    """
    counts = _fleet_counts({snapshot["run_id"]: snapshot for snapshot in snapshots})
    return [
        _watch_transition(
            "proj",
            kind="baseline",
            snapshot=snapshot,
            previous=None,
            current=snapshot["state"],
            counts=counts,
            spend_runs=[],
            rate_statuses={},
        )
        for snapshot in snapshots
    ]


def _rows(reading: list[dict[str, Any]]) -> dict[str, str]:
    """Render the reading, keyed by the run each row describes."""
    grid = ticker_module.Ticker(width=WIDTH)
    return {str(row["run_id"]): grid.render(row) for row in reading}


def _launch_failed_and_blocked() -> dict[str, str]:
    """A reading holding one launch-failed run and one ordinary blocked run."""
    return _rows(
        _reading(
            _snapshot("r-launch", "north", "launch-failed", recovery="resume"),
            _snapshot("r-blocked", "south", "blocked", recovery="decide"),
            _snapshot("r-work", "east", "working"),
            _snapshot("r-queued", "west", "waiting", recovery="wait"),
        )
    )


def test_the_counter_block_keeps_four_cells_and_its_width() -> None:
    """A launch failure's number does not widen the counter block."""
    rows = _launch_failed_and_blocked()
    launch = rows["r-launch"]

    # Four cells and three single-space gaps, at the width the module states
    # for the block: the launch failure's number did not widen the block.
    assert re.findall(r"\d[whdq]", launch) == ["1w", "2h", "0d", "1q"]
    assert BLOCK in launch
    assert launch.count("·") == 0
    assert len(BLOCK) == BLOCK_WIDTH == STATS

    # Every row stays within the pane's width, so nothing wrapped to make room.
    assert all(len(row) <= WIDTH for row in rows.values())


def test_the_launch_failure_is_still_counted_under_the_halted_bucket() -> None:
    """A launch failure contributes to the pane's halted count."""
    assert ticker_module._bucket_of("launch-failed", "launch-failed") == "halted"
