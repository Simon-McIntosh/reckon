"""A refused launch of a queued request draws no drop, and names its refusal once.

The admission pass launches a request by releasing its pointer and publishing
the attempt, then writes the request back as ``queued`` when the launch is
refused. A follower watching that window sees the request's pointer leave the
fleet and come back, which it reads as a departure: on 2026-10-21 the pane drew
``new -> dropped`` for a request that had not been dropped, then
``dropped -> queued`` 34 seconds later. The request kept its place throughout.

What the pane owes the coordinator is the refusal and the remedy, once: a queued
request refused again for the same reason is the remedy it already has.

The rows below drive ``PaneRowPath`` for the pane's own session, as the follower
does, and render every row it releases through one ``Ticker``, so the assertions
are about the lines a reader would have seen.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
PROJECT = "reckon"
NODE = "a-limit-reset-is-one-record"
REFUSAL = (
    "the queued launch was refused: PlanReviewMissingError: section 73 of the "
    "plan has an unreviewed unit"
)
REMEDY = "reckon crew review --plan the-monitor-shows-the-work-it-watches"


def _row(
    from_state: str | None, to_state: str, run_id: str, **extra: Any
) -> dict[str, Any]:
    """One transition row as the project's watcher writes it."""
    event: dict[str, Any] = {
        "project": PROJECT,
        "event": "transition",
        "run_id": run_id,
        "node": NODE,
        "session": SESSION,
        "role": "implement",
        "from_state": from_state,
        "to_state": to_state,
        "observed_at": "2026-10-21T23:44:36Z",
    }
    event.update(extra)
    return event


class _Pane:
    """The follower's row path and the grid it draws through, wired together."""

    def __init__(self) -> None:
        self.path = ticker.PaneRowPath(own_session=SESSION)
        self.grid = ticker.Ticker(width=200, color=False, model_aliases=())

    def feed(
        self, event: dict[str, Any], *, now: float
    ) -> list[tuple[dict[str, Any], str]]:
        return [
            (row, self.grid.render(row, with_session=True))
            for row in self.path.feed(event, now=now)
        ]

    def flush(self, *, now: float) -> list[tuple[dict[str, Any], str]]:
        return [
            (row, self.grid.render(row, with_session=True))
            for row in self.path.flush(now=now)
        ]


def _words(rows: list[tuple[dict[str, Any], str]]) -> list[str]:
    return [ticker._pane_word(row) for row, _ in rows]


def _refused_launch(
    pane: _Pane, run_id: str, *, at: float, refusal: str = REFUSAL
) -> list[dict[str, Any]]:
    """Drive the rows of one refused launch attempt, returning what it drew."""
    drawn: list[dict[str, Any]] = []
    for offset, event in enumerate(
        (
            # The request arrives and queues for a slot, then its launch is
            # attempted: the attempt's pointer is released and the request is
            # written back, so the follower sees it leave the fleet and return.
            _row(None, "dispatched", run_id),
            _row(
                "dispatched",
                "queued",
                run_id,
                detail="waiting for a local lane slot",
                next_action="wait for a local lane slot",
            ),
            _row("queued", "withdrawn", run_id),
            _row(None, "dispatched", run_id),
            _row(
                "dispatched",
                "queued",
                run_id,
                detail=refusal,
                next_action=REMEDY,
            ),
        )
    ):
        drawn.extend(row for row, _ in pane.feed(event, now=at + offset))
    return drawn


def test_a_refused_launch_draws_no_dropped_row_and_no_second_queued_row() -> None:
    pane = _Pane()
    drawn = _refused_launch(pane, "r-refused", at=1000.0)
    drawn.extend(row for row, _ in pane.flush(now=9000.0))

    words = _words([(row, "") for row in drawn])
    assert "dropped" not in words
    # The refusal is the one row the launch attempt owes: the refusal and the
    # remedy the classifier named. The re-queue under the fresh id adds no
    # second row of its own, because the request's word never left queued.
    assert len(drawn) == 1
    row = drawn[0]
    assert row.get(ticker.QUEUED_REFUSED_FIELD) is not None
    assert "PlanReviewMissingError" in str(row.get(ticker.QUEUED_REFUSED_FIELD))
    assert REMEDY in str(row.get(ticker.QUEUED_REFUSED_FIELD))


def test_the_refusal_is_named_on_the_request_s_row_and_it_stays_queued() -> None:
    pane = _Pane()
    drawn = _refused_launch(pane, "r-refused", at=1000.0)
    assert drawn, "the refused launch drew nothing at all"
    rendered = pane.grid.render(drawn[0], with_session=True)
    assert "refused" in rendered
    assert "PlanReviewMissingError" in rendered
    # The clause is cut to the pane like every other cell, so the remedy is
    # asserted on the row the renderer reads rather than on the cut line.
    assert REMEDY in str(drawn[0].get(ticker.QUEUED_REFUSED_FIELD))
    assert "reckon crew review" in rendered
    # The request's own word is unchanged, so the pane still holds it as queued
    # and the launch row that follows it starts from that word.
    assert pane.path.reported.get("r-refused") == "queued"


def test_a_second_identical_refusal_draws_nothing() -> None:
    pane = _Pane()
    first = _refused_launch(pane, "r-refused", at=1000.0)
    assert len(first) == 1
    second = _refused_launch(pane, "r-refused", at=1100.0)
    assert [row for row, _ in pane.flush(now=9000.0)] == []
    assert second == []


def test_a_refusal_the_request_has_not_shown_before_is_named_again() -> None:
    pane = _Pane()
    assert len(_refused_launch(pane, "r-refused", at=1000.0)) == 1
    second = "the queued launch was refused: PlanReviewMissingError: another unit"
    assert len(_refused_launch(pane, "r-refused", at=1100.0, refusal=second)) == 1
    other = (
        "the queued launch was refused: ScopeConflict: another run holds "
        "reckon/crew/ticker.py"
    )
    rows = (
        pane.feed(_row("queued", "withdrawn", "r-refused"), now=1200.0)
        + pane.feed(_row(None, "dispatched", "r-refused"), now=1201.0)
        + pane.feed(
            _row("dispatched", "queued", "r-refused", detail=other, next_action=""),
            now=1202.0,
        )
    )
    assert len([row for row, _ in rows if row.get(ticker.QUEUED_REFUSED_FIELD)]) == 1


def test_a_discarded_request_still_draws_dropped() -> None:
    """A request that really left the queue is still drawn as a drop.

    The refusal branch must not swallow the row that tells a coordinator its
    request is gone: a ``queued -> discarded`` move has no run behind it, so the
    pane names it as dropped, exactly as it did before.
    """
    pane = _Pane()
    drawn: list[tuple[dict[str, Any], str]] = []
    for offset, event in enumerate(
        (
            _row(None, "dispatched", "r-cancelled"),
            _row(
                "dispatched",
                "queued",
                "r-cancelled",
                detail="waiting for a local lane slot",
                next_action="wait for a local lane slot",
            ),
            _row("queued", "discarded", "r-cancelled"),
        )
    ):
        drawn.extend(pane.feed(event, now=1000.0 + offset))
    drawn.extend(pane.flush(now=9000.0))
    assert _words(drawn) == ["dropped"]
