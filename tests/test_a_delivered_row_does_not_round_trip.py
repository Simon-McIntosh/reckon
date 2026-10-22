"""A delivered row does not round-trip on its worker's own manifest rewrite.

A worker writes a complete manifest, then rewrites that same file during its
final turn. A producer sweeping the run while the liveness of that worker is
unknown reads the rewrite once as running and once more as the delivered state
it had already drawn, a second later. Both halves would print and cancel each
other, so the departure out of a finished word is held for the settle window
and dropped together with the return; a departure that does not come back
inside the window is delivered when the window passes, so a run that really
resumes still reaches the reader.

The rows below drive ``PaneRowPath`` with an injected clock and render every
row it releases through one ``Ticker``, so what is measured is the pane's own
output rather than the policy's opinion of it.
"""

from __future__ import annotations

from typing import Any

from reckon.crew import ticker

SESSION = "s-own"
RUN = "r-delivered"
PROJECT = "example"
NODE = "example"

# The run's manifest is complete: the pane draws it as delivered.
DELIVERED = "completed_unpromoted"
# The producer re-reads the rewritten manifest as work in progress, then as the
# delivered state again — the pair observed at 14:55:19Z and 14:55:20Z on
# 2026-10-15.
AWAY = "running"


def _view(event: dict[str, Any]) -> str:
    """The line crew view draws for a row the stream leaves out."""
    return ticker.Ticker(width=180, color=False, model_aliases=()).render(
        event, with_session=True
    )


def _row(from_state: str, to_state: str, **extra: Any) -> dict[str, Any]:
    """One transition row as the producer writes it for this run."""
    event: dict[str, Any] = {
        "project": PROJECT,
        "event": "transition",
        "run_id": RUN,
        "node": NODE,
        "session": SESSION,
        "from_state": from_state,
        "to_state": to_state,
        "observed_at": "2026-10-15T14:55:20Z",
    }
    event.update(extra)
    return event


class _Pane:
    """The follower's row path and the grid it draws through, wired together."""

    def __init__(self) -> None:
        self.path = ticker.PaneRowPath(own_session=SESSION)
        self.grid = ticker.Ticker(width=180, color=False, model_aliases=())
        # The pane already shows the run as delivered, as it does once the
        # worker's first manifest has been swept.
        self.path.remember(RUN, DELIVERED)

    def feed(self, event: dict[str, Any], *, now: float) -> list[tuple[dict, str]]:
        return [
            (row, self.grid.render(row, with_session=True))
            for row in self.path.feed(event, now=now)
        ]

    def flush(self, *, now: float) -> list[tuple[dict, str]]:
        return [
            (row, self.grid.render(row, with_session=True))
            for row in self.path.flush(now=now)
        ]


def test_a_round_trip_out_of_delivered_draws_nothing() -> None:
    """The departure and its return one second apart are one round trip.

    The run leaves the delivered word the pane shows and is back in it a second
    later — a rewrite of the same manifest, not work resuming — so neither half
    reaches the pane, and nothing is left to draw once the window closes
    either.
    """
    pane = _Pane()
    released: list[tuple[dict, str]] = []
    released += pane.feed(
        _row(DELIVERED, AWAY, recovery_classification="running"), now=1000.0
    )
    released += pane.feed(
        _row(AWAY, DELIVERED, recovery_classification="scoring"), now=1001.0
    )
    released += pane.flush(now=1e9)

    assert released == [], released


def test_a_departure_out_of_delivered_is_left_to_crew_view() -> None:
    """A run that leaves delivered for a working word asks the coordinator nothing.

    The hold drops it with its return inside the window, and past the window it
    reaches the stream as no row either: the move into a working word asks the
    coordinator for nothing, so crew view draws the departure while the stream
    carries only the finished word it left.
    """
    pane = _Pane()
    departure = _row(DELIVERED, AWAY, recovery_classification="running")
    released: list[tuple[dict, str]] = []
    released += pane.feed(departure, now=1000.0)
    assert released == [], released
    released += pane.flush(now=1000.0 + ticker.SETTLE_WINDOW - 1.0)
    assert released == [], released
    released += pane.flush(now=1000.0 + ticker.SETTLE_WINDOW)

    assert released == [], released
    assert _view(departure).strip()
