"""The pane counts every parked request, and the stuck ones apart.

A request parked before launch once fell out of the pane's waiting bucket when
its phase was a declared wait rather than the queue, so a request held on an
upstream section read as nothing in flight at all: invisible on the pane rather
than merely unmarked.

Two corrections are pinned here. Every parked request lands in the waiting
figure exactly once, whatever its phase. A request the one stuck verdict calls
stuck is counted beside the buckets in its own ``s`` cell, painted in the alarm
colour when it stays above zero and dimmed to a zero when it does not, so the
block keeps its shape on a quiet fleet.

The fleet is read through the producer's own partition and transition builder,
so the figures asserted here are the figures a follower receives.
"""

from __future__ import annotations

import re
from typing import Any

from reckon.crew import recovery_watch
from reckon.crew import ticker as ticker_module

SESSION = "s-parked"
WIDTH = 180
THEME = "light"

# One request per parked phase.
PARKED = (
    ("r-queued", "queued"),
    ("r-awaiting", "awaiting-upstream"),
    ("r-queued-blocked", "blocked-upstream"),
    ("r-held", "held"),
)

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


def plain(line: str) -> str:
    """The row as the producer's own layout reads it, colour removed."""
    return ESCAPES.sub("", line)


def _snapshot(run_id: str, state: str, *, stuck: bool = False) -> dict[str, Any]:
    """One live pointer cut down to the facts the partition and the row read."""
    return {
        "run_id": run_id,
        "node": run_id,
        "state": state,
        "detail": "parked in " + state,
        "recovery": "",
        # A parked request carries its admission phase word on the
        # classification, which is how the waiting bucket tells a held request
        # from one merely stopped.
        "recovery_classification": state,
        "role": "implement",
        "model": "m",
        "effort": "high",
        "alias": "",
        "lane": "",
        "session": SESSION,
        # The one stuck verdict writes its reason on the row, or nothing.
        "stuck": {"run_id": run_id, "phase": state, "kind": state} if stuck else None,
    }


def _fleet(*, stuck: bool = False) -> dict[str, dict[str, Any]]:
    """The session's four parked requests, its queued one stuck when asked."""
    return {
        run_id: _snapshot(run_id, state, stuck=stuck and run_id == "r-queued")
        for run_id, state in PARKED
    }


def _reading(*, stuck: bool = False, colour: bool = False) -> tuple[Any, ...]:
    """The producer's counts, its event, and the row a follower draws."""
    fleet = _fleet(stuck=stuck)
    counts = recovery_watch._transition_counts(fleet)
    event = recovery_watch._watch_transition(
        "reckon",
        kind="baseline",
        snapshot=fleet["r-queued"],
        previous=None,
        current="queued",
        counts=counts,
        spend_runs=[],
        rate_statuses={},
    )
    grid = ticker_module.Ticker(width=WIDTH, theme=THEME, color=colour)
    return counts, event, grid.render(event)


def test_every_parked_phase_counts_once_in_the_waiting_cell() -> None:
    """One request in each parked phase reads four waiting, none stuck.

    A phase that the queue does not own, such as one held on an upstream
    section, belongs in the waiting figure as much as a queued request does:
    nothing will start it without the fleet noticing.
    """
    counts, _event, line = _reading()
    assert counts["waiting"] == 4, counts
    assert counts["stuck"] == 0
    assert "4q" in plain(line), plain(line)


def test_a_stuck_request_reads_in_the_alarm_colour_beside_its_cell() -> None:
    """The stuck figure is one, in the alarm hue, and q stays four.

    A stuck request keeps the cell its phase belongs to, so the alarm figure
    recounts a row the waiting cell already holds rather than moving the
    request out of it.
    """
    counts, event, line = _reading(stuck=True, colour=True)
    assert counts["stuck"] == 1, counts
    # The figure reaches the event a follower draws, not only the counts.
    assert event["stuck"] == 1, event
    assert "4q" in plain(line), plain(line)
    alarm = "\x1b[38;5;" + str(ticker_module.STUCK_HUE[THEME]) + "m"
    assert alarm + " 1s\x1b[0m" in line, line


def test_nothing_stuck_renders_a_dimmed_zero_in_the_same_position() -> None:
    """With no stuck request the cell is a dimmed zero and the row still fits.

    The cell is drawn on every row so the block never changes shape as the
    alarm rises and clears, so a zero is painted dim rather than dropped.
    """
    counts, event, line = _reading(colour=True)
    _stuck_counts, _stuck_event, stuck_line = _reading(stuck=True)
    alarm_column = plain(stuck_line).index("1s")
    assert counts["stuck"] == 0
    assert event["stuck"] == 0
    assert "\x1b[2m 0s\x1b[0m" in line, line
    assert "4q  0s" in plain(line), plain(line)
    assert len(plain(line)) <= WIDTH, plain(line)
    # The stuck cell is the last counter, immediately right of the queue.
    # Same screen column as the alarm figure: the block does not resize.
    assert plain(line).index("0s") == alarm_column
