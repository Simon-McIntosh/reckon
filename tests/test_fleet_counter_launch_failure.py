"""The fleet counter's blocked bucket says when its number is an infrastructure fault.

A run whose launcher exited before writing any stream record never reached a
model, so it is an infrastructure fault rather than a worker turn wanting a
decision. The counter block keeps four cells, so that run's number arrives under
the blocked bucket, which leaves the row's reason clause as the only place the
line can say what kind of stop it is. These cases read the fleet the way a
follower does, through the producer's own fleet partition and transition
builder, so the counts on each row are the counts a pane receives.
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
BLOCK = " 1w  2b  0u  1q"
BLOCK_WIDTH = 15

# The same block for a reading with no launch-failed run in it.
CONTROL_BLOCK = " 1w  1b  0u  1q"

# A verbatim classifier cause for a backend that was not on the path, and an
# ordinary blocked worker's own reason: the launch cause names the launcher as
# the thing that failed, the block reason names a decision.
LAUNCH_DETAIL = (
    "the launch for backend 'claude' exited with status 127 before writing any "
    "stream record (/bin/bash: line 1: claude: command not found); "
    "1 launch failure recorded; no model was reached"
)
BLOCK_DETAIL = (
    "the worker manifest reports blocked: the gate needs a coordinator decision"
)

# What a launch-failed row renders when its record carries no cause of its own.
# The clause names the fault itself rather than leaving a blocked number
# explained by nothing; the case asserts the leading words, which survive the
# elision a pane's margin applies.
FAULT_CLAUSE_PREFIX = "the launch failed before any turn"


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
            _snapshot(
                "r-launch",
                "north",
                "launch-failed",
                detail=LAUNCH_DETAIL,
                recovery="resume",
            ),
            _snapshot(
                "r-blocked",
                "south",
                "blocked",
                detail=BLOCK_DETAIL,
                recovery="decide",
            ),
            _snapshot("r-work", "east", "working"),
            _snapshot("r-queued", "west", "waiting", recovery="wait"),
        )
    )


def test_launch_failed_row_names_the_launch_failure_the_number_counts() -> None:
    """The row a launch failure produces explains the blocked number it raises."""
    rows = _launch_failed_and_blocked()

    # The number is right and is unchanged: both stops arrive under blocked, and
    # the four-cell block says so on every row of the reading.
    assert BLOCK in rows["r-launch"]
    assert BLOCK in rows["r-blocked"]

    # The launch failure's own row names the launcher, so a reader who sees the
    # blocked number learn from the line what kind of stop part of that number
    # is. The classifier's cause travels to the pane rather than being dropped.
    launch_clause = rows["r-launch"][rows["r-launch"].index(BLOCK) + BLOCK_WIDTH :]
    assert "launch" in launch_clause.lower()
    assert "launch for backend 'claude'" in launch_clause
    # The remedy is a structured fact on the record, not row text: the row says
    # what happened, and the destination state's colour says whether the
    # coordinator must act. So the clause never spends a column restating the
    # action the record carries.
    assert "resume" not in launch_clause.lower()

    # An ordinary blocked run's row carries its own reason and no launch
    # wording, so the two rows are distinguishable from the lines alone.
    blocked_clause = rows["r-blocked"][rows["r-blocked"].index(BLOCK) + BLOCK_WIDTH :]
    assert "the gate needs a coordinator decision" in blocked_clause
    assert "launch" not in blocked_clause.lower()
    assert launch_clause.strip() != blocked_clause.strip()


def test_the_counter_block_keeps_four_cells_and_its_width() -> None:
    """The repair is what the line says, not a wider block."""
    rows = _launch_failed_and_blocked()
    launch = rows["r-launch"]

    # Four cells and three single-space gaps, at the width the module states
    # for the block: the number's bucket layout did not move to carry the fault.
    assert re.findall(r"\d[wbuq]", launch) == ["1w", "2b", "0u", "1q"]
    assert BLOCK in launch
    assert launch.count("·") == 0
    assert len(BLOCK) == BLOCK_WIDTH == STATS

    # Every row keeps the pane's width, so nothing wrapped to make room.
    assert all(len(row) == WIDTH for row in rows.values())


def test_a_reading_without_a_launch_failure_names_no_launch_fault() -> None:
    """The clause reports a fault rather than always mentioning one."""
    rows = _rows(
        _reading(
            _snapshot(
                "r-blocked",
                "south",
                "blocked",
                detail=BLOCK_DETAIL,
                recovery="decide",
            ),
            _snapshot("r-work", "east", "working"),
            _snapshot("r-queued", "west", "waiting", recovery="wait"),
        )
    )

    blocked = rows["r-blocked"]
    assert CONTROL_BLOCK in blocked
    assert "launch" not in blocked.lower()
    # The producer's label — "worker manifest reports blocked:" — restates the
    # state cell, so the row drops it and begins with the explanation itself.
    assert "the gate needs a coordinator decision" in blocked


def test_a_launch_failure_without_a_recorded_cause_still_names_the_fault() -> None:
    """A record that carries the state and no cause still explains its number.

    The state is reachable without a cause, and a blocked number rendering no
    reason is the defect this repair exists to close, so the row names the fault
    itself rather than depending on the classifier's sentence being present.
    """
    rows = _rows(
        _reading(_snapshot("r-launch", "north", "launch-failed", recovery="resume"))
    )

    row = rows["r-launch"]
    assert " 0w  1b  0u" in row
    assert "launch" in row.lower()
    assert FAULT_CLAUSE_PREFIX in row


def test_the_launch_failure_is_still_counted_under_the_blocked_bucket() -> None:
    """The classification and the count are unchanged; only the line changed."""
    assert ticker_module._bucket_of("launch-failed", "launch-failed") == "blocked"
