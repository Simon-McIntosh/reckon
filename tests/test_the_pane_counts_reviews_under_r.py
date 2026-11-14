"""A review is counted under its own ``r``, and a run being repaired is working.

The counters are how a coordinator reads a session at a glance: a session whose
only live run is a review printed ``0w``, which reads as an idle session when a
review is running. The working figure is therefore split by the role the run
was launched with, and the review work gets the pane word ``in review`` and the
letter ``r``, drawn after ``w``.

A run the review sent back into a repair round keeps the verdict it delivered
until it delivers again, so counting its state alone put a repairing run under
``d``: the reader saw delivered work with no worker on it. A repair round that
holds a live attempt is working again, and it counts under the working figure.

These cases go through the producer's own partition and transition builder, so
each figure asserted here is the figure a pane receives rather than a fixture's
opinion of one.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from reckon.crew import recovery_watch
from reckon.crew import ticker as ticker_module
from reckon.crew.recovery import _fleet_counts, _watch_transition

WIDTH = 180


def _snapshot(
    run_id: str,
    node: str,
    state: str,
    *,
    role: str = "implement",
    repair_in_flight: bool = False,
) -> dict[str, Any]:
    """One live pointer reduced to the facts the partition and the row read."""
    return {
        "run_id": run_id,
        "node": node,
        "state": state,
        "detail": "",
        "recovery_classification": state,
        "recovery": "",
        "role": role,
        "repair_in_flight": repair_in_flight,
        "model": "dsv4-flash",
        "effort": "high",
        "alias": "",
        "session": "",
    }


def _reading(*snapshots: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """The fleet's figures and one row per snapshot, through the producer."""
    counts = _fleet_counts({snapshot["run_id"]: snapshot for snapshot in snapshots})
    rows = [
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
    return counts, rows


def _render(row: dict[str, Any]) -> str:
    return ticker_module.Ticker(width=WIDTH).render(row)


def test_one_implementation_and_one_review_count_1w_1r() -> None:
    """A working review is counted under ``r`` beside the implementation's ``w``.

    This is the reading the counter exists for: the review is live work, and a
    single working figure would report the session as having one worker without
    saying whether that worker implemented or reviewed.
    """
    counts, rows = _reading(
        _snapshot("r-impl", "north", "working"),
        _snapshot("r-review", "review-of-r-impl-abc123", "working", role="review"),
    )
    assert counts["working"] == 1, counts
    assert counts["reviewing"] == 1, counts

    line = _render(rows[0])
    assert re.search(r"\b1w\s+1r\b", line), line

    # A fleet whose only live run is a review reads as reviewing, not idle.
    review_only, _ = _reading(
        _snapshot("r-review", "review-of-r-impl-abc123", "running", role="review")
    )
    assert review_only["working"] == 0, review_only
    assert review_only["reviewing"] == 1, review_only


def test_a_delivered_run_with_a_live_repair_attempt_counts_as_working() -> None:
    """A repairing run is working, not delivered.

    The review sent the run back to work, and its manifest keeps the verdict it
    delivered until the repair delivers again. Counting the state alone reports
    it as delivered, which reads as work waiting on the coordinator rather than
    work in hand.
    """
    counts, rows = _reading(
        _snapshot("r-repair", "north", "complete", repair_in_flight=True)
    )
    assert counts["working"] == 1, counts
    assert counts["unpromoted"] == 0, counts

    line = _render(rows[0])
    assert "1w" in line, line
    assert " 0d" in line, line

    # A delivered run with no live attempt is still delivered.
    idle, _ = _reading(_snapshot("r-done", "south", "complete"))
    assert idle["working"] == 0, idle
    assert idle["unpromoted"] == 1, idle


def test_the_row_carries_five_right_aligned_cells_in_the_order_w_r_n_d_q() -> None:
    """The row renders five counter cells, letters in order, at one fixed width.

    Five cells render even on a fleet holding no review, so the block never
    changes shape as reviews start and stop and the clause after it keeps its
    column. Each count is right-aligned to the block's fixed digit width, so
    the letters hold their columns across one- and two-digit fleets.
    """
    counts, rows = _reading(
        _snapshot("r-work", "north", "working"),
        _snapshot("r-review", "review-of-r-work-abc123", "working", role="review"),
    )
    line = _render(rows[0])
    assert re.findall(r"\d+([wrdqn])", line) == ["w", "r", "n", "d", "q"], line

    plain, plain_rows = _reading(_snapshot("r-work", "north", "working"))
    assert plain.get("reviewing", 0) == 0
    assert re.findall(r"\d+([wrdqn])", _render(plain_rows[0])) == [
        "w",
        "r",
        "n",
        "d",
        "q",
    ]

    assert ticker_module._MAX_CELLS == (
        "working",
        "in review",
        "needs you",
        "delivered",
        "waiting",
    )
    assert ticker_module._COUNT_FIELD["in review"] == "reviewing"
    assert ticker_module.STAT_LETTER["in review"] == "r"

    digits = ticker_module.STAT_DIGITS
    cells = ticker_module.Ticker()._stats(
        {"working": 3, "reviewing": 2, "blocked": 1, "unpromoted": 7, "waiting": 4}
    )
    texts = [text for text, _ in cells if text != " "]
    assert texts == [
        f"{3:>{digits}}w",
        f"{2:>{digits}}r",
        f"{1:>{digits}}n",
        f"{7:>{digits}}d",
        f"{4:>{digits}}q",
    ]
    assert len("".join(text for text, _ in cells)) == ticker_module.STATS
    assert counts["reviewing"] == 1


@pytest.mark.parametrize(
    ("status", "live"),
    [("resumed", True), ("dispatched", True), ("refused", False), ("held", False)],
)
def test_the_repair_record_says_whether_a_worker_is_answering(
    status: str, live: bool
) -> None:
    """Only an opening repair record means the round has a live attempt.

    A round the reflex held, declined, refused or exhausted produced no worker
    answering it, so it names nothing in flight however recently it was written.
    """
    pointer = {"repair_dispatch": {"status": status, "round_id": "r-abc@deadbeef"}}
    assert recovery_watch._repair_attempt_live(pointer) is live
    assert recovery_watch._repair_attempt_live({"repair_dispatch": "not a mapping"}) is False
    assert recovery_watch._repair_attempt_live({}) is False